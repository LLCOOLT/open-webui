"""Alpaca adapter (stocks). Uses the REST API directly via httpx, no SDK dependency.

Docs: https://docs.alpaca.markets/reference
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote as url_quote

import httpx

from open_webui.utils.finance.base import (
    Account,
    AssetClass,
    Bar,
    BrokerProvider,
    FinanceProviderError,
    NewsItem,
    NotSupportedError,
    Order,
    Position,
    Quote,
    SymbolInfo,
    parse_iso_ts,
    to_float,
    to_iso,
)

log = logging.getLogger(__name__)

PAPER_TRADING_URL = 'https://paper-api.alpaca.markets'
LIVE_TRADING_URL = 'https://api.alpaca.markets'
DATA_URL = 'https://data.alpaca.markets'

MAX_BARS = 10000
ASSET_CACHE_TTL = 24 * 3600

# Asset list is large (~12k rows) and identical for every account; cache per trading host.
_asset_cache: dict[str, tuple[float, list[dict]]] = {}
_asset_lock = asyncio.Lock()


def parse_bar(raw: dict) -> Bar:
    return Bar(
        t=parse_iso_ts(raw['t']),
        o=float(raw['o']),
        h=float(raw['h']),
        l=float(raw['l']),
        c=float(raw['c']),
        v=to_float(raw.get('v')),
        vw=to_float(raw.get('vw')),
    )


def parse_snapshot(symbol: str, raw: dict) -> Quote:
    quote = raw.get('latestQuote') or {}
    trade = raw.get('latestTrade') or {}
    last = to_float(trade.get('p'))
    if last is None:
        last = to_float((raw.get('minuteBar') or raw.get('dailyBar') or {}).get('c'))
    return Quote(
        symbol=symbol,
        bid=to_float(quote.get('bp')),
        ask=to_float(quote.get('ap')),
        bid_size=to_float(quote.get('bs')),
        ask_size=to_float(quote.get('as')),
        last=last,
        timestamp=trade.get('t') or quote.get('t'),
    )


def parse_position(raw: dict) -> Position:
    return Position(
        symbol=raw.get('symbol', ''),
        qty=to_float(raw.get('qty')) or 0.0,
        side=raw.get('side'),
        avg_entry_price=to_float(raw.get('avg_entry_price')),
        market_value=to_float(raw.get('market_value')),
        cost_basis=to_float(raw.get('cost_basis')),
        unrealized_pl=to_float(raw.get('unrealized_pl')),
        unrealized_plpc=to_float(raw.get('unrealized_plpc')),
        current_price=to_float(raw.get('current_price')),
        asset_class=raw.get('asset_class') or 'us_equity',
    )


def parse_order(raw: dict) -> Order:
    return Order(
        id=raw.get('id', ''),
        symbol=raw.get('symbol'),
        side=raw.get('side'),
        type=raw.get('type') or raw.get('order_type'),
        qty=to_float(raw.get('qty')),
        filled_qty=to_float(raw.get('filled_qty')),
        limit_price=to_float(raw.get('limit_price')),
        stop_price=to_float(raw.get('stop_price')),
        status=raw.get('status'),
        submitted_at=raw.get('submitted_at'),
        filled_avg_price=to_float(raw.get('filled_avg_price')),
        asset_class=raw.get('asset_class') or 'us_equity',
    )


def parse_news(raw: dict) -> NewsItem:
    return NewsItem(
        id=str(raw.get('id', '')),
        headline=raw.get('headline', ''),
        summary=raw.get('summary') or None,
        source=raw.get('source'),
        url=raw.get('url'),
        symbols=raw.get('symbols') or [],
        created_at=raw.get('created_at'),
    )


def rank_assets(assets: list[dict], query: str, limit: int) -> list[SymbolInfo]:
    q = query.strip().upper()
    if not q:
        return []
    scored = []
    for a in assets:
        symbol = (a.get('symbol') or '').upper()
        name = (a.get('name') or '').upper()
        if symbol == q:
            score = 0
        elif symbol.startswith(q):
            score = 1
        elif name.startswith(q):
            score = 2
        elif q in name:
            score = 3
        else:
            continue
        scored.append((score, len(symbol), symbol, a))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [
        SymbolInfo(
            symbol=a.get('symbol', ''),
            name=a.get('name'),
            exchange=a.get('exchange'),
            asset_class=a.get('class') or 'us_equity',
            tradable=bool(a.get('tradable', True)),
        )
        for *_, a in scored[:limit]
    ]


class AlpacaProvider(BrokerProvider):
    id = 'alpaca'
    asset_classes = ('us_equity',)

    def __init__(self, api_key: str, api_secret: str, is_paper: bool = True, feed: str = 'iex', timeout: float = 20):
        if not api_key or not api_secret:
            raise FinanceProviderError('Alpaca connection is missing its API key or secret', status_code=400)
        self.is_paper = is_paper
        self.feed = feed or 'iex'
        self.trading_url = PAPER_TRADING_URL if is_paper else LIVE_TRADING_URL
        self._headers = {'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret}
        self._timeout = timeout

    @property
    def cache_key(self) -> str:
        # Bars differ by feed (IEX vs SIP), so cache them separately.
        return f'alpaca:{self.feed}'

    async def _get(self, base: str, path: str, params: Optional[dict] = None):
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=True) as client:
                response = await client.get(f'{base}{path}', params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise FinanceProviderError(f'Alpaca request failed: {type(e).__name__}') from e

        if response.status_code in (401, 403):
            raise FinanceProviderError('Alpaca rejected the API credentials', status_code=400)
        if response.status_code == 404:
            raise FinanceProviderError('Not found at Alpaca', status_code=404)
        if response.status_code == 429:
            raise FinanceProviderError('Alpaca rate limit reached, try again shortly', status_code=429)
        if response.status_code >= 400:
            message = ''
            try:
                message = response.json().get('message', '')
            except ValueError:
                pass
            raise FinanceProviderError(f'Alpaca error {response.status_code}: {message}'.strip(': '))
        return response.json()

    def _require_equity(self, asset_class: AssetClass) -> None:
        if asset_class != 'us_equity':
            raise NotSupportedError(f'Asset class {asset_class!r} is not enabled yet')

    # ---- market data ----

    async def bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        asset_class: AssetClass = 'us_equity',
    ) -> list[Bar]:
        self._require_equity(asset_class)
        params = {
            'timeframe': timeframe,
            'start': to_iso(start),
            'end': to_iso(end),
            'limit': MAX_BARS,
            'adjustment': 'all',
            'feed': self.feed,
        }
        bars: list[Bar] = []
        while True:
            data = await self._get(DATA_URL, f'/v2/stocks/{url_quote(symbol, safe="")}/bars', params)
            bars.extend(parse_bar(b) for b in data.get('bars') or [])
            token = data.get('next_page_token')
            if not token or len(bars) >= MAX_BARS:
                break
            params['page_token'] = token
        return bars[:MAX_BARS]

    async def quote(self, symbol: str, asset_class: AssetClass = 'us_equity') -> Quote:
        self._require_equity(asset_class)
        data = await self._get(DATA_URL, f'/v2/stocks/{url_quote(symbol, safe="")}/snapshot', {'feed': self.feed})
        return parse_snapshot(symbol, data)

    async def _assets(self) -> list[dict]:
        cached = _asset_cache.get(self.trading_url)
        if cached and time.time() - cached[0] < ASSET_CACHE_TTL:
            return cached[1]
        async with _asset_lock:
            cached = _asset_cache.get(self.trading_url)
            if cached and time.time() - cached[0] < ASSET_CACHE_TTL:
                return cached[1]
            assets = await self._get(
                self.trading_url, '/v2/assets', {'status': 'active', 'asset_class': 'us_equity'}
            )
            slim = [
                {k: a.get(k) for k in ('symbol', 'name', 'exchange', 'class', 'tradable')}
                for a in assets
                if isinstance(a, dict)
            ]
            _asset_cache[self.trading_url] = (time.time(), slim)
            return slim

    async def search(self, query: str, limit: int = 10, asset_class: AssetClass = 'us_equity') -> list[SymbolInfo]:
        self._require_equity(asset_class)
        return rank_assets(await self._assets(), query, limit)

    async def news(self, symbols: list[str], limit: int = 10) -> list[NewsItem]:
        params = {'limit': max(1, min(limit, 50)), 'sort': 'desc'}
        if symbols:
            params['symbols'] = ','.join(symbols)
        data = await self._get(DATA_URL, '/v1beta1/news', params)
        return [parse_news(n) for n in data.get('news') or []]

    # ---- account ----

    async def account(self) -> Account:
        raw = await self._get(self.trading_url, '/v2/account')
        return Account(
            status=raw.get('status'),
            equity=to_float(raw.get('equity')),
            cash=to_float(raw.get('cash')),
            buying_power=to_float(raw.get('buying_power')),
            portfolio_value=to_float(raw.get('portfolio_value')),
            currency=raw.get('currency'),
            is_paper=self.is_paper,
        )

    async def positions(self) -> list[Position]:
        return [parse_position(p) for p in await self._get(self.trading_url, '/v2/positions')]

    async def orders(self, status: str = 'open', limit: int = 50) -> list[Order]:
        if status not in ('open', 'closed', 'all'):
            raise FinanceProviderError('status must be open, closed or all', status_code=400)
        raw = await self._get(
            self.trading_url, '/v2/orders', {'status': status, 'limit': max(1, min(limit, 500)), 'nested': 'false'}
        )
        return [parse_order(o) for o in raw]
