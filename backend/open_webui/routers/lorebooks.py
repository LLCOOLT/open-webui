import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import CHARACTER_CARD_MAX_BYTES
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.lorebooks import (
    LorebookForm,
    LorebookListResponse,
    LorebookModel,
    Lorebooks,
)
from open_webui.utils.access_control import filter_allowed_access_grants, has_permission
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.roleplay.cards import CardParseError, parse_card_bytes
from open_webui.utils.roleplay.lorebook import activate, normalize_lorebook
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()


class LorebookResponse(LorebookModel):
    write_access: bool = False


async def _require_enabled() -> None:
    if not await Config.get('characters.enable'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Character cards are disabled.')


async def _can_create(user, db: AsyncSession) -> bool:
    return user.role == 'admin' or await has_permission(
        user.id, 'workspace.lorebooks', await Config.get('user.permissions'), db=db
    )


async def _has_access(lorebook: LorebookModel, user, permission: str, db: AsyncSession) -> bool:
    if user.role == 'admin' and (permission == 'write' or BYPASS_ADMIN_ACCESS_CONTROL):
        return True
    if lorebook.user_id == user.id:
        return True
    for level in ('write',) if permission == 'write' else ('read', 'write'):
        if await AccessGrants.has_access(
            user_id=user.id, resource_type='lorebook', resource_id=lorebook.id, permission=level, db=db
        ):
            return True
    return False


async def _get_lorebook(id: str, user, permission: str, db: AsyncSession) -> LorebookModel:
    lorebook = await Lorebooks.get_lorebook_by_id(id, db=db)
    if lorebook is None or not await _has_access(lorebook, user, permission, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return lorebook


async def _filter_grants(form_data: LorebookForm, user, db: AsyncSession) -> None:
    if form_data.access_grants is not None:
        form_data.access_grants = await filter_allowed_access_grants(
            await Config.get('user.permissions'),
            user.id,
            user.role,
            form_data.access_grants,
            'sharing.public_lorebooks',
            db=db,
        )


############################
# List / get
############################


@router.get('/', response_model=LorebookListResponse)
async def get_lorebooks(
    query: str | None = None,
    permission: str = 'read',
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Lorebooks the user can read (or write, with ``permission=write``)."""
    await _require_enabled()
    see_all = user.role == 'admin' and (permission == 'write' or BYPASS_ADMIN_ACCESS_CONTROL)
    return await Lorebooks.search_lorebooks(
        None if see_all else user.id,
        query=query,
        permission='write' if permission == 'write' else 'read',
        db=db,
    )


@router.get('/{id}', response_model=LorebookResponse)
async def get_lorebook_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    await _require_enabled()
    lorebook = await _get_lorebook(id, user, 'read', db)
    return LorebookResponse(**lorebook.model_dump(), write_access=await _has_access(lorebook, user, 'write', db))


############################
# Create / update / delete
############################


@router.post('/create', response_model=LorebookModel)
async def create_lorebook(
    form_data: LorebookForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _require_enabled()
    if not await _can_create(user, db):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
    await _filter_grants(form_data, user, db)
    return await Lorebooks.insert_new_lorebook(user.id, form_data, db=db)


@router.post('/{id}/update', response_model=LorebookModel)
async def update_lorebook(
    id: str,
    form_data: LorebookForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _require_enabled()
    lorebook = await _get_lorebook(id, user, 'write', db)
    if form_data.access_grants is not None:
        # Keep grants the editor resends unchanged; only newly added ones are checked.
        existing = {(grant.principal_type, grant.principal_id, grant.permission) for grant in lorebook.access_grants}
        added = [
            grant
            for grant in form_data.access_grants
            if (grant.get('principal_type'), grant.get('principal_id'), grant.get('permission')) not in existing
        ]
        kept = [grant for grant in form_data.access_grants if grant not in added]
        form_data.access_grants = added
        await _filter_grants(form_data, user, db)
        form_data.access_grants = kept + (form_data.access_grants or [])
    updated = await Lorebooks.update_lorebook_by_id(id, form_data, db=db)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return updated


@router.delete('/{id}/delete', response_model=bool)
async def delete_lorebook(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    await _require_enabled()
    await _get_lorebook(id, user, 'write', db)
    return await Lorebooks.delete_lorebook_by_id(id, db=db)


class LorebookTestForm(BaseModel):
    data: dict
    text: str = Field(max_length=20_000)


class LorebookTestResponse(BaseModel):
    before_char: list[str]
    after_char: list[str]
    depth: list[dict]


@router.post('/test', response_model=LorebookTestResponse)
async def test_lorebook(
    form_data: LorebookTestForm, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    """Which entries of an (unsaved) lorebook ``text`` would activate, as the editor's preview."""
    await _require_enabled()
    if not await _can_create(user, db):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
    book = normalize_lorebook(form_data.data) or {'entries': []}
    result = await asyncio.to_thread(activate, [book], [{'role': 'user', 'content': form_data.text}])
    return LorebookTestResponse(
        before_char=result.before_char,
        after_char=result.after_char,
        depth=[{'text': entry.text, 'depth': entry.depth, 'role': entry.role} for entry in result.depth],
    )


############################
# Import / export
############################


def lorebook_form_from_file(data: bytes, filename: str) -> LorebookForm:
    """Read a ``lorebook_v3`` file, a SillyTavern world-info export, or a card's embedded book."""
    book = None
    if not data.startswith((b'\x89PNG', b'PK\x03\x04')):
        try:
            parsed = json.loads(data.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CardParseError('Lorebook file is not valid JSON.') from error
        if isinstance(parsed, dict) and (parsed.get('spec') == 'lorebook_v3' or 'entries' in parsed):
            book = normalize_lorebook(parsed)
    if book is None:
        character = parse_card_bytes(data, filename).character
        book = character.character_book
        if not book:
            raise CardParseError('The card has no embedded lorebook.')
        book['name'] = book.get('name') or f'{character.name} lorebook'
    if not book['entries']:
        raise CardParseError('The lorebook has no entries.')

    stem = re.sub(r'\.[^.]+$', '', filename or '') or 'Lorebook'
    return LorebookForm(
        name=(book.get('name') or stem)[:256],
        description=book.get('description') or None,
        data=book,
        meta={'source': 'import', 'filename': filename},
    )


@router.post('/import', response_model=LorebookModel)
async def import_lorebook(
    file: UploadFile = File(...),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Admin only, like character card import."""
    await _require_enabled()
    data = await file.read(CHARACTER_CARD_MAX_BYTES + 1)
    if len(data) > CHARACTER_CARD_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Lorebook file is too large.')
    try:
        form_data = await asyncio.to_thread(lorebook_form_from_file, data, file.filename or '')
    except CardParseError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return await Lorebooks.insert_new_lorebook(user.id, form_data, db=db)


def lorebook_v3(lorebook: LorebookModel) -> dict:
    return {'spec': 'lorebook_v3', 'data': lorebook.as_book()}


@router.get('/{id}/export')
async def export_lorebook(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    await _require_enabled()
    lorebook = await _get_lorebook(id, user, 'read', db)
    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', lorebook.name).strip('_') or 'lorebook'
    return Response(
        content=json.dumps(lorebook_v3(lorebook), ensure_ascii=False),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="{stem}.json"'},
    )
