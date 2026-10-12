"""Glue between stored connections, provider adapters and the bar cache.

Routers and built-in tools call into this module; they never construct adapters directly.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from open_webui.models.finance import ENABLED_ASSET_CLASSES, FinanceBars, FinanceConnections
from open_webui.utils.finance.alpaca import AlpacaProvider
from open_webui.utils.finance.base import (
    CACHE_EDGE_TOLERANCE,
    DEFAULT_LOOKBACK,
    TIMEFRAME_SECONDS,
    TIMEFRAMES,
    Bar,
    BrokerProvider,
    FinanceProviderError,
    MarketDataProvider,
    normalize_symbol,
    parse_date_input,
)
from open_webui.utils.finance.edgar import EdgarProvider

log = logging.getLogger(__name__)

EDGAR_USER_AGENT = os.getenv('FINANCE_EDGAR_USER_AGENT', '')

PROVIDERS: list[dict] = [
    {
        'id': 'alpaca',
        'name': 'Alpaca',
        'kinds': ['broker'],
        'asset_classes': ['us_equity'],
        'supports_paper': True,
        'credential_fields': [
            {'name': 'api_key', 'label': 'API Key ID', 'secret': False, 'required': True},
            {'name': 'api_secret', 'label': 'API Secret Key', 'secret': True, 'required': True},
        ],
    },
    {
        'id': 'edgar',
        'name': 'SEC EDGAR',
        'kinds': ['data'],
        'asset_classes': ['us_equity'],
        'supports_paper': False,
        'credential_fields': [
            {'name': 'user_agent', 'label': 'User-Agent (name and email)', 'secret': False, 'required': True},
        ],
    },
]
PROVIDER_IDS = {p['id']: p for p in PROVIDERS}


def validate_connection_form(form) -> None:
    provider = PROVIDER_IDS.get(form.provider)
    if not provider:
        raise FinanceProviderError(f'Unknown provider: {form.provider}', status_code=400)
    if form.kind not in provider['kinds']:
        raise FinanceProviderError(f'{provider["name"]} cannot be used as a {form.kind} connection', status_code=400)
    disabled = [a for a in form.asset_classes if a not in ENABLED_ASSET_CLASSES]
    if disabled:
        raise FinanceProviderError(f'Asset classes not enabled yet: {", ".join(disabled)}', status_code=400)


def build_provider(connection, credentials: dict) -> MarketDataProvider:
    meta = connection.meta or {}
    if connection.provider == 'alpaca':
        return AlpacaProvider(
            api_key=credentials.get('api_key', ''),
            api_secret=credentials.get('api_secret', ''),
            is_paper=connection.is_paper,
            feed=meta.get('feed', 'iex'),
        )
    if connection.provider == 'edgar':
        return EdgarProvider(user_agent=credentials.get('user_agent') or EDGAR_USER_AGENT)
    raise FinanceProviderError(f'Unknown provider: {connection.provider}', status_code=400)


async def get_user_connection(user_id: str, connection_id: str):
    connection = await FinanceConnections.get_by_id(connection_id)
    if not connection or connection.user_id != user_id:
        raise FinanceProviderError('Connection not found', status_code=404)
    return connection


async def get_broker(user_id: str, connection_id: Optional[str] = None) -> BrokerProvider:
    if connection_id:
        connection = await get_user_connection(user_id, connection_id)
        if connection.kind != 'broker':
            raise FinanceProviderError('That connection is not a broker connection', status_code=400)
    else:
        connection = await FinanceConnections.get_default(user_id, kind='broker')
        if not connection:
            raise FinanceProviderError(
                'No broker connection configured. Add an Alpaca connection under Markets.', status_code=400
            )
    credentials = await FinanceConnections.get_credentials_by_id(connection.id)
    return build_provider(connection, credentials)


async def get_edgar(user_id: str) -> EdgarProvider:
    connection = await FinanceConnections.get_default(user_id, kind='data', provider='edgar')
    if connection:
        credentials = await FinanceConnections.get_credentials_by_id(connection.id)
        return build_provider(connection, credentials)
    if EDGAR_USER_AGENT:
        return EdgarProvider(user_agent=EDGAR_USER_AGENT)
    raise FinanceProviderError(
        'SEC EDGAR is not configured. Add a SEC EDGAR connection under Markets with your name and email.',
        status_code=400,
    )


async def test_connection(connection, credentials: dict) -> dict:
    provider = build_provider(connection, credentials)
    if isinstance(provider, BrokerProvider):
        account = await provider.account()
        return {'ok': True, 'account': account.model_dump()}
    if isinstance(provider, EdgarProvider):
        await provider.cik_for('AAPL')
        return {'ok': True}
    return {'ok': True}


def _cache_covers(cached: list[dict], start: int, end: int, timeframe: str) -> bool:
    if not cached:
        return False
    tolerance = CACHE_EDGE_TOLERANCE[timeframe]
    return cached[0]['t'] - start <= tolerance and end - cached[-1]['t'] <= tolerance


async def get_bars(
    provider: MarketDataProvider,
    symbol: str,
    timeframe: str = '1Day',
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Bar]:
    """Bars with a read-through cache. Only completed bars are cached; the
    current, still-forming bar is always fetched live."""
    symbol = normalize_symbol(symbol)
    if timeframe not in TIMEFRAMES:
        raise FinanceProviderError(f'timeframe must be one of {", ".join(TIMEFRAMES)}', status_code=400)

    now = datetime.now(timezone.utc)
    end_dt = min(parse_date_input(end) or now, now)
    start_dt = parse_date_input(start) or (end_dt - DEFAULT_LOOKBACK[timeframe])
    if start_dt >= end_dt:
        raise FinanceProviderError('start must be before end', status_code=400)

    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    # A bar is complete once its whole interval has passed.
    completed_before = int(time.time()) - TIMEFRAME_SECONDS[timeframe]
    range_is_historical = end_ts <= completed_before

    bars: Optional[list[Bar]] = None
    if range_is_historical:
        try:
            cached = await FinanceBars.get_range(provider.cache_key, symbol, timeframe, start_ts, end_ts)
            if _cache_covers(cached, start_ts, end_ts, timeframe):
                bars = [Bar(**b) for b in cached]
        except Exception as e:  # cache is an optimisation; never fail the request on it
            log.warning('finance bar cache read failed: %s', e)

    if bars is None:
        bars = await provider.bars(symbol, timeframe, start_dt, end_dt)
        complete = [b.model_dump() for b in bars if b.t <= completed_before]
        try:
            await FinanceBars.upsert_many(provider.cache_key, symbol, timeframe, complete)
        except Exception as e:
            log.warning('finance bar cache write failed: %s', e)

    if limit:
        bars = bars[-limit:]
    return bars
