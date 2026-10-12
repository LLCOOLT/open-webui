import math

import numpy as np
import pandas as pd
import pytest

from open_webui.utils.finance.backtest import engine, indicators
from open_webui.utils.finance.backtest.engine import BacktestError, run_job
from open_webui.utils.finance.backtest.starters import PYTHON_STARTER, STARTER_TEMPLATES
from open_webui.utils.finance.backtest.template import TemplateError, template_signals
from open_webui.utils.finance.backtest.validate import validate_strategy

DAY = 86400


def make_bars(closes, opens=None, highs=None, lows=None, start=1_600_000_000):
    opens = opens or closes
    return [
        {
            't': start + i * DAY,
            'o': opens[i],
            'h': (highs or [max(o, c) for o, c in zip(opens, closes)])[i],
            'l': (lows or [min(o, c) for o, c in zip(opens, closes)])[i],
            'c': closes[i],
            'v': 1000,
        }
        for i in range(len(closes))
    ]


def frame(bars):
    return engine.bars_to_frame(bars)


def test_indicators_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert indicators.sma(s, 2).tolist()[1:] == [1.5, 2.5, 3.5, 4.5]
    assert math.isnan(indicators.sma(s, 2).iloc[0])
    assert indicators.rsi(pd.Series(np.arange(1.0, 30)), 14).iloc[-1] == 100
    bb = indicators.bbands(pd.Series([2.0] * 25), 20, 2)
    assert bb['upper'].iloc[-1] == bb['lower'].iloc[-1] == 2


def test_signals_fill_next_open_no_lookahead():
    # Entry signal on bar 1's close must fill at bar 2's open (11), not bar 1's close.
    closes = [10, 10, 12, 13, 13]
    opens = [10, 10, 11, 12, 13]
    df = frame(make_bars(closes, opens))
    entries = pd.Series([False, True, False, False, False], index=df.index)
    exits = pd.Series([False, False, False, True, False], index=df.index)
    equity, trades, bars_in = engine.simulate(df, entries, exits, {'size': 1.0}, 1000, 0, 0, 'X')
    assert len(trades) == 1
    t = trades[0]
    assert t['entry_price'] == 11 and t['exit_price'] == 13 and t['exit_reason'] == 'signal'
    assert t['entry_t'] == df.index[2] and t['exit_t'] == df.index[4]
    assert equity.iloc[-1] == pytest.approx(1000 * 13 / 11)
    assert equity.iloc[1] == 1000  # untouched before the fill


def test_stop_loss_gap_fills_at_open():
    closes = [10, 10, 10, 7, 7]
    opens = [10, 10, 10, 8, 7]
    df = frame(make_bars(closes, opens))
    entries = pd.Series([True, False, False, False, False], index=df.index)
    exits = pd.Series(False, index=df.index)
    _, trades, _ = engine.simulate(df, entries, exits, {'stop_loss': 0.1, 'size': 1.0}, 1000, 0, 0, 'X')
    assert trades[0]['exit_reason'] == 'stop_loss'
    assert trades[0]['exit_price'] == 8  # gapped below the 9 stop, so filled at the open


def test_costs_reduce_returns():
    df = frame(make_bars([10, 10, 11, 12, 12]))
    entries = pd.Series([True, False, False, False, False], index=df.index)
    exits = pd.Series(False, index=df.index)
    free, _, _ = engine.simulate(df, entries, exits, {'size': 1.0}, 1000, 0, 0, 'X')
    costly, _, _ = engine.simulate(df, entries, exits, {'size': 1.0}, 1000, 0.001, 0.001, 'X')
    assert costly.iloc[-1] < free.iloc[-1]


def test_metrics_drawdown_and_return():
    eq = pd.Series([100.0, 120, 90, 110], index=[0, DAY, 2 * DAY, 3 * DAY])
    m = engine.compute_metrics(eq, 100, '1Day', trades=[], exposure=0.5)
    assert m['total_return'] == pytest.approx(0.1)
    assert m['max_drawdown'] == pytest.approx(90 / 120 - 1)
    assert m['trades'] == 0 and m['exposure'] == 0.5


@pytest.mark.parametrize('starter', STARTER_TEMPLATES, ids=lambda s: s['id'])
def test_starters_run(starter):
    rng = np.random.default_rng(1)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, 600))))
    out = run_job(
        {
            'bars': {'SPY': make_bars(closes)},
            'kind': 'template',
            'definition': starter['definition'],
            'params': starter['params'],
            'timeframe': '1Day',
            'initial_cash': 10000,
            'commission_bps': 0,
            'slippage_bps': 5,
        }
    )
    assert out['metrics']['final_equity'] > 0
    assert out['benchmark']['final_equity'] > 0
    assert len(out['result']['equity']) == 600


def test_grid_ranks_and_multi_symbol():
    rng = np.random.default_rng(2)
    bars = {
        s: make_bars(list(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, 400))))) for s in ('AAA', 'BBB')
    }
    ema = next(t for t in STARTER_TEMPLATES if t['id'] == 'ema_cross')
    out = run_job(
        {
            'bars': bars,
            'kind': 'template',
            'definition': ema['definition'],
            'params': ema['params'],
            'grid': {'fast': [5, 10], 'slow': [30, 60]},
            'timeframe': '1Day',
            'initial_cash': 10000,
        }
    )
    grid = out['result']['grid']
    assert len(grid) == 4
    sharpes = [g['metrics']['sharpe'] if g['metrics']['sharpe'] is not None else -math.inf for g in grid]
    assert sharpes == sorted(sharpes, reverse=True)
    assert out['result']['best_params'] == grid[0]['params']
    assert set(out['result']['per_symbol']) == {'AAA', 'BBB'}


def test_grid_limit():
    with pytest.raises(BacktestError):
        engine.expand_grid({}, {'a': list(range(20)), 'b': list(range(20))})


def test_python_strategy_matches_template():
    rng = np.random.default_rng(3)
    bars = make_bars(list(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))))
    ema = next(t for t in STARTER_TEMPLATES if t['id'] == 'ema_cross')
    common = {'bars': {'X': bars}, 'params': {'fast': 20, 'slow': 50}, 'timeframe': '1Day', 'initial_cash': 1000}
    a = run_job({**common, 'kind': 'template', 'definition': ema['definition']})
    b = run_job({**common, 'kind': 'python', 'source': PYTHON_STARTER})
    assert a['metrics']['final_equity'] == pytest.approx(b['metrics']['final_equity'])


def test_template_errors():
    df = frame(make_bars([1.0] * 50))
    bad = [
        {'entry': {'all': [{'left': 'nope', 'op': '>', 'right': 1}]}},
        {'indicators': [{'id': 'x', 'type': 'sma', 'period': '$missing'}], 'entry': {'all': [{'left': 'x', 'op': '>', 'right': 1}]}},
        {'indicators': [{'id': 'close', 'type': 'sma', 'period': 5}], 'entry': {'all': [{'left': 'close', 'op': '>', 'right': 1}]}},
        {'entry': {'all': []}},
        {'entry': {'all': [{'left': 'close', 'op': '>', 'right': 1}]}, 'position_size_pct': 150},
    ]
    for d in bad:
        with pytest.raises(TemplateError):
            template_signals(d, df, {})


def test_validate_python_is_parse_only():
    with pytest.raises(BacktestError):
        validate_strategy('python', None, 'def x(:', {})
    with pytest.raises(BacktestError):
        validate_strategy('python', None, 'def other(df, params):\n    return 1', {})
    # Valid source is not executed during validation.
    validate_strategy('python', None, 'raise SystemExit\ndef signals(df, params):\n    return df, df', {})
