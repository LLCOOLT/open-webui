import ast
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel

from open_webui.utils.finance.backtest.engine import BacktestError
from open_webui.utils.finance.backtest.template import TemplateError, template_signals


class StrategyValidateForm(BaseModel):
    kind: Literal['template', 'python'] = 'template'
    definition: Optional[dict] = None
    source: Optional[str] = None
    params: Optional[dict[str, float]] = None


def _sample_frame(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {'open': close, 'high': close * 1.01, 'low': close * 0.99, 'close': close, 'volume': 1e6},
        index=np.arange(n) * 86400,
    )


def validate_strategy(
    kind: str, definition: Optional[dict], source: Optional[str], params: dict, compile_source: bool = True
) -> None:
    """Check a strategy without running user code in the server process.

    Templates are fully evaluated on synthetic data (safe: no code). Python source is
    only parsed; it first executes inside the backtest worker."""
    if kind == 'template':
        if not definition:
            raise BacktestError('Template strategies need a definition')
        try:
            template_signals(definition, _sample_frame(), params)
        except TemplateError as e:
            raise BacktestError(str(e)) from e
    elif kind == 'python':
        if not source or not source.strip():
            raise BacktestError('Python strategies need source code')
        if compile_source:
            try:
                tree = ast.parse(source)
            except SyntaxError as e:
                raise BacktestError(f'Syntax error on line {e.lineno}: {e.msg}') from e
            if not any(isinstance(n, ast.FunctionDef) and n.name == 'signals' for n in tree.body):
                raise BacktestError('Python strategies must define signals(df, params)')
    else:
        raise BacktestError(f'Unknown strategy kind: {kind}')
