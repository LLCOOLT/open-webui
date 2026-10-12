from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import json
import logging
import posixpath
import re
import uuid
import zipfile
from copy import deepcopy
from typing import Literal, Optional
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import (
    BYPASS_MODEL_ACCESS_CONTROL,
    CHARACTER_CARD_MAX_BYTES,
    ENABLE_PROFILE_IMAGE_URL_FORWARDING,
    PROFILE_IMAGE_ALLOWED_MIME_TYPES,
)
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants, normalize_access_grants
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.groups import Groups
from open_webui.models.lorebooks import LorebookForm, Lorebooks
from open_webui.models.model_history import ModelHistories, ModelHistoryModel, ModelHistoryResponse
from open_webui.models.models import (
    ModelAccessListResponse,
    ModelAccessResponse,
    ModelForm,
    ModelListResponse,
    ModelMeta,
    ModelModel,
    ModelParams,
    ModelResponse,
    Models,
)
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import filter_allowed_access_grants, has_access, has_permission
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.chat_variables import get_chat_variables_schema
from open_webui.utils.models import get_all_models
from open_webui.utils.roleplay.assist import clean_image_prompt, sprite_edit_prompt, sprite_prompt
from open_webui.utils.roleplay.cards import (
    ZIP_SIGNATURE,
    CardParseError,
    ParsedCard,
    character_to_v3,
    parse_card_bytes,
    sniff_image_type,
    write_card_png,
)
from open_webui.utils.roleplay.character import CharacterMeta, character_summary
from open_webui.utils.roleplay.convert import (
    Conversion,
    ai_conversion_prompt,
    character_from_ai_reply,
    parse_system_prompt,
)
from open_webui.utils.roleplay.expressions import (
    MAX_EXPRESSIONS,
    classify_prompt,
    clean_expression_name,
    expression_name_from_filename,
    pick_expression,
)
from open_webui.utils.roleplay.filter import skip_character_system_prompt
from open_webui.utils.roleplay.images import avatar_thumbnail_data_uri, ensure_png, expression_image
from open_webui.utils.roleplay.prompt import build_macro_context
from open_webui.utils.roleplay.prompt import greetings as character_greetings
from open_webui.utils.roleplay.sources import SOURCES as CARD_SOURCES
from open_webui.utils.roleplay.sources import SourceError, download_card, parse_card_link, search_cards
from open_webui.utils.roleplay.story import MAX_STORY_CHARS as STORY_MAX_CHARS
from open_webui.utils.roleplay.story import analyze_story, greeting_prompt, write_profile
from open_webui.utils.roleplay.task_model import run_task_prompt
from open_webui.utils.validate import BACKGROUND_IMAGE_MAX_BYTES, validate_background_image
from open_webui.utils.voice_avatar import (
    AVATAR_MAX_BYTES,
    ANIMATION_MAX_BYTES,
    validate_voice_avatar,
    validate_voice_animation,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()


def model_response(model):
    return model.model_dump() if model is not None else None


async def _check_model_controls(form, previous, user, request):
    old = previous.params.model_dump().get('model_controls', {}) if previous else {}
    if 'model_controls' not in form.params.model_fields_set:
        if old:
            form.params.model_controls = previous.params.model_controls
        return
    controls = form.params.model_dump().get('model_controls', {})
    if controls:
        if not request.app.state.MODELS:
            await get_all_models(request, user=user)
        base = request.app.state.MODELS.get(form.base_model_id or form.id, {})
        if 'pipe' in base or base.get('direct') or base.get('owned_by') == 'arena':
            raise HTTPException(400, 'Model controls require a server-managed provider model.')


def add_chat_variables_schema(model_dict: dict) -> dict:
    system = (model_dict.get('params') or {}).get('system') if isinstance(model_dict.get('params'), dict) else None
    schema = get_chat_variables_schema(system)
    if schema:
        model_dict.setdefault('meta', {})['chat_variables_schema'] = schema
    elif isinstance(model_dict.get('meta'), dict):
        model_dict['meta'].pop('chat_variables_schema', None)
    return model_dict


def hide_curated_config(model_dict: dict) -> dict:
    """Remove prompt configuration that read-only callers must not see (in place)."""
    model_dict['params'] = {}
    meta = model_dict.get('meta')
    if isinstance(meta, dict) and isinstance(meta.get('character'), dict):
        meta['character'] = character_summary(meta['character'])
    return model_dict


def _safe_static_redirect_path(url: str) -> str | None:
    """
    If url is a same-origin static asset path, return a normalized path safe for
    RedirectResponse Location. Otherwise None (caller should fall back to default).
    Rejects traversal (..), encoded dots, query/fragment, and non-/static targets.
    """
    if not url or not isinstance(url, str):
        return None
    path = url.split('?', 1)[0].split('#', 1)[0].strip()
    for _ in range(2):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    # Fail closed: a value still encoded after the cap would be decoded further downstream.
    if unquote(path) != path:
        return None
    if '\x00' in path or '\\' in path:
        return None
    if not path.startswith('/'):
        return None
    normalized = posixpath.normpath(path)
    if normalized in ('.', '/'):
        return None
    if not (normalized == '/static' or normalized.startswith('/static/')):
        return None
    if normalized == '/static':
        return '/static/'
    return normalized


def is_valid_model_id(model_id: str) -> bool:
    return model_id and len(model_id) <= 256 and not any(char.isspace() for char in model_id)


async def _verify_background_image(url: str | None, user, db, previous_url: str | None = None) -> None:
    if not url or url == previous_url:
        return
    file_id = url.split('/')[-2]
    file = await Files.get_file_by_id(file_id, db=db)
    if not file or not (
        user.role == 'admin' or file.user_id == user.id or await has_access_to_file(file_id, 'read', user, db=db)
    ):
        raise HTTPException(status_code=403, detail='Background image is not accessible.')
    try:
        path = await asyncio.to_thread(Storage.get_file, file.path)
        with open(path, 'rb') as image:
            data = await asyncio.to_thread(image.read, BACKGROUND_IMAGE_MAX_BYTES + 1)
        content_type = await asyncio.to_thread(validate_background_image, data)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if (file.meta or {}).get('content_type') != content_type:
        if not await Files.update_file_metadata_by_id(file_id, {'content_type': content_type}, db=db):
            raise HTTPException(status_code=500, detail='Could not validate background image.')


async def _verify_voice_avatar(avatar, user, db, previous=None) -> None:
    if not avatar:
        return
    assets = {avatar.file_id: (AVATAR_MAX_BYTES, validate_voice_avatar)}
    for asset in [*avatar.states.values(), *avatar.gestures]:
        if asset.file_id == avatar.file_id:
            raise HTTPException(status_code=400, detail='An animation must be a VRMA file, not the avatar.')
        assets[asset.file_id] = (ANIMATION_MAX_BYTES, validate_voice_animation)
    previous_assets = (
        (
            {previous.file_id: validate_voice_avatar}
            | {asset.file_id: validate_voice_animation for asset in [*previous.states.values(), *previous.gestures]}
        )
        if previous
        else {}
    )
    for file_id, (limit, validate) in assets.items():
        if previous_assets.get(file_id) is validate:
            continue
        file = await Files.get_file_by_id(file_id, db=db)
        if not file or not (
            user.role == 'admin' or file.user_id == user.id or await has_access_to_file(file_id, 'read', user, db=db)
        ):
            raise HTTPException(status_code=403, detail='Avatar or animation file is not accessible. Upload it again.')
        try:
            path = await asyncio.to_thread(Storage.get_file, file.path)
            with open(path, 'rb') as source:
                data = await asyncio.to_thread(source.read, limit + 1)
            await asyncio.to_thread(validate, data)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


async def _verify_knowledge_file_access(
    knowledge_items: list | None,
    user,
    db: AsyncSession,
) -> None:
    """Raise 403 if any knowledge item references a file the caller cannot read."""
    if not knowledge_items or user.role == 'admin':
        return
    for item in knowledge_items:
        if not isinstance(item, dict) or item.get('type') != 'file':
            continue
        file_id = item.get('id')
        if not file_id:
            continue
        if not await has_access_to_file(file_id, 'read', user, db=db):
            log.warning(
                'knowledge file access denied: user %s cannot read file %s',
                user.id,
                file_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )


###########################
# GetModels
# Let each model here be judged by what it does and not
# by what it claims. The house deserves honest servants.
###########################


PAGE_ITEM_COUNT = 30


@router.get(
    '/list',
    response_model=ModelAccessListResponse,
    response_model_exclude={'items': {'__all__': {'meta': {'profile_image_url'}}}},
)  # do NOT use "/" as path, conflicts with main.py
async def get_models(
    query: str | None = None,
    view_option: str | None = None,
    tag: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    page: int | None = 1,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    limit = PAGE_ITEM_COUNT

    page = max(1, page)
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter['query'] = query
    if view_option:
        filter['view_option'] = view_option
    if tag:
        filter['tag'] = tag
    if order_by:
        filter['order_by'] = order_by
    if direction:
        filter['direction'] = direction

    # Pre-fetch user group IDs once - used for both filter and write_access check
    groups = await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)
    user_group_ids = {group.id for group in groups}

    if not user.role == 'admin' or not BYPASS_ADMIN_ACCESS_CONTROL:
        if groups:
            filter['group_ids'] = [group.id for group in groups]

        filter['user_id'] = user.id

    result = await Models.search_models(user.id, filter=filter, skip=skip, limit=limit, db=db)

    # Batch-fetch writable model IDs in a single query instead of N has_access calls
    model_ids = [model.id for model in result.items]
    writable_model_ids = await AccessGrants.get_accessible_resource_ids(
        user_id=user.id,
        resource_type='model',
        resource_ids=model_ids,
        permission='write',
        user_group_ids=user_group_ids,
        db=db,
    )

    # Strip profile_image_url from meta — images are served via /model/profile/image.
    items = []
    for model in result.items:
        data = add_chat_variables_schema(model_response(model))
        if data.get('meta'):
            data['meta'].pop('profile_image_url', None)
        write_access = (
            (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
            or user.id == model.user_id
            or model.id in writable_model_ids
        )
        # Strip params (system prompt and other curated config) for read-only
        # callers, mirroring the per-id endpoint.
        if not write_access:
            hide_curated_config(data)
        items.append(ModelAccessResponse(**data, write_access=write_access))

    return ModelAccessListResponse(
        items=items,
        total=result.total,
    )


###########################
# GetBaseModels
###########################


@router.get('/all', response_model=list[ModelResponse])
async def get_all_model_records(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    return await Models.get_all_models(db=db)


@router.get('/base/tags', response_model=list[str])
async def get_base_model_tags(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    tags = await Models.get_all_tags(user_id=user.id, is_admin=True, is_base_model=True, db=db)
    return sorted(tags)


@router.get('/base', response_model=list[ModelResponse])
async def get_base_models(
    tag: str | None = None,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await Models.get_base_models(tag=tag, db=db)


###########################
# GetModelTags
###########################


@router.get('/tags', response_model=list[str])
async def get_model_tags(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    tags = await Models.get_all_tags(
        user_id=user.id,
        is_admin=(user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL),
        db=db,
    )
    return sorted(tags)


############################
# CreateNewModel
############################


@router.post('/create', response_model=ModelModel | None)
async def create_new_model(
    request: Request,
    form_data: ModelForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Create a new workspace model entry."""
    if user.role != 'admin' and not await has_permission(
        user.id, 'workspace.models', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    if not is_valid_model_id(form_data.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.MODEL_ID_TOO_LONG,
        )
    if form_data.base_model_id == form_data.id:
        # Should never be stored: a model cannot be based on itself.
        form_data.base_model_id = None

    model = await Models.get_model_by_id(form_data.id, db=db)
    if model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.MODEL_ID_TAKEN,
        )

    if user.role != 'admin':
        if not form_data.base_model_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )

        if not request.app.state.MODELS:
            await get_all_models(request, user=user)
        for base_model in request.app.state.MODELS.values():
            base_model_id = base_model.get('id')
            if base_model.get('preset') or not base_model_id:
                continue

            if form_data.id == base_model_id or (
                base_model.get('owned_by') == 'ollama' and form_data.id == base_model_id.split(':', 1)[0]
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=ERROR_MESSAGES.MODEL_ID_TAKEN,
                )

    await _verify_knowledge_file_access(
        getattr(form_data.meta, 'knowledge', None) if form_data.meta else None,
        user,
        db,
    )

    await _verify_background_image(form_data.meta.background_image_url, user, db)
    _keep_character_files(form_data.meta, None)
    await _verify_voice_avatar(form_data.meta.voice_avatar, user, db)

    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_models',
    )

    await _check_model_controls(form_data, None, user, request)
    model = await Models.insert_new_model(form_data, user.id, db=db)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.DEFAULT(),
        )

    await publish_event(
        request,
        EVENTS.MODEL_CREATED,
        actor=user,
        subject_id=model.id,
        data={'name': model.name},
    )
    return model_response(model)


############################
# ExportModels
############################


class ModelExportResponse(ModelModel):
    background_image_data: str | None = None


@router.get('/export', response_model=list[ModelExportResponse])
async def export_models(
    request: Request,
    ids: list[str] | None = Query(None),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role != 'admin' and not await has_permission(
        user.id,
        'workspace.models_export',
        await Config.get('user.permissions'),
        db=db,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    if user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL:
        models = await Models.get_models(db=db, ids=ids)
    else:
        models = await Models.get_models(writable_by_user_id=user.id, db=db, ids=ids)
    if ids is not None:
        requested = set(ids)
        if requested != {model.id for model in models}:
            raise HTTPException(status_code=403, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
    exported = []
    for model in models:
        data = model_response(model)
        url = model.meta.background_image_url
        if url:
            try:
                file = await Files.get_file_by_id(url.split('/')[-2], db=db)
                if not file:
                    raise ValueError('Image file is missing')
                path = await asyncio.to_thread(Storage.get_file, file.path)
                with open(path, 'rb') as image:
                    image_data = await asyncio.to_thread(image.read, BACKGROUND_IMAGE_MAX_BYTES + 1)
                content_type = await asyncio.to_thread(validate_background_image, image_data)
                data['background_image_data'] = f'data:{content_type};base64,' + base64.b64encode(image_data).decode(
                    'ascii'
                )
                data['meta']['background_image_url'] = None
            except Exception as error:
                raise HTTPException(
                    status_code=400, detail=f'Could not export background for model {model.id}.'
                ) from error
        exported.append(data)
    return exported


############################
# ImportModels
############################


class ModelsImportForm(BaseModel):
    models: list[dict]


@router.post('/import', response_model=bool)
async def import_models(
    request: Request,
    user=Depends(get_verified_user),
    form_data: ModelsImportForm = (...),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role != 'admin' and not await has_permission(
        user.id,
        'workspace.models_import',
        await Config.get('user.permissions'),
        db=db,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    try:
        data = form_data.models
        if isinstance(data, list):
            # Batch-fetch all existing models in one query to avoid N+1
            model_ids = [
                model_data.get('id')
                for model_data in data
                if model_data.get('id') and is_valid_model_id(model_data.get('id'))
            ]
            existing_models = {
                model.id: model for model in (await Models.get_models_by_ids(model_ids, db=db) if model_ids else [])
            }

            # Batch-resolve write permissions in one query instead of
            # per-model has_access calls (N+1 avoidance).
            existing_model_ids = list(existing_models.keys())
            if user.role != 'admin' and existing_model_ids:
                groups = await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)
                user_group_ids = {group.id for group in groups}
                writable_model_ids = await AccessGrants.get_accessible_resource_ids(
                    user_id=user.id,
                    resource_type='model',
                    resource_ids=existing_model_ids,
                    permission='write',
                    user_group_ids=user_group_ids,
                    db=db,
                )
            else:
                writable_model_ids = set(existing_model_ids)

            base_model_ids = None
            imported_ids = []
            for model_data in data:
                model_id = model_data.get('id')

                if model_id and is_valid_model_id(model_id):
                    if model_data.get('base_model_id') == model_id:
                        # Should never be stored: heal bad exports/API payloads.
                        model_data['base_model_id'] = None

                    # Defense-in-depth: skip models referencing inaccessible files
                    try:
                        await _verify_knowledge_file_access(
                            (model_data.get('meta') or {}).get('knowledge'),
                            user,
                            db,
                        )
                    except HTTPException:
                        log.warning(
                            'import_models: user %s skipped model %s (knowledge file access denied)',
                            user.id,
                            model_id,
                        )
                        continue

                    existing_model = existing_models.get(model_id)
                    if existing_model:
                        # Enforce ownership/write-access before allowing overwrite
                        if (
                            user.role != 'admin'
                            and existing_model.user_id != user.id
                            and model_id not in writable_model_ids
                        ):
                            log.warning(
                                'import_models: user %s skipped model %s (no write access)',
                                user.id,
                                model_id,
                            )
                            continue

                        if (
                            user.role != 'admin'
                            and existing_model.base_model_id
                            and not model_data.get('base_model_id', existing_model.base_model_id)
                        ):
                            log.warning(
                                'import_models: user %s skipped model %s (cannot clear base model)',
                                user.id,
                                model_id,
                            )
                            continue

                        # Update existing model
                        model_data['meta'] = {
                            **existing_model.meta.model_dump(),
                            **(model_data.get('meta') or {}),
                        }
                        model_data['params'] = model_data.get('params', {})

                        updated_model = ModelForm(**{**existing_model.model_dump(), **model_data})
                        # Only filter access_grants when explicitly provided
                        # in the payload to avoid altering existing ACLs on
                        # metadata-only imports.
                        if 'access_grants' in model_data:
                            updated_model.access_grants = await filter_allowed_access_grants(
                                await Config.get('user.permissions'),
                                user.id,
                                user.role,
                                updated_model.access_grants,
                                'sharing.public_models',
                            )
                        imported_model = updated_model
                    else:
                        # Insert new model
                        model_data['meta'] = model_data.get('meta', {})
                        model_data['params'] = model_data.get('params', {})
                        new_model = ModelForm(**model_data)

                        if user.role != 'admin':
                            if not new_model.base_model_id:
                                log.warning(
                                    'import_models: user %s skipped model %s (no base model set)',
                                    user.id,
                                    model_id,
                                )
                                continue

                            if base_model_ids is None:
                                base_model_ids = set()
                                if not request.app.state.MODELS:
                                    await get_all_models(request, user=user)
                                for base_model in request.app.state.MODELS.values():
                                    base_model_id = base_model.get('id')
                                    if base_model.get('preset') or not base_model_id:
                                        continue

                                    base_model_ids.add(base_model_id)
                                    if base_model.get('owned_by') == 'ollama':
                                        base_model_ids.add(base_model_id.split(':', 1)[0])

                            if model_id in base_model_ids:
                                log.warning(
                                    'import_models: user %s skipped model %s (id belongs to a base model)',
                                    user.id,
                                    model_id,
                                )
                                continue

                        new_model.access_grants = await filter_allowed_access_grants(
                            await Config.get('user.permissions'),
                            user.id,
                            user.role,
                            new_model.access_grants,
                            'sharing.public_models',
                        )
                        imported_model = new_model

                    await _check_model_controls(imported_model, existing_model, user, request)
                    uploaded = None
                    save_attempted = False
                    try:
                        encoded = model_data.pop('background_image_data', None)
                        if encoded is not None:
                            if (
                                not isinstance(encoded, str)
                                or len(encoded) > 4 * ((BACKGROUND_IMAGE_MAX_BYTES + 2) // 3) + 64
                            ):
                                raise ValueError('Background image must be at most 5 MiB.')
                            header, payload = encoded.split(',', 1)
                            image_data = base64.b64decode(payload, validate=True)
                            content_type = await asyncio.to_thread(validate_background_image, image_data)
                            if header != f'data:{content_type};base64':
                                raise ValueError('Invalid background image data URI.')
                            from fastapi import UploadFile
                            from open_webui.routers.files import upload_file_handler

                            uploaded = await upload_file_handler(
                                request,
                                file=UploadFile(
                                    file=io.BytesIO(image_data),
                                    filename='background.' + content_type.split('/')[1],
                                ),
                                metadata=None,
                                process=False,
                                user=user,
                                db=db,
                            )
                            imported_model.meta.background_image_url = f'/api/v1/files/{uploaded.id}/content'
                        await _verify_background_image(
                            imported_model.meta.background_image_url,
                            user,
                            db,
                            existing_model.meta.background_image_url if existing_model else None,
                        )
                        _keep_character_files(imported_model.meta, existing_model.meta if existing_model else None)
                        if existing_model and 'voice_avatar' not in imported_model.meta.model_fields_set:
                            imported_model.meta.voice_avatar = existing_model.meta.voice_avatar
                        await _verify_voice_avatar(
                            imported_model.meta.voice_avatar,
                            user,
                            db,
                            existing_model.meta.voice_avatar if existing_model else None,
                        )
                        save_attempted = True
                        saved = (
                            await Models.update_model_by_id(model_id, imported_model, db=db, user_id=user.id)
                            if existing_model
                            else await Models.insert_new_model(user_id=user.id, form_data=imported_model, db=db)
                        )
                        if not saved:
                            raise HTTPException(status_code=500, detail=f'Could not import model {model_id}.')
                    except Exception:
                        # A failed response can follow a commit; history may retain this upload.
                        if uploaded and not save_attempted:
                            try:
                                await Files.delete_file_by_id(uploaded.id, db=db)
                                await asyncio.to_thread(Storage.delete_file, uploaded.path)
                            except Exception:
                                log.exception('Could not clean up failed model background upload')
                        raise

                    imported_ids.append(model_id)
            await publish_event(
                request,
                EVENTS.MODEL_IMPORTED,
                actor=user,
                subject_type='model',
                data={'count': len(imported_ids), 'model_ids': imported_ids},
            )
            return True
        else:
            raise HTTPException(status_code=400, detail='Invalid JSON format')
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


############################
# Character cards
############################


def _character_model_id(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:64].strip('-')
    return f'{slug or "character"}-{uuid.uuid4().hex[:8]}'


async def _require_character_cards_enabled() -> None:
    if not await Config.get('characters.enable'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Character cards are disabled.')


async def _has_model_access(model: ModelModel, user, permission: str, db: AsyncSession) -> bool:
    # Admins can already edit every model through /model/update.
    if user.role == 'admin' or model.user_id == user.id:
        return True
    for level in ('write',) if permission == 'write' else ('read', 'write'):
        if await AccessGrants.has_access(
            user_id=user.id, resource_type='model', resource_id=model.id, permission=level, db=db
        ):
            return True
    return False


def _keep_character_files(meta, previous_meta) -> None:
    """Character file references (full-size avatar, expression images) are set only server side, by
    import and the expression endpoints; client-sent values could point at other users' files."""
    character = getattr(meta, 'character', None) if meta is not None else None
    if character is None:
        return
    previous = getattr(previous_meta, 'character', None) if previous_meta is not None else None
    character.avatar_file_id = previous.avatar_file_id if previous is not None else None
    character.expressions = dict(previous.expressions) if previous is not None else {}


async def _get_character_model(id: str, user, db: AsyncSession, permission: str) -> ModelModel:
    """Load a character model the user may read (or write). Missing and forbidden look the same."""
    model = await Models.get_model_by_id(id, db=db)
    if model is None or model.meta.character is None or not await _has_model_access(model, user, permission, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return model


async def _upload_character_image(request: Request, data: bytes, filename: str, user, db: AsyncSession):
    from open_webui.routers.files import upload_file_handler

    return await upload_file_handler(
        request,
        file=UploadFile(file=io.BytesIO(data), filename=filename),
        metadata=None,
        process=False,
        user=user,
        db=db,
    )


async def _discard_uploads(uploads: list, db: AsyncSession) -> None:
    for uploaded in uploads:
        try:
            await Files.delete_file_by_id(uploaded.id, db=db)
            await asyncio.to_thread(Storage.delete_file, uploaded.path)
        except Exception:
            log.exception('Could not clean up character image upload %s', uploaded.id)


async def _character_meta_from_card(request: Request, parsed: ParsedCard, user, db, uploads: list) -> ModelMeta:
    character = CharacterMeta.from_card(parsed.character)
    meta = {
        'description': character.creator_notes.strip()[:1000] or None,
        'tags': character.tags,
    }
    if parsed.avatar:
        meta['profile_image_url'] = await asyncio.to_thread(avatar_thumbnail_data_uri, parsed.avatar)
        extension = parsed.avatar_content_type.split('/')[1]
        uploaded = await _upload_character_image(request, parsed.avatar, f'avatar.{extension}', user, db)
        uploads.append(uploaded)
        character.avatar_file_id = uploaded.id
    if parsed.background:
        try:
            content_type = await asyncio.to_thread(validate_background_image, parsed.background)
        except ValueError as error:
            log.info('Skipping character background for %s: %s', character.name, error)
        else:
            extension = content_type.split('/')[1]
            uploaded = await _upload_character_image(request, parsed.background, f'background.{extension}', user, db)
            uploads.append(uploaded)
            meta['background_image_url'] = f'/api/v1/files/{uploaded.id}/content'
    for name, image, _ in parsed.expressions:
        if len(character.expressions) >= MAX_EXPRESSIONS:
            break
        stored = await _store_expression(request, name, image, user, db, uploads)
        if stored:
            character.expressions[stored[0]] = stored[1]
    return ModelMeta(**meta, character=character)


async def _store_expression(
    request: Request, name: str, image: bytes, user, db, uploads: list
) -> tuple[str, str] | None:
    """Normalize and upload one expression image; ``(clean name, file id)``, or ``None`` if unusable."""
    clean = clean_expression_name(name)
    if not clean:
        return None
    try:
        data, content_type = await asyncio.to_thread(expression_image, image)
    except ValueError as error:
        log.info('Skipping expression %r: %s', name, error)
        return None
    uploaded = await _upload_character_image(request, data, f'expression.{content_type.split("/")[1]}', user, db)
    uploads.append(uploaded)
    return clean, uploaded.id


async def _lorebook_from_card(character: CharacterMeta, access_grants: list, user, db: AsyncSession):
    """Move a card's embedded book into a standalone lorebook shared like the model; link it."""
    book = character.character_book
    character.character_book = None
    if not book or not book.get('entries'):
        return None
    lorebook = await Lorebooks.insert_new_lorebook(
        user.id,
        LorebookForm(
            name=(book.get('name') or f'{character.name} lorebook')[:256],
            description=book.get('description') or None,
            data=book,
            meta={'source': 'character_card', 'character': character.name},
            access_grants=access_grants,
        ),
        db=db,
    )
    return lorebook


def _merged_character_book(books: list[dict]) -> dict | None:
    """One ``character_book`` from several lorebooks: the first book's settings, every entry in order."""
    books = [book for book in books if book and book.get('entries')]
    if not books:
        return None
    entries = [entry for book in books for entry in book['entries']]
    return {**books[0], 'entries': [{**entry, 'id': index} for index, entry in enumerate(entries)]}


async def _base_model_exists(request: Request, base_model_id: str, user) -> bool:
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)
    return any(
        model.get('id') == base_model_id and not model.get('preset') for model in request.app.state.MODELS.values()
    )


@router.post('/characters/import', response_model=list[ModelModel])
async def import_character_cards(
    request: Request,
    files: list[UploadFile] = File(...),
    base_model_id: str = Form(...),
    public: bool = Form(False),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Create one character model per card (JSON, PNG or CHARX). Admin only, regardless of permissions."""
    await _require_character_cards_enabled()
    if not await _base_model_exists(request, base_model_id, user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown base model.')

    parsed_cards = []
    for file in files:
        data = await file.read(CHARACTER_CARD_MAX_BYTES + 1)
        try:
            parsed_cards.append(await asyncio.to_thread(parse_card_bytes, data, file.filename or ''))
        except CardParseError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f'{file.filename or "Card"}: {error}'
            ) from error
    return await _create_character_models(request, parsed_cards, base_model_id, public, user, db)


async def _create_character_models(request: Request, parsed_cards: list, base_model_id: str, public: bool, user, db):
    access_grants = [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}] if public else []
    created = []
    for parsed in parsed_cards:
        uploads: list = []
        lorebook = None
        try:
            meta = await _character_meta_from_card(request, parsed, user, db, uploads)
            lorebook = await _lorebook_from_card(meta.character, access_grants, user, db)
            if lorebook is not None:
                meta.lorebook_ids = [lorebook.id]
            model = await Models.insert_new_model(
                ModelForm(
                    id=_character_model_id(parsed.character.name),
                    base_model_id=base_model_id,
                    name=parsed.character.name.strip(),
                    meta=meta,
                    params=ModelParams(),
                    access_grants=access_grants,
                ),
                user.id,
                db=db,
            )
            if not model:
                raise HTTPException(status_code=500, detail=f'Could not import {parsed.character.name}.')
        except Exception as error:
            await _discard_uploads(uploads, db)
            if lorebook is not None:
                await Lorebooks.delete_lorebook_by_id(lorebook.id, db=db)
            if isinstance(error, ValueError):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
            raise
        created.append(model)
        await publish_event(request, EVENTS.MODEL_CREATED, actor=user, subject_id=model.id, data={'name': model.name})

    return created


class CardSourceImportForm(BaseModel):
    source: str = ''
    id: str = ''
    url: str = ''
    base_model_id: str
    public: bool = False


@router.get('/characters/sources')
async def list_card_sources(user=Depends(get_admin_user)):
    await _require_character_cards_enabled()
    return [
        {
            'id': source.id,
            'name': source.name,
            'url': source.url,
            'searchable': source.searchable,
            'supports_tags': source.supports_tags,
            # Whether the tags shown on results are the site's tags (Character Tavern shows warnings).
            'result_tags': source.supports_tags and source.tags_in_results,
        }
        for source in CARD_SOURCES.values()
    ]


@router.get('/characters/sources/{source_id}/search')
async def search_card_source(
    source_id: str,
    q: str = '',
    page: int = 1,
    sort: str = 'popular',
    nsfw: bool = False,
    tags: str = '',
    exclude_tags: str = '',
    user=Depends(get_admin_user),
):
    """Search a public card site. Admin only, like card import; the server fetches, so it works
    regardless of the browser's CORS rules. ``tags``/``exclude_tags`` are comma-separated."""
    await _require_character_cards_enabled()
    try:
        result = await search_cards(
            source_id, q, page, sort, nsfw, tags=tags.split(','), exclude_tags=exclude_tags.split(',')
        )
    except SourceError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    return result.as_dict()


@router.get('/characters/sources/{source_id}/card')
async def preview_card_from_source(source_id: str, id: str, user=Depends(get_admin_user)):
    """A card's contents, to read before importing. The download is kept briefly for the import."""
    await _require_character_cards_enabled()
    try:
        data, filename = await download_card(source_id, id)
    except SourceError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    try:
        parsed = await asyncio.to_thread(parse_card_bytes, data, filename)
    except CardParseError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    character = parsed.character
    book = character.character_book or {}
    return {
        'name': character.name,
        'creator': character.creator,
        'creator_notes': character.creator_notes,
        'description': character.description,
        'personality': character.personality,
        'scenario': character.scenario,
        'first_mes': character.first_mes,
        'alternate_greetings': character.alternate_greetings,
        'mes_example': character.mes_example,
        'system_prompt': character.system_prompt,
        'tags': character.tags,
        'lorebook_entries': len(book.get('entries') or []) if isinstance(book, dict) else 0,
        'expressions': [name for name, _, _ in parsed.expressions],
        'size': len(data),
    }


@router.post('/characters/sources/import', response_model=list[ModelModel])
async def import_card_from_source(
    request: Request,
    form_data: CardSourceImportForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Download one card from a card site (by source and id, or by its page link) and import it."""
    await _require_character_cards_enabled()
    await _require_base_model(request, form_data.base_model_id, user)
    source_id, card_id = form_data.source, form_data.id
    if form_data.url:
        link = parse_card_link(form_data.url)
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Paste the link to a character page on Chub, RisuRealm or Character Tavern, or a Poe bot.',
            )
        source_id, card_id = link
    try:
        data, filename = await download_card(source_id, card_id)
    except SourceError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    try:
        parsed = await asyncio.to_thread(parse_card_bytes, data, filename)
    except CardParseError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return await _create_character_models(request, [parsed], form_data.base_model_id, form_data.public, user, db)


class CharacterConvertForm(BaseModel):
    system: str = Field(max_length=100_000)
    name: str = ''
    base_model_id: str | None = None
    use_ai: bool = False


class CharacterConvertResponse(BaseModel):
    character: dict
    detected: list[str]
    method: Literal['parser', 'ai']


async def _convert_with_task_model(request: Request, form_data: CharacterConvertForm, user) -> Conversion:
    """Ask the configured task model to split the prompt; same model selection as title generation."""
    if not form_data.base_model_id or not await _base_model_exists(request, form_data.base_model_id, user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Choose a base model first.')
    reply = await run_task_prompt(
        request, user, form_data.base_model_id, ai_conversion_prompt(form_data.system), 'character_conversion'
    )
    return character_from_ai_reply(reply, form_data.name)


@router.post('/characters/convert', response_model=CharacterConvertResponse)
async def convert_system_prompt_to_character(
    request: Request,
    form_data: CharacterConvertForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Split an existing system prompt into character fields for the editor to review; saves nothing."""
    await _require_character_cards_enabled()
    if user.role != 'admin' and not await has_permission(
        user.id, 'workspace.models', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
    if not form_data.system.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='The system prompt is empty.')

    if not form_data.use_ai:
        conversion = parse_system_prompt(form_data.system, form_data.name)
        return CharacterConvertResponse(character=conversion.character, detected=conversion.detected, method='parser')

    try:
        conversion = await _convert_with_task_model(request, form_data, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    return CharacterConvertResponse(character=conversion.character, detected=conversion.detected, method='ai')


############################
# Characters from a story
############################


class StoryAnalyzeForm(BaseModel):
    text: str = Field(max_length=STORY_MAX_CHARS)
    title: str = Field(default='', max_length=200)
    base_model_id: str
    analysis_model_id: str | None = None  # None: the task model
    detailed: bool = True


class StoryProfileForm(BaseModel):
    text: str = Field(max_length=STORY_MAX_CHARS)
    title: str = Field(default='', max_length=200)
    base_model_id: str
    analysis_model_id: str | None = None
    character: dict


class StoryGreetingForm(BaseModel):
    base_model_id: str
    character: dict
    scenario: str = ''


class StoryCharacterForm(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ''
    personality: str = ''
    first_mes: str = ''
    mes_example: str = ''
    creator_notes: str = ''


class StoryCreateForm(BaseModel):
    title: str = Field(default='', max_length=200)
    base_model_id: str
    public: bool = False
    scenario: str = ''
    characters: list[StoryCharacterForm] = Field(min_length=1, max_length=20)
    lorebook: dict | None = None


async def _require_base_model(request: Request, base_model_id: str, user) -> None:
    if not await _base_model_exists(request, base_model_id, user):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown base model.')


@router.post('/characters/story/analyze')
async def analyze_story_for_characters(request: Request, form_data: StoryAnalyzeForm, user=Depends(get_admin_user)):
    """Draft characters, a world lorebook and a timeline from a story. Admin only; saves nothing."""
    await _require_character_cards_enabled()
    await _require_base_model(request, form_data.base_model_id, user)
    complete = await _story_completer(request, user, form_data.base_model_id, form_data.analysis_model_id)
    try:
        draft = await analyze_story(form_data.text, complete, form_data.title.strip(), detailed=form_data.detailed)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return dataclasses.asdict(draft)


async def _story_completer(request: Request, user, base_model_id: str, analysis_model_id: str | None):
    """Prompts go to the chosen analysis model, or to the task model for the characters' base model."""
    if analysis_model_id:
        await _require_base_model(request, analysis_model_id, user)

    async def complete(prompt: str) -> str:
        if analysis_model_id:
            return await run_task_prompt(request, user, analysis_model_id, prompt, 'story_analysis', direct=True)
        return await run_task_prompt(request, user, base_model_id, prompt, 'story_analysis')

    return complete


@router.post('/characters/story/profile')
async def write_story_profile(request: Request, form_data: StoryProfileForm, user=Depends(get_admin_user)):
    """A detailed sheet for one drafted character, from the passages they appear in. Saves nothing."""
    await _require_character_cards_enabled()
    await _require_base_model(request, form_data.base_model_id, user)
    complete = await _story_completer(request, user, form_data.base_model_id, form_data.analysis_model_id)
    try:
        return await write_profile(form_data.character, form_data.text, complete, form_data.title.strip())
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post('/characters/story/greeting')
async def draft_story_greeting(request: Request, form_data: StoryGreetingForm, user=Depends(get_admin_user)):
    await _require_character_cards_enabled()
    await _require_base_model(request, form_data.base_model_id, user)
    try:
        reply = await run_task_prompt(
            request,
            user,
            form_data.base_model_id,
            greeting_prompt(form_data.character, form_data.scenario),
            'story_greeting',
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    first_mes = reply.strip()
    if len(first_mes) > 1 and first_mes[0] == first_mes[-1] == '"':
        # The whole reply is wrapped in quotes; quotes inside it are dialogue and stay.
        first_mes = first_mes[1:-1].strip()
    return {'first_mes': first_mes}


@router.post('/characters/story/create', response_model=list[ModelModel])
async def create_characters_from_story(
    request: Request,
    form_data: StoryCreateForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Create the reviewed characters (and their shared world lorebook). Admin only, like card import."""
    await _require_character_cards_enabled()
    await _require_base_model(request, form_data.base_model_id, user)
    access_grants = [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}] if form_data.public else []
    title = form_data.title.strip()

    lorebook = None
    if form_data.lorebook and form_data.lorebook.get('entries'):
        lorebook = await Lorebooks.insert_new_lorebook(
            user.id,
            LorebookForm(
                name=(form_data.lorebook.get('name') or f'{title or "Story"} world')[:256],
                data=form_data.lorebook,
                meta={'source': 'story', 'title': title},
                access_grants=access_grants,
            ),
            db=db,
        )

    created = []
    try:
        for draft in form_data.characters:
            character = CharacterMeta(
                spec_version='owui',
                **draft.model_dump(),
                scenario=form_data.scenario,
                source=[title] if title else [],
            )
            model = await Models.insert_new_model(
                ModelForm(
                    id=_character_model_id(draft.name),
                    base_model_id=form_data.base_model_id,
                    name=draft.name.strip(),
                    meta=ModelMeta(
                        description=draft.creator_notes.strip() or None,
                        character=character,
                        lorebook_ids=[lorebook.id] if lorebook else None,
                    ),
                    params=ModelParams(),
                    access_grants=access_grants,
                ),
                user.id,
                db=db,
            )
            if not model:
                raise HTTPException(status_code=500, detail=f'Could not create {draft.name}.')
            created.append(model)
    except Exception:
        for model in created:
            await Models.delete_model_by_id(model.id, db=db)
        if lorebook is not None:
            await Lorebooks.delete_lorebook_by_id(lorebook.id, db=db)
        raise

    for model in created:
        await publish_event(request, EVENTS.MODEL_CREATED, actor=user, subject_id=model.id, data={'name': model.name})
    return created


async def _read_avatar(model: ModelModel, db: AsyncSession) -> bytes | None:
    file_id = model.meta.character.avatar_file_id
    file = await Files.get_file_by_id(file_id, db=db) if file_id else None
    if file and file.path:
        try:
            path = await asyncio.to_thread(Storage.get_file, file.path)
            with open(path, 'rb') as image:
                return await asyncio.to_thread(image.read, CHARACTER_CARD_MAX_BYTES)
        except OSError:
            log.warning('Character avatar file %s is unreadable; falling back to the thumbnail', file_id)
    url = model.meta.profile_image_url or ''
    if url.startswith('data:image') and ';base64,' in url:
        return base64.b64decode(url.split(',', 1)[1])
    return None


@router.get('/model/character/export')
async def export_character_card(
    id: str,
    format: Literal['png', 'json'] = 'png',
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Export a character model as a V3 card (PNG also carries a V2 projection)."""
    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'write')
    character = model.meta.character
    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', character.name or model.name).strip('_') or 'character'
    headers = {'Content-Disposition': f'attachment; filename="{stem}.{format}"'}
    if character.raw_truncated:
        # The original card was too large to keep; unknown fields from it are not in the export.
        headers['X-Character-Raw-Truncated'] = 'true'

    card_data = character.to_card(model.name)
    lorebook_ids = model.meta.lorebook_ids or []
    lorebooks = await (
        Lorebooks.get_lorebooks_by_ids(lorebook_ids, db=db)
        if user.role == 'admin'
        else Lorebooks.get_readable_lorebooks_by_ids(lorebook_ids, user.id, db=db)
    )
    card_data.character_book = _merged_character_book(
        [card_data.character_book, *[lorebook.as_book() for lorebook in lorebooks]]
    )
    if format == 'json':
        content = json.dumps(character_to_v3(card_data, assets=[]), ensure_ascii=False).encode('utf-8')
        return Response(content=content, media_type='application/json', headers=headers)

    png = await asyncio.to_thread(ensure_png, await _read_avatar(model, db))
    content = await asyncio.to_thread(write_card_png, png, character_to_v3(card_data))
    return Response(content=content, media_type='image/png', headers=headers)


@router.get('/model/character/greetings', response_model=list[str])
async def get_character_greetings(
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The first message and alternate greetings, with macros expanded for the requesting user."""
    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'read')
    character = model.meta.character
    ctx = build_macro_context(character, model_name=model.name, user_name=user.name)
    return character_greetings(character, ctx)


@router.get('/model/character/expression')
async def get_character_expression(
    id: str,
    name: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """One expression image of a character the user may chat with."""
    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'read')
    file_id = model.meta.character.expressions.get(clean_expression_name(name))
    file = await Files.get_file_by_id(file_id, db=db) if file_id else None
    if not file or not file.path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    try:
        path = await asyncio.to_thread(Storage.get_file, file.path)
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND) from error
    content_type = (file.meta or {}).get('content_type') or 'image/webp'
    if content_type not in PROFILE_IMAGE_ALLOWED_MIME_TYPES:
        content_type = 'application/octet-stream'
    return FileResponse(
        path,
        media_type=content_type,
        headers={'Cache-Control': 'private, max-age=86400', 'X-Content-Type-Options': 'nosniff'},
    )


class CharacterExpressionsResponse(BaseModel):
    expressions: list[str]
    skipped: list[str] = []


async def _save_character_meta(model: ModelModel, db: AsyncSession) -> None:
    form = ModelForm(**{**model.model_dump(), 'meta': model.meta})
    if not await Models.update_model_by_id(model.id, form, db=db):
        raise HTTPException(status_code=500, detail='Could not update the character.')


async def _write_expressions(model_id: str, changes: dict[str, str | None], db: AsyncSession):
    """Apply ``{name: file id}`` (``None`` removes) to the character as stored now and save it.

    Image work is slow, and the editor may save the character meanwhile; reading the model again
    right before writing keeps both changes. Returns ``(names, replaced file ids, names not added)``.
    """
    # A new session: the request's own session would return the copy it loaded earlier.
    model = await Models.get_model_by_id(model_id)
    if model is None or model.meta.character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    expressions = model.meta.character.expressions
    replaced: list[str] = []
    rejected: list[str] = []
    for name, file_id in changes.items():
        if file_id is None:
            if name in expressions:
                replaced.append(expressions.pop(name))
            continue
        if name in expressions:
            replaced.append(expressions[name])
        elif len(expressions) >= MAX_EXPRESSIONS:
            rejected.append(name)
            continue
        expressions[name] = file_id
    if changes and len(rejected) < len(changes):
        await _save_character_meta(model, db)
    return sorted(expressions), replaced, rejected


def _expression_files(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """``(filename, bytes)`` for an uploaded image, or every image in an uploaded sprite-pack zip."""
    if data.startswith(ZIP_SIGNATURE):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            raise HTTPException(status_code=400, detail=f'{name}: not a valid zip file.') from error
        entries = [info for info in archive.infolist() if not info.is_dir() and info.file_size <= 20 * 1024 * 1024]
        if sum(info.file_size for info in entries) > CHARACTER_CARD_MAX_BYTES:
            raise HTTPException(status_code=400, detail=f'{name}: the archive is too large.')
        return [(info.filename, archive.read(info)) for info in entries if not info.filename.startswith('__MACOSX')]
    return [(name, data)]


@router.post('/model/character/expressions', response_model=CharacterExpressionsResponse)
async def upload_character_expressions(
    request: Request,
    id: str,
    files: list[UploadFile] = File(...),
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Add or replace expression images: each image is named by its file name (``joy.png`` is
    "joy"); a zip adds every image in it, as SillyTavern sprite packs are shared."""
    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'write')
    uploads: list = []
    skipped: list[str] = []
    changes: dict[str, str] = {}
    try:
        for upload in files:
            data = await upload.read(CHARACTER_CARD_MAX_BYTES + 1)
            if len(data) > CHARACTER_CARD_MAX_BYTES:
                raise HTTPException(status_code=400, detail=f'{upload.filename}: the file is too large.')
            for filename, image in _expression_files(upload.filename or 'image', data):
                name = expression_name_from_filename(filename)
                if not sniff_image_type(image) or not name:
                    skipped.append(filename)
                    continue
                if (
                    name not in model.meta.character.expressions
                    and name not in changes
                    and len(model.meta.character.expressions) + len(changes) >= MAX_EXPRESSIONS
                ):
                    skipped.append(filename)
                    continue
                stored = await _store_expression(request, name, image, user, db, uploads)
                if stored is None:
                    skipped.append(filename)
                    continue
                if stored[0] in changes:
                    # The same name twice in one upload: the later image wins.
                    await _discard_file_ids([changes[stored[0]]], db)
                changes[stored[0]] = stored[1]
        names, replaced, rejected = await _write_expressions(model.id, changes, db)
    except Exception:
        await _discard_uploads(uploads, db)
        raise
    await _discard_file_ids([*replaced, *(changes[name] for name in rejected)], db)
    skipped += rejected
    return CharacterExpressionsResponse(expressions=names, skipped=skipped)


@router.delete('/model/character/expression', response_model=CharacterExpressionsResponse)
async def delete_character_expression(
    id: str,
    name: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'write')
    names, replaced, _ = await _write_expressions(model.id, {clean_expression_name(name): None}, db)
    await _discard_file_ids(replaced, db)
    return CharacterExpressionsResponse(expressions=names)


class ExpressionClassifyForm(BaseModel):
    model_id: str
    text: str = Field(max_length=200_000)


@router.post('/model/character/expression/classify')
async def classify_character_expression(
    request: Request,
    form_data: ExpressionClassifyForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The character's expression that best fits a reply, picked by the task model."""
    await _require_character_cards_enabled()
    model = await _get_character_model(form_data.model_id, user, db, 'read')
    character = model.meta.character
    names = sorted(character.expressions)
    if not names or not form_data.text.strip():
        return {'name': None}
    if len(names) == 1:
        return {'name': names[0]}
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)
    prompt = classify_prompt(character.display_name(model.name), names, form_data.text)
    # Without a separate task model the task goes to the character model itself.
    skip_character_system_prompt(request, model.id)
    try:
        reply = await run_task_prompt(request, user, model.id, prompt, 'character_expression')
    except Exception as error:
        log.info('Expression classification failed for %s: %s', model.id, error)
        return {'name': None}
    return {'name': pick_expression(reply, names)}


class ExpressionGenerateForm(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    style: str = Field(default='', max_length=300)


async def _generated_image_bytes(image: dict, db: AsyncSession) -> bytes:
    file = await Files.get_file_by_id(image.get('id') or '', db=db)
    if not file or not file.path:
        raise HTTPException(status_code=502, detail='The image generator returned no image.')
    path = await asyncio.to_thread(Storage.get_file, file.path)
    with open(path, 'rb') as handle:
        return await asyncio.to_thread(handle.read, CHARACTER_CARD_MAX_BYTES)


@router.post('/model/character/expressions/generate', response_model=CharacterExpressionsResponse)
async def generate_character_expression(
    request: Request,
    id: str,
    form_data: ExpressionGenerateForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Draw one expression image. With image editing set up and an avatar, the avatar is redrawn
    with the expression (keeps the look and art style); otherwise the image generator draws a
    portrait from a prompt the task model writes from the character's description."""
    from open_webui.routers.images import (
        CreateImageForm,
        EditImageForm,
        get_image_config,
        image_edits,
        image_generations,
    )

    await _require_character_cards_enabled()
    model = await _get_character_model(id, user, db, 'write')
    character = model.meta.character
    name = clean_expression_name(form_data.name)
    if not name:
        raise HTTPException(status_code=400, detail='Give the expression a name.')
    if name not in character.expressions and len(character.expressions) >= MAX_EXPRESSIONS:
        raise HTTPException(status_code=400, detail='This character has the most expressions allowed.')

    image_config = await get_image_config()
    if user.role != 'admin' and not await has_permission(
        user.id, 'features.image_generation', image_config.USER_PERMISSIONS, db=db
    ):
        raise HTTPException(status_code=403, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
    avatar = await _read_avatar(model, db) if image_config.ENABLE_IMAGE_EDIT else None
    if avatar:
        content_type = sniff_image_type(avatar) or 'image/png'
        data_url = f'data:{content_type};base64,{base64.b64encode(avatar).decode()}'
        images = await image_edits(
            request, EditImageForm(image=data_url, prompt=sprite_edit_prompt(name)), metadata={}, user=user
        )
    elif image_config.ENABLE_IMAGE_GENERATION:
        if not request.app.state.MODELS:
            await get_all_models(request, user=user)
        ctx = build_macro_context(character, model_name=model.name, user_name=user.name)
        skip_character_system_prompt(request, model.id)
        try:
            reply = await run_task_prompt(
                request, user, model.id, sprite_prompt(character, ctx, name, form_data.style), 'character_sprite'
            )
        except Exception as error:
            raise HTTPException(status_code=502, detail='The model did not answer.') from error
        prompt = clean_image_prompt(reply)
        if not prompt:
            raise HTTPException(status_code=502, detail='The model returned an empty prompt.')
        images = await image_generations(request, CreateImageForm(prompt=prompt), metadata={}, user=user)
    else:
        raise HTTPException(status_code=400, detail='Image generation is not set up.')
    if not images:
        raise HTTPException(status_code=502, detail='The image generator returned no image.')

    uploads: list = []
    try:
        image = await _generated_image_bytes(images[0], db)
        stored = await _store_expression(request, name, image, user, db, uploads)
        if stored is None:
            raise HTTPException(status_code=502, detail='The image generator returned an unreadable image.')
        names, replaced, rejected = await _write_expressions(model.id, {stored[0]: stored[1]}, db)
        if rejected:
            raise HTTPException(status_code=400, detail='This character has the most expressions allowed.')
    except Exception:
        await _discard_uploads(uploads, db)
        raise
    finally:
        # The generator's own copies; the expression keeps a normalized one.
        await _discard_file_ids([image.get('id') for image in images if image.get('id')], db)
    await _discard_file_ids(replaced, db)
    return CharacterExpressionsResponse(expressions=names)


async def _discard_file_ids(file_ids: list[str], db: AsyncSession) -> None:
    for file_id in file_ids:
        file = await Files.get_file_by_id(file_id, db=db)
        if file:
            await _discard_uploads([file], db)


############################
# SyncModels
############################


class SyncModelsForm(BaseModel):
    models: list[ModelModel] = []


@router.post('/sync', response_model=list[ModelModel])
async def sync_models(
    request: Request,
    form_data: SyncModelsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    existing = {model.id: model for model in await Models.get_models_by_ids([m.id for m in form_data.models], db=db)}
    for model in form_data.models:
        previous = existing.get(model.id)
        await _check_model_controls(model, previous, user, request)
        if previous and 'voice_avatar' not in model.meta.model_fields_set:
            model.meta.voice_avatar = previous.meta.voice_avatar
        await _verify_voice_avatar(model.meta.voice_avatar, user, db, previous.meta.voice_avatar if previous else None)
        if previous and 'background_image_url' not in model.meta.model_fields_set:
            model.meta.background_image_url = previous.meta.background_image_url
        await _verify_background_image(
            model.meta.background_image_url, user, db, previous.meta.background_image_url if previous else None
        )
        _keep_character_files(model.meta, previous.meta if previous else None)
    models = await Models.sync_models(user.id, form_data.models, db=db)
    await publish_event(
        request,
        EVENTS.MODEL_SYNCED,
        actor=user,
        subject_type='model',
        data={'count': len(models), 'model_ids': [model.id for model in models]},
    )
    return models


###########################
# GetModelById
###########################


class ModelIdForm(BaseModel):
    id: str


# Note: We're not using the typical url path param here, but instead using a query parameter to allow '/' in the id
@router.get('/model', response_model=ModelAccessResponse | None)
async def get_model_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    model = await Models.get_model_by_id(id, db=db)
    if model:
        write_access = (
            (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
            or user.id == model.user_id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='model',
                resource_id=model.id,
                permission='write',
                db=db,
            )
        )

        if write_access or await AccessGrants.has_access(
            user_id=user.id,
            resource_type='model',
            resource_id=model.id,
            permission='read',
            db=db,
        ):
            model_dict = model_response(model)
            model_dict = add_chat_variables_schema(model_dict)
            # Strip params (system prompt and other admin-curated config)
            # for read-only callers — matches the params strip already
            # enforced on /api/models in utils/models.py.  Owners, admins
            # under BYPASS_ADMIN_ACCESS_CONTROL, and write-grant holders
            # still receive the full object so the workspace edit UI keeps
            # working for users who legitimately curate the model.
            if not write_access:
                hide_curated_config(model_dict)
            return ModelAccessResponse(
                **model_dict,
                write_access=write_access,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


###########################
# GetModelById
###########################


async def authorized_model_history(id, user, db):
    model = await Models.get_model_by_id(id, db=db)
    if not model:
        raise HTTPException(404, ERROR_MESSAGES.NOT_FOUND)
    if not (
        user.id == model.user_id
        or (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
        or await AccessGrants.has_access(
            user_id=user.id, resource_type='model', resource_id=id, permission='write', db=db
        )
    ):
        raise HTTPException(403, ERROR_MESSAGES.ACCESS_PROHIBITED)
    return model


@router.get('/model/history', response_model=list[ModelHistoryResponse])
async def get_model_history(
    id: str,
    page: int = 1,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await authorized_model_history(id, user, db)
    return await ModelHistories.get_history_by_model_id(id, page, db=db)


@router.get('/model/history/{history_id}', response_model=ModelHistoryModel)
async def get_model_history_entry(
    id: str,
    history_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await authorized_model_history(id, user, db)
    entry = await ModelHistories.get_history_by_id(id, history_id, db=db)
    if not entry:
        raise HTTPException(404, 'Model version not found')
    return entry


@router.delete('/model/history/{history_id}', response_model=bool)
async def delete_model_history_entry(
    id: str,
    history_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await authorized_model_history(id, user, db)
    if not await ModelHistories.delete_history_entry(id, history_id, db):
        raise HTTPException(404, 'Model version not found')
    return True


async def _verify_version_dependencies(request, form, user, db):
    from open_webui.models.functions import Functions
    from open_webui.models.knowledge import Knowledges
    from open_webui.models.skills import Skills
    from open_webui.routers.terminals import list_terminal_servers
    from open_webui.routers.tools import get_tools

    async def require_resource(resource_type, resource_id, resource):
        if not resource or getattr(resource, 'is_active', True) is False:
            raise HTTPException(400, f'Referenced {resource_type} is unavailable: {resource_id}')
        if not (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL) and resource.user_id != user.id:
            if not await AccessGrants.has_access(
                user_id=user.id, resource_type=resource_type, resource_id=resource_id, permission='read', db=db
            ):
                raise HTTPException(403, f'Referenced {resource_type} is not accessible: {resource_id}')

    if form.base_model_id:
        available = await get_all_models(request, user=user)
        if not any(model['id'] == form.base_model_id for model in available):
            raise HTTPException(400, 'The base model is unavailable.')
    for item in form.meta.knowledge or []:
        if not isinstance(item, dict) or not item.get('id') or item.get('legacy'):
            continue
        if item.get('type') == 'file':
            file = await Files.get_file_by_id(item['id'], db=db)
            if not file or not (user.role == 'admin' or await has_access_to_file(file.id, 'read', user, db=db)):
                raise HTTPException(400, 'A referenced knowledge file is missing or inaccessible.')
        else:
            await require_resource('knowledge', item['id'], await Knowledges.get_knowledge_by_id(item['id'], db=db))
    for skill_id in getattr(form.meta, 'skillIds', None) or []:
        await require_resource('skill', skill_id, await Skills.get_skill_by_id(skill_id, db=db))
    tool_ids = set(getattr(form.meta, 'toolIds', None) or [])
    if tool_ids:
        available_tools = {tool.id for tool in await get_tools(request, user=user, db=db)}
        if not tool_ids.issubset(available_tools):
            raise HTTPException(400, 'A referenced tool is missing or inaccessible.')
    for key in ('filterIds', 'defaultFilterIds', 'actionIds'):
        for function_id in getattr(form.meta, key, None) or []:
            function = await Functions.get_function_by_id(function_id, db=db)
            if not function or not function.is_active:
                raise HTTPException(400, f'A referenced function is unavailable: {function_id}')
    terminal_id = getattr(form.meta, 'terminalId', None)
    if terminal_id and terminal_id not in {t['id'] for t in await list_terminal_servers(request, user=user)}:
        raise HTTPException(400, 'The referenced terminal is missing or inaccessible.')


class ModelVersionForm(BaseModel):
    version_id: str


@router.post('/model/update/version', response_model=ModelModel)
async def set_model_version(
    request: Request,
    id: str,
    form_data: ModelVersionForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    model = await authorized_model_history(id, user, db)
    entry = await ModelHistories.get_history_by_id(id, form_data.version_id, db=db)
    if not entry:
        raise HTTPException(404, 'Model version not found')
    form = ModelForm(id=id, **deepcopy(entry.snapshot))
    # A missing historical controls key means an empty configuration, not "preserve current".
    form.params.model_fields_set.add('model_controls')
    if user.role != 'admin' and model.base_model_id and not form.base_model_id:
        raise HTTPException(403, ERROR_MESSAGES.ACCESS_PROHIBITED)
    await _check_model_controls(form, model, user, request)
    await _verify_version_dependencies(request, form, user, db)
    await _verify_background_image(form.meta.background_image_url, user, db)
    await _verify_voice_avatar(form.meta.voice_avatar, user, db)
    result = await Models.update_model_by_id(id, form, db=db, user_id=user.id, production_version_id=entry.id)
    if result is None:
        raise HTTPException(404, ERROR_MESSAGES.NOT_FOUND)
    await publish_event(
        request,
        EVENTS.MODEL_UPDATED,
        actor=user,
        subject_id=id,
        data={'name': result.name, 'version_id': result.version_id},
    )
    return model_response(result)


@router.get('/model/profile/image')
async def get_model_profile_image(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    profile_image_url = None
    updated_at = None

    bypass_access_control = BYPASS_MODEL_ACCESS_CONTROL or (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)

    # First, check the database for regular models
    model_meta = await Models.get_model_meta_by_id(id, db=db)
    if model_meta:
        meta, model_user_id, model_updated_at = model_meta
        # Denied callers get the default image rather than an error, so model ids stay unprobeable.
        if (
            bypass_access_control
            or user.id == model_user_id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='model',
                resource_id=id,
                permission='read',
                db=db,
            )
        ):
            profile_image_url = (meta or {}).get('profile_image_url')
            updated_at = model_updated_at

    # Fallback: check arena models stored in config (not in the DB)
    if not profile_image_url:
        arena_models = await Config.get('evaluation.arena.models', []) or []
        for arena_model in arena_models:
            if arena_model.get('id') == id:
                arena_meta = arena_model.get('meta', {})
                access_grants = arena_meta.get('access_grants', [])
                if (
                    bypass_access_control
                    or (not access_grants and user.role == 'admin')
                    or await has_access(user.id, permission='read', access_grants=access_grants, db=db)
                ):
                    profile_image_url = arena_meta.get('profile_image_url')
                break

    if profile_image_url:
        if profile_image_url.startswith('http'):
            if ENABLE_PROFILE_IMAGE_URL_FORWARDING:
                return Response(
                    status_code=status.HTTP_302_FOUND,
                    headers={'Location': profile_image_url},
                )
            # When forwarding is disabled, fall through to the
            # default image to prevent client-side IP/UA/Referer
            # leaks via 302 redirect to external origins.
        elif profile_image_url.startswith('data:image'):
            try:
                header, base64_data = profile_image_url.split(',', 1)
                image_data = base64.b64decode(base64_data)
                image_buffer = io.BytesIO(image_data)
                media_type = header.split(';')[0].lstrip('data:').lower()

                # only serve known-safe raster types inline; reject SVG/unknown (can run script on our origin)
                if media_type not in PROFILE_IMAGE_ALLOWED_MIME_TYPES:
                    # LICENSE covers this Open WebUI fallback logo.
                    # Do not alter, remove, obscure, or replace it except as LICENSE permits:
                    # https://docs.openwebui.com/license.
                    return RedirectResponse(
                        url='/static/favicon.png',
                        status_code=status.HTTP_302_FOUND,
                    )

                headers = {
                    'Content-Disposition': 'inline',
                    'X-Content-Type-Options': 'nosniff',
                }
                if updated_at:
                    headers['ETag'] = f'"{updated_at}"'

                return StreamingResponse(
                    image_buffer,
                    media_type=media_type,
                    headers=headers,
                )
            except Exception:
                pass
        else:
            safe_static = _safe_static_redirect_path(profile_image_url)
            if safe_static:
                return RedirectResponse(
                    url=safe_static,
                    status_code=status.HTTP_302_FOUND,
                )

    # LICENSE covers this Open WebUI fallback logo.
    # Do not alter, remove, obscure, or replace it except as LICENSE permits:
    # https://docs.openwebui.com/license.
    return RedirectResponse(
        url='/static/favicon.png',
        status_code=status.HTTP_302_FOUND,
    )


############################
# ToggleModelById
############################


@router.post('/model/toggle', response_model=ModelResponse | None)
async def toggle_model_by_id(
    request: Request, id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    model = await Models.get_model_by_id(id, db=db)
    if model:
        if (
            user.role == 'admin'
            or model.user_id == user.id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='model',
                resource_id=model.id,
                permission='write',
                db=db,
            )
        ):
            model = await Models.toggle_model_by_id(id, db=db)

            if model:
                await publish_event(
                    request,
                    EVENTS.MODEL_ENABLED if model.is_active else EVENTS.MODEL_DISABLED,
                    actor=user,
                    subject_id=model.id,
                    subject_type='model',
                    data={'name': model.name},
                )
                return model_response(model)
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT('Error updating function'),
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# UpdateModelById
############################


@router.post('/model/update', response_model=ModelModel | None)
async def update_model_by_id(
    request: Request,
    form_data: ModelForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Update a workspace model's configuration."""
    model = await Models.get_model_by_id(form_data.id, db=db)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        model.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='model',
            resource_id=model.id,
            permission='write',
            db=db,
        )
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    await _verify_knowledge_file_access(
        getattr(form_data.meta, 'knowledge', None) if form_data.meta else None,
        user,
        db,
    )

    if 'base_model_id' not in form_data.model_fields_set:
        form_data.base_model_id = model.base_model_id
    if form_data.base_model_id == form_data.id:
        # Should never be stored: a model cannot be based on itself.
        form_data.base_model_id = None

    if user.role != 'admin' and model.base_model_id and not form_data.base_model_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    if 'profile_image_url' not in form_data.meta.model_fields_set:
        form_data.meta.profile_image_url = model.meta.profile_image_url

    if 'voice_avatar' not in form_data.meta.model_fields_set:
        form_data.meta.voice_avatar = model.meta.voice_avatar
    await _verify_voice_avatar(form_data.meta.voice_avatar, user, db, model.meta.voice_avatar)
    if 'background_image_url' not in form_data.meta.model_fields_set:
        form_data.meta.background_image_url = model.meta.background_image_url
    await _verify_background_image(form_data.meta.background_image_url, user, db, model.meta.background_image_url)
    _keep_character_files(form_data.meta, model.meta)

    if form_data.access_grants is not None:
        # The editor resends every stored grant, so re-checking them would strip sharing this user cannot re-create.
        existing_access_grants = {
            (grant.principal_type, grant.principal_id, grant.permission) for grant in model.access_grants
        }
        submitted_access_grants_map = {
            (grant['principal_type'], grant['principal_id'], grant['permission']): grant
            for grant in normalize_access_grants(form_data.access_grants)
        }
        preserved_access_grants = [
            grant for key, grant in submitted_access_grants_map.items() if key in existing_access_grants
        ]
        new_access_grants = [
            grant for key, grant in submitted_access_grants_map.items() if key not in existing_access_grants
        ]

        form_data.access_grants = preserved_access_grants + await filter_allowed_access_grants(
            await Config.get('user.permissions'),
            user.id,
            user.role,
            new_access_grants,
            'sharing.public_models',
        )

    await _check_model_controls(form_data, model, user, request)
    model = await Models.update_model_by_id(form_data.id, form_data, db=db, user_id=user.id)
    if model:
        await publish_event(
            request,
            EVENTS.MODEL_UPDATED,
            actor=user,
            subject_id=model.id,
            data={'name': model.name},
        )
    return model_response(model)


############################
# UpdateModelAccessById
############################


class ModelAccessGrantsForm(BaseModel):
    id: str = Field(pattern=r'^\S+$')
    name: str | None = None
    access_grants: list[dict]


@router.post('/model/access/update', response_model=ModelModel | None)
async def update_model_access_by_id(
    request: Request,
    form_data: ModelAccessGrantsForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    model = await Models.get_model_by_id(form_data.id, db=db)

    # Non-preset models (e.g. direct Ollama/OpenAI models) may not have a DB
    # entry yet. Create a minimal one so access grants can be stored.
    if not model:
        if user.role != 'admin':
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        model = await Models.insert_new_model(
            ModelForm(
                id=form_data.id,
                name=form_data.name or form_data.id,
                meta=ModelMeta(),
                params=ModelParams(),
            ),
            user.id,
            db=db,
        )
        if not model:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT('Error creating model entry'),
            )

    if (
        model.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='model',
            resource_id=model.id,
            permission='write',
            db=db,
        )
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_models',
    )

    await AccessGrants.set_access_grants('model', form_data.id, form_data.access_grants, db=db)

    await Models.update_model_updated_at_by_id(form_data.id, db=db)

    model = await Models.get_model_by_id(form_data.id, db=db)
    await publish_event(
        request,
        EVENTS.MODEL_ACCESS_UPDATED,
        actor=user,
        subject_id=form_data.id,
    )
    return model_response(model)


############################
# DeleteModelById
############################


@router.post('/model/delete', response_model=bool)
async def delete_model_by_id(
    request: Request,
    form_data: ModelIdForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    model = await Models.get_model_by_id(form_data.id, db=db)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        user.role != 'admin'
        and model.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='model',
            resource_id=model.id,
            permission='write',
            db=db,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    result = await Models.delete_model_by_id(form_data.id, db=db)
    if result:
        await publish_event(
            request,
            EVENTS.MODEL_DELETED,
            actor=user,
            subject_id=form_data.id,
            data={'name': model.name},
        )
    return result


@router.delete('/delete/all', response_model=bool)
async def delete_all_models(
    request: Request, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)
):
    result = await Models.delete_all_models(db=db)
    if result:
        await publish_event(request, EVENTS.MODEL_DELETED, actor=user, subject_type='model')
    return result
