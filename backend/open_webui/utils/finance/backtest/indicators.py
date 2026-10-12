"""Technical indicators on pandas Series. Every value at bar i uses only bars <= i."""

import numpy as np
import pandas as pd


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # No losses in the window means RSI 100.
    return out.where(loss != 0, 100.0).where(gain.notna())


def bbands(s: pd.Series, period: int = 20, std: float = 2.0) -> dict[str, pd.Series]:
    middle = sma(s, period)
    dev = s.rolling(period, min_periods=period).std(ddof=0)
    return {'upper': middle + std * dev, 'middle': middle, 'lower': middle - std * dev}


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, pd.Series]:
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return {'macd': line, 'signal': sig, 'hist': line - sig}


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def roc(s: pd.Series, period: int) -> pd.Series:
    return s.pct_change(period) * 100


def highest(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).max()


def lowest(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).min()
