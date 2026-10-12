import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.config import Config
from open_webui.models.finance import (
    FinanceBacktestModel,
    FinanceBacktests,
    FinanceConnectionForm,
    FinanceConnectionResponse,
    FinanceConnections,
    FinanceStrategies,
    FinanceStrategyForm,
    FinanceStrategyModel,
    FinanceStrategyVersionModel,
)
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_verified_user
from open_webui.utils.finance import service
from open_webui.utils.finance.backtest import runner as backtest_runner
from open_webui.utils.finance.backtest.runner import BacktestRequest
from open_webui.utils.finance.backtest.engine import BacktestError
from open_webui.utils.finance.backtest.starters import PYTHON_STARTER, STARTER_TEMPLATES
from open_webui.utils.finance.backtest.validate import StrategyValidateForm, validate_strategy
from open_webui.utils.finance.base import TIMEFRAMES, FinanceProviderError, normalize_symbol

log = logging.getLogger(__name__)

router = APIRouter()


############################
# Helpers
############################


async def check_finance_permission(user):
    config = await Config.get_many('finance.enable', 'user.permissions')
    if not config.get('finance.enable'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.UNAUTHORIZED)
    if user.role != 'admin' and not await has_permission(user.id, 'features.finance', config.get('user.permissions')):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.UNAUTHORIZED)


async def finance_user(user=Depends(get_verified_user)):
    await check_finance_permission(user)
    return user


