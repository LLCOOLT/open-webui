"""Constrained strategy templates: a JSON definition compiled into entry/exit signals.

This is the safe authoring path for model-generated strategies: no code runs,
only a fixed set of indicators and comparisons.
"""

from typing import Any, Literal, Optional, Union

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from open_webui.utils.finance.backtest import indicators as ind

PRICE_SERIES = ('open', 'high', 'low', 'close', 'volume')
INDICATOR_TYPES = ('sma', 'ema', 'rsi', 'bbands', 'macd', 'atr', 'roc', 'highest', 'lowest')
MULTI_OUTPUTS = {'bbands': ('upper', 'middle', 'lower'), 'macd': ('macd', 'signal', 'hist')}
OPS = ('>', '<', '>=', '<=', 'crosses_above', 'crosses_below')

Number = Union[int, float, str]  # a str must be "$param"


class TemplateError(ValueError):
    pass


class Indicator(BaseModel):
    id: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,40}$')
    type: Literal['sma', 'ema', 'rsi', 'bbands', 'macd', 'atr', 'roc', 'highest', 'lowest']
    source: Literal['open', 'high', 'low', 'close', 'volume'] = 'close'
    period: Optional[Number] = None
    std: Optional[Number] = None
    fast: Optional[Number] = None
    slow: Optional[Number] = None
    signal: Optional[Number] = None


class Condition(BaseModel):
    left: str
    op: Literal['>', '<', '>=', '<=', 'crosses_above', 'crosses_below']
    right: Union[str, int, float]


class ConditionGroup(BaseModel):
    all: Optional[list[Union[Condition, 'ConditionGroup']]] = None
    any: Optional[list[Union[Condition, 'ConditionGroup']]] = None

    @model_validator(mode='after')
    def one_of(self):
        if (self.all is None) == (self.any is None):
            raise ValueError('a condition group needs exactly one of "all" or "any"')
        if not (self.all or self.any):
            raise ValueError('a condition group cannot be empty')
        return self


class TemplateDefinition(BaseModel):
    direction: Literal['long'] = 'long'  # short selling arrives with live execution
    indicators: list[Indicator] = Field(default_factory=list, max_length=30)
    entry: ConditionGroup
    exit: Optional[ConditionGroup] = None
    stop_loss_pct: Optional[Number] = None
    take_profit_pct: Optional[Number] = None
    position_size_pct: Number = 100

    @field_validator('indicators')
    @classmethod
    def unique_ids(cls, v):
        ids = [i.id for i in v]
        if len(ids) != len(set(ids)):
            raise ValueError('indicator ids must be unique')
        if any(i in PRICE_SERIES for i in ids):
            raise ValueError(f'indicator ids cannot shadow price series {PRICE_SERIES}')
        return v


def resolve(value: Any, params: dict, name: str = 'value') -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.startswith('$'):
            raise TemplateError(f'{name}: expected a number or "$param", got {value!r}')
        key = value[1:]
        if key not in params:
            raise TemplateError(f'{name}: unknown parameter {key!r}')
        value = params[key]
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise TemplateError(f'{name}: {value!r} is not a number') from e


def _period(value, params, name, default=None) -> int:
    v = resolve(value, params, name)
    if v is None:
        if default is None:
            raise TemplateError(f'{name} is required')
        v = default
    if v < 1 or v > 1000 or v != int(v):
        raise TemplateError(f'{name} must be a whole number between 1 and 1000')
    return int(v)


def parse_definition(definition: dict) -> TemplateDefinition:
    try:
        return TemplateDefinition.model_validate(definition)
    except Exception as e:
        raise TemplateError(str(e)) from e


