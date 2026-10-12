"""Starter strategy templates offered in the UI and to the model."""

STARTER_TEMPLATES: list[dict] = [
    {
        'id': 'ema_cross',
        'name': 'EMA crossover',
        'description': 'Buy when the fast EMA crosses above the slow EMA; sell when it crosses back below.',
        'params': {'fast': 20, 'slow': 50},
        'definition': {
            'direction': 'long',
            'indicators': [
                {'id': 'fast', 'type': 'ema', 'period': '$fast'},
                {'id': 'slow', 'type': 'ema', 'period': '$slow'},
            ],
            'entry': {'all': [{'left': 'fast', 'op': 'crosses_above', 'right': 'slow'}]},
            'exit': {'any': [{'left': 'fast', 'op': 'crosses_below', 'right': 'slow'}]},
            'position_size_pct': 100,
        },
    },
    {
        'id': 'sma_trend',
        'name': 'SMA trend filter',
        'description': 'Hold while the close is above its long simple moving average (e.g. the 200-day).',
        'params': {'period': 200},
        'definition': {
            'direction': 'long',
            'indicators': [{'id': 'trend', 'type': 'sma', 'period': '$period'}],
            'entry': {'all': [{'left': 'close', 'op': '>', 'right': 'trend'}]},
            'exit': {'any': [{'left': 'close', 'op': '<', 'right': 'trend'}]},
            'position_size_pct': 100,
        },
    },
    {
        'id': 'rsi_mean_reversion',
        'name': 'RSI mean reversion',
        'description': 'Buy oversold dips (RSI below a threshold) in an uptrend; sell when RSI recovers. Uses a stop-loss.',
        'params': {'rsi_period': 14, 'oversold': 30, 'exit_level': 55, 'trend': 200, 'stop': 8},
        'definition': {
            'direction': 'long',
            'indicators': [
                {'id': 'rsi', 'type': 'rsi', 'period': '$rsi_period'},
                {'id': 'trend', 'type': 'sma', 'period': '$trend'},
            ],
            'entry': {
                'all': [
                    {'left': 'rsi', 'op': '<', 'right': '$oversold'},
                    {'left': 'close', 'op': '>', 'right': 'trend'},
                ]
            },
            'exit': {'any': [{'left': 'rsi', 'op': '>', 'right': '$exit_level'}]},
            'stop_loss_pct': '$stop',
            'position_size_pct': 100,
        },
    },
    {
        'id': 'bollinger_breakout',
        'name': 'Bollinger breakout',
        'description': 'Buy when the close breaks above the upper Bollinger band; exit below the middle band.',
        'params': {'period': 20, 'std': 2},
        'definition': {
            'direction': 'long',
            'indicators': [{'id': 'bb', 'type': 'bbands', 'period': '$period', 'std': '$std'}],
            'entry': {'all': [{'left': 'close', 'op': 'crosses_above', 'right': 'bb.upper'}]},
            'exit': {'any': [{'left': 'close', 'op': 'crosses_below', 'right': 'bb.middle'}]},
            'position_size_pct': 100,
        },
    },
    {
        'id': 'momentum',
        'name': 'Momentum',
        'description': 'Hold while the rate of change over the lookback is positive and price is above its trend.',
        'params': {'lookback': 126, 'trend': 200},
        'definition': {
            'direction': 'long',
            'indicators': [
                {'id': 'mom', 'type': 'roc', 'period': '$lookback'},
                {'id': 'trend', 'type': 'sma', 'period': '$trend'},
            ],
            'entry': {
                'all': [
                    {'left': 'mom', 'op': '>', 'right': 0},
                    {'left': 'close', 'op': '>', 'right': 'trend'},
                ]
            },
            'exit': {
                'any': [
                    {'left': 'mom', 'op': '<', 'right': 0},
                    {'left': 'close', 'op': '<', 'right': 'trend'},
                ]
            },
            'position_size_pct': 100,
        },
    },
    {
        'id': 'macd_signal',
        'name': 'MACD signal cross',
        'description': 'Buy when MACD crosses above its signal line; sell on the cross back down.',
        'params': {'fast': 12, 'slow': 26, 'signal': 9},
        'definition': {
            'direction': 'long',
            'indicators': [{'id': 'm', 'type': 'macd', 'fast': '$fast', 'slow': '$slow', 'signal': '$signal'}],
            'entry': {'all': [{'left': 'm.macd', 'op': 'crosses_above', 'right': 'm.signal'}]},
            'exit': {'any': [{'left': 'm.macd', 'op': 'crosses_below', 'right': 'm.signal'}]},
            'position_size_pct': 100,
        },
    },
]

for t in STARTER_TEMPLATES:
    t.setdefault('kind', 'template')
    t.setdefault('timeframe', '1Day')
    t.setdefault('universe', ['SPY'])

PYTHON_STARTER = '''# Return boolean Series (entries, exits) indexed like df.
# Signals are read at each bar's close and filled at the NEXT bar's open.
# Never use future data (e.g. df.shift(-1)); that is look-ahead bias.
# Available: pd (pandas), np (numpy), ta (sma, ema, rsi, bbands, macd, atr, roc, highest, lowest).
# Optionally return a third value: {"stop_loss": 0.05, "take_profit": 0.1, "size": 1.0}


def signals(df, params):
    fast = ta.ema(df["close"], int(params.get("fast", 20)))
    slow = ta.ema(df["close"], int(params.get("slow", 50)))
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return entries, exits
'''
