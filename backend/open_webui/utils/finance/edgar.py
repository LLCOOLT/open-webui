"""SEC EDGAR adapter: free fundamentals (XBRL company facts) and filings.

SEC requires a descriptive User-Agent with a contact email and allows ~10 req/s.
Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from open_webui.utils.finance.base import FinanceProviderError, MarketDataProvider, SymbolInfo

log = logging.getLogger(__name__)

TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'
SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik}.json'
FACTS_URL = 'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
ARCHIVE_URL = 'https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}'

TICKER_CACHE_TTL = 24 * 3600
_ticker_cache: Optional[tuple[float, dict[str, dict]]] = None
_ticker_lock = asyncio.Lock()

# (output name, candidate us-gaap / dei concepts in priority order, unit)
FUNDAMENTAL_CONCEPTS: list[tuple[str, list[str], str]] = [
    (
        'revenue',
        [
            'Revenues',
            'RevenueFromContractWithCustomerExcludingAssessedTax',
            'SalesRevenueNet',
            'RevenueFromContractWithCustomerIncludingAssessedTax',
        ],
        'USD',
    ),
    ('gross_profit', ['GrossProfit'], 'USD'),
    ('operating_income', ['OperatingIncomeLoss'], 'USD'),
    ('net_income', ['NetIncomeLoss', 'ProfitLoss'], 'USD'),
    ('eps_diluted', ['EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted'], 'USD/shares'),
    ('total_assets', ['Assets'], 'USD'),
    ('total_liabilities', ['Liabilities'], 'USD'),
    ('stockholders_equity', ['StockholdersEquity'], 'USD'),
    ('cash', ['CashAndCashEquivalentsAtCarryingValue'], 'USD'),
    ('operating_cash_flow', ['NetCashProvidedByUsedInOperatingActivities'], 'USD'),
    ('shares_outstanding', ['EntityCommonStockSharesOutstanding', 'CommonStockSharesOutstanding'], 'shares'),
]


def annual_series(facts: dict, concepts: list[str], unit: str, years: int) -> list[dict]:
    """Latest `years` fiscal-year values from 10-K filings for the first concept that has data."""
    for concept in concepts:
        for taxonomy in ('us-gaap', 'dei'):
            entries = ((facts.get(taxonomy) or {}).get(concept) or {}).get('units', {}).get(unit) or []
            annual = [e for e in entries if e.get('form') in ('10-K', '10-K/A') and e.get('fp') == 'FY']
            if not annual:
                continue
            # Keep the most recently filed value for each period end (restatements win).
            by_end: dict[str, dict] = {}
            for e in annual:
                end = e.get('end')
                if end and (end not in by_end or e.get('filed', '') > by_end[end].get('filed', '')):
                    by_end[end] = e
            latest = sorted(by_end.values(), key=lambda e: e['end'], reverse=True)[:years]
            if latest:
                return [{'period_end': e['end'], 'fiscal_year': e.get('fy'), 'value': e.get('val')} for e in latest]
    return []


def parse_fundamentals(symbol: str, raw: dict, years: int = 4) -> dict:
    facts = raw.get('facts') or {}
    return {
        'symbol': symbol,
        'company': raw.get('entityName'),
        'cik': raw.get('cik'),
        'source': 'SEC EDGAR (10-K, fiscal years)',
        'metrics': {
            name: annual_series(facts, concepts, unit, years) for name, concepts, unit in FUNDAMENTAL_CONCEPTS
        },
    }


def parse_filings(cik: str, raw: dict, forms: Optional[list[str]], limit: int) -> list[dict]:
    recent = (raw.get('filings') or {}).get('recent') or {}
    result = []
    wanted = {f.upper() for f in forms} if forms else None
    for i, form in enumerate(recent.get('form') or []):
        if wanted and form.upper() not in wanted:
            continue
        accession = recent['accessionNumber'][i]
        document = (recent.get('primaryDocument') or [''] * (i + 1))[i]
        result.append(
            {
                'form': form,
                'filing_date': recent['filingDate'][i],
                'report_date': (recent.get('reportDate') or [None] * (i + 1))[i] or None,
                'description': (recent.get('primaryDocDescription') or [None] * (i + 1))[i] or None,
                'url': ARCHIVE_URL.format(cik=int(cik), accession=accession.replace('-', ''), document=document)
                if document
                else None,
            }
        )
        if len(result) >= limit:
            break
    return result


class EdgarProvider(MarketDataProvider):
    id = 'edgar'

    def __init__(self, user_agent: str, timeout: float = 20):
        if not user_agent or '@' not in user_agent:
            raise FinanceProviderError(
                'SEC EDGAR needs a User-Agent containing a contact email (e.g. "Jane Doe jane@example.com")',
                status_code=400,
            )
        self._headers = {'User-Agent': user_agent, 'Accept-Encoding': 'gzip, deflate'}
        self._timeout = timeout

    async def _get(self, url: str):
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=True) as client:
                response = await client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise FinanceProviderError(f'SEC EDGAR request failed: {type(e).__name__}') from e
        if response.status_code == 404:
            raise FinanceProviderError('No SEC data found for this company', status_code=404)
        if response.status_code == 429:
            raise FinanceProviderError('SEC EDGAR rate limit reached, try again shortly', status_code=429)
        if response.status_code >= 400:
            raise FinanceProviderError(f'SEC EDGAR error {response.status_code}')
        return response.json()

    async def _tickers(self) -> dict[str, dict]:
        global _ticker_cache
        if _ticker_cache and time.time() - _ticker_cache[0] < TICKER_CACHE_TTL:
            return _ticker_cache[1]
        async with _ticker_lock:
            if _ticker_cache and time.time() - _ticker_cache[0] < TICKER_CACHE_TTL:
                return _ticker_cache[1]
            raw = await self._get(TICKERS_URL)
            tickers = {
                str(v['ticker']).upper(): {'cik': str(v['cik_str']).zfill(10), 'name': v.get('title')}
                for v in raw.values()
                if isinstance(v, dict) and v.get('ticker')
            }
            _ticker_cache = (time.time(), tickers)
            return tickers

    async def cik_for(self, symbol: str) -> str:
        tickers = await self._tickers()
        # SEC uses '-' for share classes (BRK-B); brokers often use '.'.
        entry = tickers.get(symbol) or tickers.get(symbol.replace('.', '-'))
        if not entry:
            raise FinanceProviderError(f'{symbol} is not in the SEC ticker list', status_code=404)
        return entry['cik']

    async def search(self, query: str, limit: int = 10, asset_class='us_equity') -> list[SymbolInfo]:
        q = query.strip().upper()
        tickers = await self._tickers()
        hits = [
            SymbolInfo(symbol=t, name=v['name'])
            for t, v in tickers.items()
            if t.startswith(q) or q in (v.get('name') or '').upper()
        ]
        hits.sort(key=lambda s: (s.symbol != q, not s.symbol.startswith(q), len(s.symbol)))
        return hits[:limit]

    async def fundamentals(self, symbol: str, years: int = 4) -> dict:
        cik = await self.cik_for(symbol)
        raw = await self._get(FACTS_URL.format(cik=cik))
        return parse_fundamentals(symbol, raw, years=max(1, min(years, 10)))

    async def filings(self, symbol: str, forms: Optional[list[str]] = None, limit: int = 10) -> list[dict]:
        cik = await self.cik_for(symbol)
        raw = await self._get(SUBMISSIONS_URL.format(cik=cik))
        return parse_filings(cik, raw, forms, max(1, min(limit, 100)))
