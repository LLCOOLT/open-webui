"""Runs backtests off the event loop.

Bars are fetched asynchronously in the server process; the CPU-bound simulation
runs in a separate worker process (spawned, so it never inherits the server's
threads or sockets) with a hard timeout.
"""

import asyncio
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field

from open_webui.models.finance import FinanceBacktestModel, FinanceBacktests, FinanceStrategies
from open_webui.utils.finance import service
from open_webui.utils.finance.backtest.engine import MAX_GRID_COMBOS, BacktestError, run_job
from open_webui.utils.finance.backtest.template import TemplateError, parse_definition
from open_webui.utils.finance.base import TIMEFRAMES, FinanceProviderError, normalize_symbol

log = logging.getLogger(__name__)

WORKERS = max(1, int(os.getenv('FINANCE_BACKTEST_WORKERS', '2')))
TIMEOUT = max(10, int(os.getenv('FINANCE_BACKTEST_TIMEOUT', '300')))
MAX_SYMBOLS = 20

_pool: Optional[ProcessPoolExecutor] = None
_tasks: set[asyncio.Task] = set()


class BacktestRequest(BaseModel):
    strategy_id: Optional[str] = None
    definition: Optional[dict] = None  # inline template, no saved strategy
    symbols: Optional[list[str]] = Field(default=None, max_length=MAX_SYMBOLS)
    timeframe: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    params: Optional[dict[str, float]] = None
    grid: Optional[dict[str, list[float]]] = None
    initial_cash: float = Field(default=10000, gt=0, le=1e12)
    commission_bps: float = Field(default=0, ge=0, le=1000)
    slippage_bps: float = Field(default=5, ge=0, le=1000)


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=multiprocessing.get_context('spawn'))
    return _pool


def _reset_pool() -> None:
    """Kill workers after a timeout; a stuck strategy must not hold a slot forever."""
    global _pool
    pool, _pool = _pool, None
    if pool is None:
        return
    for proc in list(getattr(pool, '_processes', {}).values()):
        try:
            proc.terminate()
        except Exception:
            pass
    pool.shutdown(wait=False, cancel_futures=True)


def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def prepare(user, form: BacktestRequest) -> tuple[dict, str, Optional[str], Optional[int]]:
    """Validate a request and resolve it into a job config. Raises FinanceProviderError (400/403/404)."""
    strategy = None
    if form.strategy_id:
        strategy = await FinanceStrategies.get_by_id(form.strategy_id)
        if not strategy or strategy.user_id != user.id:
            raise FinanceProviderError('Strategy not found', status_code=404)
        kind, definition, source = strategy.kind, strategy.definition, strategy.source
        params = {**strategy.params, **(form.params or {})}
        symbols = form.symbols or strategy.universe
        timeframe = form.timeframe or strategy.timeframe
        name = strategy.name
    elif form.definition:
        kind, definition, source = 'template', form.definition, None
        params = dict(form.params or {})
        symbols = form.symbols or ['SPY']
        timeframe = form.timeframe or '1Day'
        name = 'Ad-hoc template'
    else:
        raise FinanceProviderError('Provide a strategy_id or an inline template definition', status_code=400)

    if kind == 'python' and user.role != 'admin':
        raise FinanceProviderError('Only admins can run Python strategies', status_code=403)
    if kind == 'template':
        try:
            parse_definition(definition or {})
        except TemplateError as e:
            raise FinanceProviderError(f'Invalid template: {e}', status_code=400)

    if timeframe not in TIMEFRAMES:
        raise FinanceProviderError(f'timeframe must be one of {", ".join(TIMEFRAMES)}', status_code=400)
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if not symbols or len(symbols) > MAX_SYMBOLS:
        raise FinanceProviderError(f'Provide between 1 and {MAX_SYMBOLS} symbols', status_code=400)
    if form.grid:
        count = 1
        for values in form.grid.values():
            count *= max(1, len(values))
        if count > MAX_GRID_COMBOS:
            raise FinanceProviderError(f'Grid has {count} combinations; the maximum is {MAX_GRID_COMBOS}', status_code=400)

    end = form.end or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    start = form.start or (datetime.now(timezone.utc) - timedelta(days=365 * 5)).strftime('%Y-%m-%d')

    config = {
        'symbols': symbols,
        'timeframe': timeframe,
        'start': start,
        'end': end,
        'params': params,
        'grid': form.grid or None,
        'initial_cash': form.initial_cash,
        'commission_bps': form.commission_bps,
        'slippage_bps': form.slippage_bps,
        'kind': kind,
        'definition': definition if strategy is None else None,  # inline runs keep their definition
    }
    # Underscored keys are passed to the worker but not stored with the backtest row.
    config['_definition'] = definition
    config['_source'] = source
    return config, name, (strategy.id if strategy else None), (strategy.version if strategy else None)


async def _execute(backtest_id: str, user_id: str, config: dict) -> None:
    try:
        await FinanceBacktests.update_status(backtest_id, 'running')
        broker = await service.get_broker(user_id)
        bars = {}
        for symbol in config['symbols']:
            series = await service.get_bars(broker, symbol, config['timeframe'], config['start'], config['end'])
            bars[symbol] = [b.model_dump() for b in series]

        job = {
            'bars': bars,
            'kind': config['kind'],
            'definition': config.get('_definition'),
            'source': config.get('_source'),
            'timeframe': config['timeframe'],
            'params': config['params'],
            'grid': config.get('grid'),
            'initial_cash': config['initial_cash'],
            'commission_bps': config['commission_bps'],
            'slippage_bps': config['slippage_bps'],
        }
        loop = asyncio.get_running_loop()
        try:
            out = await asyncio.wait_for(loop.run_in_executor(_get_pool(), run_job, job), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            _reset_pool()
            raise BacktestError(f'Backtest exceeded the {TIMEOUT}s time limit')

        await FinanceBacktests.update_status(
            backtest_id,
            'completed',
            metrics=out['metrics'],
            benchmark=out['benchmark'],
            result=out['result'],
        )
    except (FinanceProviderError, BacktestError) as e:
        await FinanceBacktests.update_status(backtest_id, 'failed', error=str(e))
    except Exception as e:
        log.exception('backtest %s failed', backtest_id)
        await FinanceBacktests.update_status(backtest_id, 'failed', error=f'{type(e).__name__}: {e}')


async def start(user, form: BacktestRequest) -> FinanceBacktestModel:
    config, name, strategy_id, version = await prepare(user, form)
    stored = {k: v for k, v in config.items() if not k.startswith('_')}
    backtest = await FinanceBacktests.insert(user.id, name, stored, strategy_id=strategy_id, strategy_version=version)
    task = asyncio.create_task(_execute(backtest.id, user.id, config))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return backtest


async def start_and_wait(user, form: BacktestRequest, wait: float = 120) -> FinanceBacktestModel:
    backtest = await start(user, form)
    deadline = asyncio.get_running_loop().time() + wait
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.5)
        current = await FinanceBacktests.get_by_id(backtest.id)
        if current and current.status in ('completed', 'failed'):
            return current
    return await FinanceBacktests.get_by_id(backtest.id)