def provider_error(e: FinanceProviderError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


async def get_owned_connection(id: str, user) -> FinanceConnectionResponse:
    connection = await FinanceConnections.get_by_id(id)
    if not connection or connection.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return connection


############################
# Providers & Connections
############################


@router.get('/providers')
async def get_providers(user=Depends(finance_user)):
    return service.PROVIDERS


@router.get('/connections', response_model=list[FinanceConnectionResponse])
async def get_connections(user=Depends(finance_user)):
    return await FinanceConnections.get_by_user(user.id)


@router.post('/connections/create', response_model=FinanceConnectionResponse)
async def create_connection(form_data: FinanceConnectionForm, user=Depends(finance_user)):
    try:
        service.validate_connection_form(form_data)
    except FinanceProviderError as e:
        raise provider_error(e)
    return await FinanceConnections.insert(user.id, form_data)


@router.post('/connections/{id}/update', response_model=FinanceConnectionResponse)
async def update_connection(id: str, form_data: FinanceConnectionForm, user=Depends(finance_user)):
    await get_owned_connection(id, user)
    try:
        service.validate_connection_form(form_data)
    except FinanceProviderError as e:
        raise provider_error(e)
    return await FinanceConnections.update_by_id(id, form_data)


@router.delete('/connections/{id}/delete')
async def delete_connection(id: str, user=Depends(finance_user)):
    await get_owned_connection(id, user)
    return await FinanceConnections.delete_by_id(id)


@router.post('/connections/{id}/test')
async def test_connection(id: str, user=Depends(finance_user)):
    connection = await get_owned_connection(id, user)
    credentials = await FinanceConnections.get_credentials_by_id(id)
    try:
        return await service.test_connection(connection, credentials)
    except FinanceProviderError as e:
        return {'ok': False, 'error': str(e)}


############################
# Market data (read-only)
############################


@router.get('/quote')
async def get_quote(symbol: str, connection_id: Optional[str] = None, user=Depends(finance_user)):
    try:
        broker = await service.get_broker(user.id, connection_id)
        return await broker.quote(normalize_symbol(symbol))
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/bars')
async def get_bars(
    symbol: str,
    timeframe: str = '1Day',
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
    connection_id: Optional[str] = None,
    user=Depends(finance_user),
):
    try:
        broker = await service.get_broker(user.id, connection_id)
        bars = await service.get_bars(broker, symbol, timeframe, start, end, limit)
        return {'symbol': normalize_symbol(symbol), 'timeframe': timeframe, 'bars': bars}
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/search')
async def search_symbols(q: str, limit: int = 10, connection_id: Optional[str] = None, user=Depends(finance_user)):
    try:
        broker = await service.get_broker(user.id, connection_id)
        return await broker.search(q, limit=max(1, min(limit, 50)))
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/news')
async def get_news(
    symbols: Optional[str] = None, limit: int = 10, connection_id: Optional[str] = None, user=Depends(finance_user)
):
    try:
        broker = await service.get_broker(user.id, connection_id)
        symbol_list = [normalize_symbol(s) for s in (symbols or '').split(',') if s.strip()]
        return await broker.news(symbol_list, limit=limit)
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/fundamentals')
async def get_fundamentals(symbol: str, years: int = 4, user=Depends(finance_user)):
    try:
        edgar = await service.get_edgar(user.id)
        return await edgar.fundamentals(normalize_symbol(symbol), years=years)
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/filings')
async def get_filings(symbol: str, forms: Optional[str] = None, limit: int = 10, user=Depends(finance_user)):
    try:
        edgar = await service.get_edgar(user.id)
        form_list = [f.strip() for f in (forms or '').split(',') if f.strip()] or None
        return await edgar.filings(normalize_symbol(symbol), forms=form_list, limit=limit)
    except FinanceProviderError as e:
        raise provider_error(e)


############################
# Account (read-only)
############################


@router.get('/account')
async def get_account(connection_id: Optional[str] = None, user=Depends(finance_user)):
    try:
        broker = await service.get_broker(user.id, connection_id)
        return await broker.account()
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/positions')
async def get_positions(connection_id: Optional[str] = None, user=Depends(finance_user)):
    try:
        broker = await service.get_broker(user.id, connection_id)
        return await broker.positions()
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/orders')
async def get_orders(
    status: str = 'open', limit: int = 50, connection_id: Optional[str] = None, user=Depends(finance_user)
):
    try:
        broker = await service.get_broker(user.id, connection_id)
        return await broker.orders(status=status, limit=limit)
    except FinanceProviderError as e:
        raise provider_error(e)


############################
# Strategies
############################


def check_strategy_kind(form: FinanceStrategyForm, user) -> None:
    # Python strategies execute arbitrary code on the server, same trust level as Tools/Functions.
    if form.kind == 'python' and user.role != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Only admins can use Python strategies')
    try:
        validate_strategy(form.kind, form.definition, form.source, form.params)
    except BacktestError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if form.timeframe not in TIMEFRAMES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid timeframe')
    try:
        form.universe = list(dict.fromkeys(normalize_symbol(s) for s in form.universe))
    except FinanceProviderError as e:
        raise provider_error(e)


async def get_owned_strategy(id: str, user) -> FinanceStrategyModel:
    strategy = await FinanceStrategies.get_by_id(id)
    if not strategy or strategy.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return strategy


@router.get('/strategies/templates')
async def get_strategy_templates(user=Depends(finance_user)):
    templates = list(STARTER_TEMPLATES)
    if user.role == 'admin':
        templates.append(
            {
                'id': 'python_blank',
                'name': 'Python strategy',
                'description': 'Write signals(df, params) in Python. Runs on the server (admin only).',
                'kind': 'python',
                'source': PYTHON_STARTER,
                'params': {'fast': 20, 'slow': 50},
                'timeframe': '1Day',
                'universe': ['SPY'],
            }
        )
    return templates


@router.get('/strategies', response_model=list[FinanceStrategyModel])
async def get_strategies(user=Depends(finance_user)):
    return await FinanceStrategies.get_by_user(user.id)


@router.post('/strategies/create', response_model=FinanceStrategyModel)
async def create_strategy(form_data: FinanceStrategyForm, user=Depends(finance_user)):
    check_strategy_kind(form_data, user)
    return await FinanceStrategies.insert(user.id, form_data)


@router.post('/strategies/validate')
async def validate_strategy_route(form_data: StrategyValidateForm, user=Depends(finance_user)):
    if form_data.kind == 'python' and user.role != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Only admins can use Python strategies')
    try:
        # Python source is only syntax-checked here; it is executed in the worker during backtests.
        validate_strategy(form_data.kind, form_data.definition, form_data.source, form_data.params or {})
        return {'ok': True}
    except BacktestError as e:
        return {'ok': False, 'error': str(e)}


@router.get('/strategies/{id}', response_model=FinanceStrategyModel)
async def get_strategy(id: str, user=Depends(finance_user)):
    return await get_owned_strategy(id, user)


@router.post('/strategies/{id}/update', response_model=FinanceStrategyModel)
async def update_strategy(id: str, form_data: FinanceStrategyForm, user=Depends(finance_user)):
    await get_owned_strategy(id, user)
    check_strategy_kind(form_data, user)
    return await FinanceStrategies.update_by_id(id, form_data)


@router.get('/strategies/{id}/versions', response_model=list[FinanceStrategyVersionModel])
async def get_strategy_versions(id: str, user=Depends(finance_user)):
    await get_owned_strategy(id, user)
    return await FinanceStrategies.get_versions(id)


@router.delete('/strategies/{id}/delete')
async def delete_strategy(id: str, user=Depends(finance_user)):
    await get_owned_strategy(id, user)
    return await FinanceStrategies.delete_by_id(id)


############################
# Backtests
############################


@router.post('/backtests/run', response_model=FinanceBacktestModel)
async def run_backtest(form_data: BacktestRequest, user=Depends(finance_user)):
    try:
        return await backtest_runner.start(user, form_data)
    except FinanceProviderError as e:
        raise provider_error(e)


@router.get('/backtests', response_model=list[FinanceBacktestModel])
async def get_backtests(strategy_id: Optional[str] = None, limit: int = 50, user=Depends(finance_user)):
    return await FinanceBacktests.get_by_user(user.id, strategy_id=strategy_id, limit=max(1, min(limit, 200)))


@router.get('/backtests/{id}', response_model=FinanceBacktestModel)
async def get_backtest(id: str, user=Depends(finance_user)):
    backtest = await FinanceBacktests.get_by_id(id)
    if not backtest or backtest.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return backtest


@router.delete('/backtests/{id}/delete')
async def delete_backtest(id: str, user=Depends(finance_user)):
    await get_backtest(id, user)
    return await FinanceBacktests.delete_by_id(id)