def compute_series(defn: TemplateDefinition, df: pd.DataFrame, params: dict) -> dict[str, pd.Series]:
    series: dict[str, pd.Series] = {k: df[k] for k in PRICE_SERIES if k in df}
    for i in defn.indicators:
        src = df[i.source]
        name = f'indicator {i.id}'
        if i.type == 'sma':
            series[i.id] = ind.sma(src, _period(i.period, params, f'{name}.period'))
        elif i.type == 'ema':
            series[i.id] = ind.ema(src, _period(i.period, params, f'{name}.period'))
        elif i.type == 'rsi':
            series[i.id] = ind.rsi(src, _period(i.period, params, f'{name}.period', 14))
        elif i.type == 'roc':
            series[i.id] = ind.roc(src, _period(i.period, params, f'{name}.period'))
        elif i.type == 'highest':
            series[i.id] = ind.highest(src, _period(i.period, params, f'{name}.period'))
        elif i.type == 'lowest':
            series[i.id] = ind.lowest(src, _period(i.period, params, f'{name}.period'))
        elif i.type == 'atr':
            series[i.id] = ind.atr(df['high'], df['low'], df['close'], _period(i.period, params, f'{name}.period', 14))
        elif i.type == 'bbands':
            std = resolve(i.std, params, f'{name}.std') or 2.0
            for k, v in ind.bbands(src, _period(i.period, params, f'{name}.period', 20), std).items():
                series[f'{i.id}.{k}'] = v
        elif i.type == 'macd':
            out = ind.macd(
                src,
                _period(i.fast, params, f'{name}.fast', 12),
                _period(i.slow, params, f'{name}.slow', 26),
                _period(i.signal, params, f'{name}.signal', 9),
            )
            for k, v in out.items():
                series[f'{i.id}.{k}'] = v
    return series


def _operand(value, series: dict[str, pd.Series], params: dict, index) -> pd.Series:
    if isinstance(value, (int, float)):
        return pd.Series(float(value), index=index)
    if isinstance(value, str) and value.startswith('$'):
        return pd.Series(resolve(value, params), index=index)
    if value not in series:
        available = ', '.join(sorted(series))
        raise TemplateError(f'unknown series {value!r}; available: {available}')
    return series[value]


def _eval_condition(c: Condition, series, params, index) -> pd.Series:
    left = _operand(c.left, series, params, index)
    right = _operand(c.right, series, params, index)
    if c.op == '>':
        out = left > right
    elif c.op == '<':
        out = left < right
    elif c.op == '>=':
        out = left >= right
    elif c.op == '<=':
        out = left <= right
    elif c.op == 'crosses_above':
        out = (left > right) & (left.shift(1) <= right.shift(1))
    else:
        out = (left < right) & (left.shift(1) >= right.shift(1))
    # Comparisons against NaN (indicator warm-up) are False.
    return out.fillna(False).astype(bool)


def _eval_group(g: ConditionGroup, series, params, index) -> pd.Series:
    items = g.all if g.all is not None else g.any
    results = [
        _eval_group(i, series, params, index) if isinstance(i, ConditionGroup) else _eval_condition(i, series, params, index)
        for i in items
    ]
    out = results[0]
    for r in results[1:]:
        out = (out & r) if g.all is not None else (out | r)
    return out


def template_signals(definition: dict, df: pd.DataFrame, params: dict) -> tuple[pd.Series, pd.Series, dict]:
    """Returns (entries, exits, risk) where risk holds stop/take-profit/size as fractions."""
    defn = parse_definition(definition)
    series = compute_series(defn, df, params)
    entries = _eval_group(defn.entry, series, params, df.index)
    exits = (
        _eval_group(defn.exit, series, params, df.index)
        if defn.exit
        else pd.Series(False, index=df.index)
    )
    stop = resolve(defn.stop_loss_pct, params, 'stop_loss_pct')
    take = resolve(defn.take_profit_pct, params, 'take_profit_pct')
    size = resolve(defn.position_size_pct, params, 'position_size_pct')
    if size is None or not 0 < size <= 100:
        raise TemplateError('position_size_pct must be between 0 and 100')
    for label, v in (('stop_loss_pct', stop), ('take_profit_pct', take)):
        if v is not None and not 0 < v < 100:
            raise TemplateError(f'{label} must be between 0 and 100')
    risk = {
        'stop_loss': stop / 100 if stop else None,
        'take_profit': take / 100 if take else None,
        'size': size / 100,
    }
    return entries, exits, risk


ConditionGroup.model_rebuild()
