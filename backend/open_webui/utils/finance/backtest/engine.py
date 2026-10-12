"""Deterministic bar-based backtest engine (long only).

Fill model, chosen to avoid look-ahead:
- Signals are evaluated on bar i's close and filled at bar i+1's open, with slippage.
- Stop-loss / take-profit are checked against each bar's low/high from the fill bar on;
  gaps fill at the open. If both trigger in one bar the stop wins (conservative).
- Open positions are marked to the last close at the end of the test.
"""

import itertools
import math
from typing import Callable, Optional

import numpy as np
import pandas as pd

from open_webui.utils.finance.backtest import indicators
from open_webui.utils.finance.backtest.template import TemplateError, template_signals

BARS_PER_YEAR = {
    '1Min': 252 * 390,
    '5Min': 252 * 78,
    '15Min': 252 * 26,
    '1Hour': 252 * 7,
    '1Day': 252,
    '1Week': 52,
}
MAX_GRID_COMBOS = 200
MIN_BARS = 2

SignalFn = Callable[[pd.DataFrame, dict], tuple[pd.Series, pd.Series, dict]]


class BacktestError(ValueError):
    pass


def bars_to_frame(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    if df.empty:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    df = df.drop_duplicates('t').sort_values('t').set_index('t')
    if 'volume' not in df:
        df['volume'] = np.nan
    return df[['open', 'high', 'low', 'close', 'volume']].astype(float)


# ---------------------------------------------------------------------------
# Signal sources
# ---------------------------------------------------------------------------


def compile_python_strategy(source: str) -> SignalFn:
    """Compile user Python exposing `signals(df, params)`.

    NOT a sandbox: callers must restrict this to trusted (admin) users and run it
    in the worker process, never in the web server's event loop.
    """
    namespace = {'pd': pd, 'np': np, 'ta': indicators, '__name__': 'finance_strategy'}
    try:
        exec(compile(source, '<strategy>', 'exec'), namespace)
    except Exception as e:
        raise BacktestError(f'Strategy source failed to load: {type(e).__name__}: {e}') from e
    fn = namespace.get('signals')
    if not callable(fn):
        raise BacktestError('Python strategies must define signals(df, params)')

    def run(df: pd.DataFrame, params: dict):
        out = fn(df.copy(), dict(params))
        if not isinstance(out, tuple) or len(out) not in (2, 3):
            raise BacktestError('signals() must return (entries, exits) or (entries, exits, risk)')
        entries, exits = out[0], out[1]
        risk = dict(out[2]) if len(out) == 3 and out[2] else {}
        entries = pd.Series(entries, index=df.index).fillna(False).astype(bool)
        exits = pd.Series(exits, index=df.index).fillna(False).astype(bool)
        return entries, exits, {
            'stop_loss': risk.get('stop_loss'),
            'take_profit': risk.get('take_profit'),
            'size': risk.get('size', 1.0),
        }

    return run


def make_signal_fn(kind: str, definition: Optional[dict], source: Optional[str]) -> SignalFn:
    if kind == 'template':
        if not definition:
            raise BacktestError('Template strategies need a definition')

        def run(df, params):
            try:
                return template_signals(definition, df, params)
            except TemplateError as e:
                raise BacktestError(str(e)) from e

        return run
    if kind == 'python':
        if not source:
            raise BacktestError('Python strategies need source code')
        return compile_python_strategy(source)
    raise BacktestError(f'Unknown strategy kind: {kind}')


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def simulate(
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    risk: dict,
    cash: float,
    commission: float,
    slippage: float,
    symbol: str,
) -> tuple[pd.Series, list[dict], int]:
    o, h, l, c = (df[k].to_numpy() for k in ('open', 'high', 'low', 'close'))
    ts = df.index.to_numpy()
    entry_sig = entries.to_numpy()
    exit_sig = exits.to_numpy()
    stop_pct, take_pct = risk.get('stop_loss'), risk.get('take_profit')
    size = float(risk.get('size') or 1.0)
    if not 0 < size <= 1:
        raise BacktestError('position size must be between 0 and 100%')

    n = len(df)
    equity = np.empty(n)
    trades: list[dict] = []
    qty = 0.0
    entry_price = entry_cost = 0.0
    entry_t = None
    bars_in_market = 0
    pending: Optional[str] = None  # 'buy' | 'sell' queued for this bar's open

    def close_position(i: int, raw_price: float, reason: str):
        nonlocal cash, qty
        price = raw_price * (1 - slippage)
        proceeds = qty * price * (1 - commission)
        cash += proceeds
        trades.append(
            {
                'symbol': symbol,
                'entry_t': int(entry_t),
                'entry_price': entry_price,
                'exit_t': int(ts[i]),
                'exit_price': price,
                'qty': qty,
                'pnl': proceeds - entry_cost,
                'return_pct': proceeds / entry_cost - 1 if entry_cost else 0.0,
                'exit_reason': reason,
            }
        )
        qty = 0.0

    for i in range(n):
        if pending == 'sell' and qty > 0:
            close_position(i, o[i], 'signal')
        elif pending == 'buy' and qty == 0:
            price = o[i] * (1 + slippage)
            budget = cash * size
            qty = budget / (price * (1 + commission))
            if qty > 0:
                entry_cost = budget
                cash -= budget
                entry_price, entry_t = price, ts[i]
        pending = None

        if qty > 0:
            stop = entry_price * (1 - stop_pct) if stop_pct else None
            take = entry_price * (1 + take_pct) if take_pct else None
            if stop is not None and l[i] <= stop:
                close_position(i, min(o[i], stop), 'stop_loss')
            elif take is not None and h[i] >= take:
                close_position(i, max(o[i], take), 'take_profit')

        if qty > 0:
            bars_in_market += 1
        equity[i] = cash + qty * c[i]

        if i < n - 1:
            if qty > 0 and exit_sig[i]:
                pending = 'sell'
            elif qty == 0 and entry_sig[i] and not exit_sig[i]:
                pending = 'buy'

    if qty > 0:
        close_position(n - 1, c[-1], 'end')
        equity[-1] = cash

    return pd.Series(equity, index=df.index), trades, bars_in_market


def buy_and_hold(df: pd.DataFrame, cash: float, commission: float, slippage: float) -> pd.Series:
    price = df['open'].iloc[0] * (1 + slippage)
    qty = cash / (price * (1 + commission))
    return qty * df['close']


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _clean(x) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def compute_metrics(
    equity: pd.Series, initial: float, timeframe: str, trades: Optional[list[dict]] = None, exposure: Optional[float] = None
) -> dict:
    if len(equity) < MIN_BARS:
        return {}
    final = float(equity.iloc[-1])
    total_return = final / initial - 1
    years = (int(equity.index[-1]) - int(equity.index[0])) / (365.25 * 86400)
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else None

    returns = pd.concat([pd.Series([equity.iloc[0] / initial - 1]), equity.pct_change().iloc[1:]]).to_numpy()
    bpy = BARS_PER_YEAR.get(timeframe, 252)
    std = returns.std(ddof=1) if len(returns) > 1 else 0
    sharpe = returns.mean() / std * math.sqrt(bpy) if std > 0 else None
    downside = returns[returns < 0]
    dstd = math.sqrt((downside**2).sum() / len(returns)) if len(downside) else 0
    sortino = returns.mean() / dstd * math.sqrt(bpy) if dstd > 0 else None
    max_dd = float((equity / equity.cummax() - 1).min())

    metrics = {
        'total_return': _clean(total_return),
        'cagr': _clean(cagr),
        'sharpe': _clean(sharpe),
        'sortino': _clean(sortino),
        'max_drawdown': _clean(max_dd),
        'final_equity': _clean(final),
        'exposure': _clean(exposure),
        'trades': None,
        'win_rate': None,
        'profit_factor': None,
        'avg_trade_return': None,
    }
    if trades is not None:
        metrics['trades'] = len(trades)
        if trades:
            wins = [t['pnl'] for t in trades if t['pnl'] > 0]
            losses = [-t['pnl'] for t in trades if t['pnl'] < 0]
            metrics['win_rate'] = len(wins) / len(trades)
            metrics['profit_factor'] = _clean(sum(wins) / sum(losses)) if losses else None
            metrics['avg_trade_return'] = _clean(sum(t['return_pct'] for t in trades) / len(trades))
    return metrics


# ---------------------------------------------------------------------------
# Portfolio runs
# ---------------------------------------------------------------------------


def _combine(curves: dict[str, pd.Series], allocations: dict[str, float]) -> pd.Series:
    """Sum per-symbol equity on the union of timestamps. Before a symbol's first
    bar its allocation is held as cash; after its last bar the value carries forward."""
    frame = pd.DataFrame(curves).sort_index()
    for sym, alloc in allocations.items():
        frame[sym] = frame[sym].ffill().fillna(alloc)
    return frame.sum(axis=1)


def run_portfolio(
    frames: dict[str, pd.DataFrame],
    signal_fn: SignalFn,
    params: dict,
    timeframe: str,
    initial_cash: float,
    commission: float,
    slippage: float,
    detail: bool = True,
) -> dict:
    allocation = initial_cash / len(frames)
    curves, bench_curves, all_trades, per_symbol = {}, {}, [], {}
    total_bars = in_market = 0

    for sym, df in frames.items():
        entries, exits, risk = signal_fn(df, params)
        equity, trades, bars_in = simulate(df, entries, exits, risk, allocation, commission, slippage, sym)
        bench = buy_and_hold(df, allocation, commission, slippage)
        curves[sym], bench_curves[sym] = equity, bench
        all_trades.extend(trades)
        total_bars += len(df)
        in_market += bars_in
        if detail:
            per_symbol[sym] = {
                'metrics': compute_metrics(equity, allocation, timeframe, trades, bars_in / len(df)),
                'benchmark': compute_metrics(bench, allocation, timeframe),
            }

    equity = _combine(curves, {s: allocation for s in frames})
    bench = _combine(bench_curves, {s: allocation for s in frames})
    exposure = in_market / total_bars if total_bars else None
    out = {
        'metrics': compute_metrics(equity, initial_cash, timeframe, all_trades, exposure),
        'benchmark': compute_metrics(bench, initial_cash, timeframe),
    }
    if detail:
        all_trades.sort(key=lambda t: t['entry_t'])
        out['result'] = {
            'equity': [{'t': int(t), 'v': round(float(v), 4)} for t, v in equity.items()],
            'benchmark_equity': [{'t': int(t), 'v': round(float(v), 4)} for t, v in bench.items()],
            'trades': all_trades,
            'per_symbol': per_symbol,
        }
    return out


def expand_grid(base: dict, grid: Optional[dict]) -> list[dict]:
    if not grid:
        return [dict(base)]
    keys = list(grid)
    values = [list(grid[k]) for k in keys]
    if any(not v for v in values):
        raise BacktestError('every grid parameter needs at least one value')
    count = math.prod(len(v) for v in values)
    if count > MAX_GRID_COMBOS:
        raise BacktestError(f'grid has {count} combinations; the maximum is {MAX_GRID_COMBOS}')
    return [{**base, **dict(zip(keys, combo))} for combo in itertools.product(*values)]


def _rank_key(entry: dict):
    sharpe = entry['metrics'].get('sharpe')
    return sharpe if sharpe is not None else -math.inf


def run_job(job: dict) -> dict:
    """Entry point executed in the worker process. `job` is plain data so it pickles."""
    frames = {}
    for sym, bars in job['bars'].items():
        df = bars_to_frame(bars)
        if len(df) < MIN_BARS:
            raise BacktestError(f'Not enough data for {sym} in the requested range')
        frames[sym] = df
    if not frames:
        raise BacktestError('No symbols to test')

    signal_fn = make_signal_fn(job['kind'], job.get('definition'), job.get('source'))
    timeframe = job.get('timeframe', '1Day')
    cash = float(job.get('initial_cash', 10000))
    commission = float(job.get('commission_bps', 0)) / 10000
    slippage = float(job.get('slippage_bps', 0)) / 10000
    combos = expand_grid(job.get('params') or {}, job.get('grid'))

    if len(combos) == 1:
        return run_portfolio(frames, signal_fn, combos[0], timeframe, cash, commission, slippage)

    ranking = []
    for params in combos:
        r = run_portfolio(frames, signal_fn, params, timeframe, cash, commission, slippage, detail=False)
        ranking.append({'params': params, 'metrics': r['metrics']})
    ranking.sort(key=_rank_key, reverse=True)

    best = ranking[0]['params']
    out = run_portfolio(frames, signal_fn, best, timeframe, cash, commission, slippage)
    out['result']['grid'] = ranking
    out['result']['best_params'] = best
    return out
