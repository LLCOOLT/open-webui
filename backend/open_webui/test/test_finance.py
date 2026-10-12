import time
from datetime import datetime, timezone

import pytest

from open_webui.utils.finance import alpaca, edgar, service
from open_webui.utils.finance.base import Bar, FinanceProviderError, MarketDataProvider, normalize_symbol


def test_parse_bar_and_snapshot():
    bar = alpaca.parse_bar({'t': '2024-01-02T05:00:00Z', 'o': 1, 'h': 2, 'l': 0.5, 'c': 1.5, 'v': 100, 'vw': 1.2})
    assert bar.t == int(datetime(2024, 1, 2, 5, tzinfo=timezone.utc).timestamp())
    assert (bar.o, bar.h, bar.l, bar.c, bar.v, bar.vw) == (1, 2, 0.5, 1.5, 100, 1.2)

    quote = alpaca.parse_snapshot(
        'AAPL',
        {'latestQuote': {'bp': 10, 'ap': 10.1, 'bs': 1, 'as': 2}, 'latestTrade': {'p': 10.05, 't': 'x'}},
    )
    assert quote.bid == 10 and quote.ask == 10.1 and quote.last == 10.05 and quote.timestamp == 'x'

    # Falls back to the latest bar close when there is no trade.
    assert alpaca.parse_snapshot('AAPL', {'dailyBar': {'c': 9}}).last == 9


def test_parse_position_and_order_strings():
    pos = alpaca.parse_position({'symbol': 'SPY', 'qty': '3', 'market_value': '1500.5', 'unrealized_plpc': ''})
    assert pos.qty == 3 and pos.market_value == 1500.5 and pos.unrealized_plpc is None
    order = alpaca.parse_order({'id': 'o1', 'symbol': 'SPY', 'qty': '1', 'order_type': 'limit', 'limit_price': '400'})
    assert order.type == 'limit' and order.limit_price == 400


def test_rank_assets_prefers_exact_then_prefix():
    assets = [
        {'symbol': 'AAPLW', 'name': 'Something'},
        {'symbol': 'AAPL', 'name': 'Apple Inc.'},
        {'symbol': 'XYZ', 'name': 'Pineapple Co'},
        {'symbol': 'MSFT', 'name': 'Microsoft'},
    ]
    result = [a.symbol for a in alpaca.rank_assets(assets, 'aapl', 10)]
    assert result == ['AAPL', 'AAPLW']
    assert [a.symbol for a in alpaca.rank_assets(assets, 'apple', 10)] == ['AAPL', 'XYZ']


def test_normalize_symbol():
    assert normalize_symbol(' brk.b ') == 'BRK.B'
    for bad in ['', 'A B', '../x', 'X' * 30]:
        with pytest.raises(FinanceProviderError):
            normalize_symbol(bad)


def test_alpaca_requires_credentials_and_picks_host():
    with pytest.raises(FinanceProviderError):
        alpaca.AlpacaProvider('', 'secret')
    assert alpaca.AlpacaProvider('k', 's').trading_url == alpaca.PAPER_TRADING_URL
    assert alpaca.AlpacaProvider('k', 's', is_paper=False).trading_url == alpaca.LIVE_TRADING_URL


def test_edgar_requires_email_user_agent():
    with pytest.raises(FinanceProviderError):
        edgar.EdgarProvider('no email here')
    edgar.EdgarProvider('Jane Doe jane@example.com')


def test_edgar_annual_series_uses_latest_restatement():
    facts = {
        'us-gaap': {
            'Revenues': {
                'units': {
                    'USD': [
                        {'end': '2023-12-31', 'val': 100, 'form': '10-K', 'fp': 'FY', 'fy': 2023, 'filed': '2024-02-01'},
                        {'end': '2023-12-31', 'val': 110, 'form': '10-K/A', 'fp': 'FY', 'fy': 2023, 'filed': '2024-05-01'},
                        {'end': '2022-12-31', 'val': 90, 'form': '10-K', 'fp': 'FY', 'fy': 2022, 'filed': '2023-02-01'},
                        {'end': '2024-03-31', 'val': 30, 'form': '10-Q', 'fp': 'Q1', 'fy': 2024, 'filed': '2024-04-01'},
                    ]
                }
            }
        }
    }
    result = edgar.parse_fundamentals('X', {'facts': facts, 'entityName': 'X Corp', 'cik': 1})
    assert result['metrics']['revenue'] == [
        {'period_end': '2023-12-31', 'fiscal_year': 2023, 'value': 110},
        {'period_end': '2022-12-31', 'fiscal_year': 2022, 'value': 90},
    ]
    assert result['metrics']['net_income'] == []


