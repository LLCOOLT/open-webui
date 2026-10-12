import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.config import Config
from open_webui.models.finance import (
    FinanceConnectionForm,
    FinanceConnectionResponse,
    FinanceConnections,
)
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_verified_user
from open_webui.utils.finance import service
from open_webui.utils.finance.base import FinanceProviderError, normalize_symbol

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
