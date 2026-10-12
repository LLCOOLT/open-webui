"""Provider protocols and shared types for the Markets (finance) feature.

Phase 1 implements US equities only. Options and crypto have reserved hooks
(``asset_class`` on every call, multi-leg orders, ``option_chain``) so new
adapters can be added without changing callers or the database schema.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

AssetClass = Literal['us_equity', 'option', 'crypto']
Timeframe = Literal['1Min', '5Min', '15Min', '1Hour', '1Day', '1Week']
TIMEFRAMES: tuple[str, ...] = ('1Min', '5Min', '15Min', '1Hour', '1Day', '1Week')

# Default lookback when no start date is given.
DEFAULT_LOOKBACK: dict[str, timedelta] = {
    '1Min': timedelta(days=2),
    '5Min': timedelta(days=7),
    '15Min': timedelta(days=21),
    '1Hour': timedelta(days=60),
    '1Day': timedelta(days=365),
    '1Week': timedelta(days=365 * 5),
}

# Max allowed gap between the requested range edge and the cached data edge
# (weekends and holidays mean the first/last bar is rarely on the exact bound).
CACHE_EDGE_TOLERANCE: dict[str, int] = {
    '1Min': 4 * 86400,
    '5Min': 4 * 86400,
    '15Min': 4 * 86400,
    '1Hour': 4 * 86400,
    '1Day': 5 * 86400,
    '1Week': 10 * 86400,
}

TIMEFRAME_SECONDS: dict[str, int] = {
    '1Min': 60,
    '5Min': 300,
    '15Min': 900,
    '1Hour': 3600,
    '1Day': 86400,
    '1Week': 7 * 86400,
}


class FinanceProviderError(Exception):
    """Raised for provider failures; the message is safe to show to the user (never contains secrets)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class NotSupportedError(FinanceProviderError):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class Bar(BaseModel):
    t: int  # unix seconds, bar open time
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: Optional[float] = None
    vw: Optional[float] = None


class Quote(BaseModel):
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    timestamp: Optional[str] = None


class SymbolInfo(BaseModel):
    symbol: str
    name: Optional[str] = None
    exchange: Optional[str] = None
    asset_class: str = 'us_equity'
    tradable: bool = True


class Account(BaseModel):
    status: Optional[str] = None
    equity: Optional[float] = None
    cash: Optional[float] = None
    buying_power: Optional[float] = None
    portfolio_value: Optional[float] = None
    currency: Optional[str] = None
    is_paper: bool = True


class Position(BaseModel):
    symbol: str
    qty: float
    side: Optional[str] = None
    avg_entry_price: Optional[float] = None
    market_value: Optional[float] = None
    cost_basis: Optional[float] = None
    unrealized_pl: Optional[float] = None
    unrealized_plpc: Optional[float] = None
    current_price: Optional[float] = None
    asset_class: str = 'us_equity'


class OrderLeg(BaseModel):
    """One leg of an order. Stocks use a single leg; options spreads will use several."""

    symbol: str
    side: Literal['buy', 'sell']
    ratio_qty: float = 1
    asset_class: AssetClass = 'us_equity'


class OrderIntent(BaseModel):
    """Order request shape for phase 3 (paper execution). Defined now so the
    broker protocol and the risk gate are written against a stable contract."""

    legs: list[OrderLeg] = Field(min_length=1)
    qty: Optional[float] = None
    notional: Optional[float] = None
    type: Literal['market', 'limit', 'stop', 'stop_limit'] = 'market'
    time_in_force: Literal['day', 'gtc', 'ioc', 'fok'] = 'day'
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    client_order_id: Optional[str] = None


class Order(BaseModel):
    id: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    type: Optional[str] = None
    qty: Optional[float] = None
    filled_qty: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: Optional[str] = None
    submitted_at: Optional[str] = None
    filled_avg_price: Optional[float] = None
    asset_class: str = 'us_equity'


class NewsItem(BaseModel):
    id: str
    headline: str
    summary: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    symbols: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None


def to_float(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_iso_ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_date_input(value: Optional[str]) -> Optional[datetime]:
    """Accept 'YYYY-MM-DD', a full ISO timestamp, or unix seconds."""
    if value is None or value == '':
        return None
    value = str(value).strip()
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise FinanceProviderError(f'Invalid date: {value}', status_code=400) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_symbol(symbol: str) -> str:
    symbol = (symbol or '').strip().upper()
    if not symbol or len(symbol) > 21 or not all(ch.isalnum() or ch in '.-/' for ch in symbol):
        raise FinanceProviderError(f'Invalid symbol: {symbol!r}', status_code=400)
    return symbol


class MarketDataProvider:
    """Read-only market data. Adapters override what they support."""

    id: str = ''
    asset_classes: tuple[str, ...] = ('us_equity',)

    @property
    def cache_key(self) -> str:
        return self.id

    async def bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        asset_class: AssetClass = 'us_equity',
    ) -> list[Bar]:
        raise NotSupportedError(f'{self.id} does not provide bars')

    async def quote(self, symbol: str, asset_class: AssetClass = 'us_equity') -> Quote:
        raise NotSupportedError(f'{self.id} does not provide quotes')

    async def search(self, query: str, limit: int = 10, asset_class: AssetClass = 'us_equity') -> list[SymbolInfo]:
        raise NotSupportedError(f'{self.id} does not provide symbol search')

    async def news(self, symbols: list[str], limit: int = 10) -> list[NewsItem]:
        raise NotSupportedError(f'{self.id} does not provide news')

    async def option_chain(self, underlying: str, expiration: Optional[str] = None) -> list[dict]:
        # Hook for options support (deferred).
        raise NotSupportedError('Options are not supported yet')


class BrokerProvider(MarketDataProvider):
    """Account access. Order submission (`submit_order`, `cancel_order`) arrives in phase 3."""

    is_paper: bool = True

    async def account(self) -> Account:
        raise NotSupportedError(f'{self.id} does not provide account data')

    async def positions(self) -> list[Position]:
        raise NotSupportedError(f'{self.id} does not provide positions')

    async def orders(self, status: str = 'open', limit: int = 50) -> list[Order]:
        raise NotSupportedError(f'{self.id} does not provide orders')

    async def submit_order(self, intent: OrderIntent) -> Order:
        raise NotSupportedError('Order placement is not enabled yet')

    async def cancel_order(self, order_id: str) -> bool:
        raise NotSupportedError('Order cancellation is not enabled yet')