def test_edgar_parse_filings_filters_forms():
    raw = {
        'filings': {
            'recent': {
                'form': ['8-K', '10-Q', '10-K'],
                'filingDate': ['2024-03-01', '2024-02-01', '2024-01-01'],
                'accessionNumber': ['0001-24-000003', '0001-24-000002', '0001-24-000001'],
                'primaryDocument': ['a.htm', 'b.htm', 'c.htm'],
            }
        }
    }
    filings = edgar.parse_filings('0000320193', raw, ['10-k', '10-Q'], 10)
    assert [f['form'] for f in filings] == ['10-Q', '10-K']
    assert filings[1]['url'] == 'https://www.sec.gov/Archives/edgar/data/320193/000124000001/c.htm'


class _Conn:
    def __init__(self, provider, kind, asset_classes=('us_equity',)):
        self.provider, self.kind, self.asset_classes = provider, kind, list(asset_classes)


def test_validate_connection_form():
    service.validate_connection_form(_Conn('alpaca', 'broker'))
    service.validate_connection_form(_Conn('edgar', 'data'))
    for bad in [_Conn('nope', 'broker'), _Conn('edgar', 'broker'), _Conn('alpaca', 'broker', ['crypto'])]:
        with pytest.raises(FinanceProviderError):
            service.validate_connection_form(bad)


class _FakeProvider(MarketDataProvider):
    id = 'fake'

    def __init__(self, bars):
        self._bars = bars
        self.calls = 0

    async def bars(self, symbol, timeframe, start, end, asset_class='us_equity'):
        self.calls += 1
        return [b for b in self._bars if start.timestamp() <= b.t <= end.timestamp()]


class _FakeCache:
    def __init__(self):
        self.rows = {}

    async def get_range(self, provider, symbol, timeframe, start, end):
        return sorted(
            (b for (p, s, tf, t), b in self.rows.items() if (p, s, tf) == (provider, symbol, timeframe) and start <= t <= end),
            key=lambda b: b['t'],
        )

    async def upsert_many(self, provider, symbol, timeframe, bars):
        for b in bars:
            self.rows[(provider, symbol, timeframe, b['t'])] = b


@pytest.mark.asyncio
async def test_get_bars_read_through_cache(monkeypatch):
    cache = _FakeCache()
    monkeypatch.setattr(service, 'FinanceBars', cache)

    day = 86400
    base = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    bars = [Bar(t=base + i * day, o=1, h=1, l=1, c=1 + i) for i in range(30)]
    provider = _FakeProvider(bars)

    first = await service.get_bars(provider, 'spy', '1Day', '2024-01-01', '2024-01-30')
    assert provider.calls == 1 and len(first) == 30

    second = await service.get_bars(provider, 'SPY', '1Day', '2024-01-01', '2024-01-30', limit=5)
    assert provider.calls == 1  # served from cache
    assert [b.c for b in second] == [26, 27, 28, 29, 30]

    # A range the cache does not cover goes back to the provider.
    await service.get_bars(provider, 'SPY', '1Day', '2023-06-01', '2024-01-30')
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_get_bars_does_not_cache_forming_bar(monkeypatch):
    cache = _FakeCache()
    monkeypatch.setattr(service, 'FinanceBars', cache)
    now = int(time.time())
    provider = _FakeProvider([Bar(t=now - 3 * 86400, o=1, h=1, l=1, c=1), Bar(t=now - 60, o=1, h=1, l=1, c=2)])

    result = await service.get_bars(provider, 'SPY', '1Day')
    assert len(result) == 2
    assert [b['c'] for b in cache.rows.values()] == [1]


@pytest.mark.asyncio
async def test_get_bars_validates_input():
    provider = _FakeProvider([])
    with pytest.raises(FinanceProviderError):
        await service.get_bars(provider, 'SPY', '2Day')
    with pytest.raises(FinanceProviderError):
        await service.get_bars(provider, 'SPY', '1Day', '2024-02-01', '2024-01-01')
