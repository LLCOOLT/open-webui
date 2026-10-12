from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from open_webui.config import (
    CONTEXT_COMPACTION_RETENTION_PERCENTAGE,
    CONTEXT_COMPACTION_TOKEN_THRESHOLD,
    ENABLE_ADMIN_CHAT_ACCESS,
    ENABLE_ADMIN_EXPORT,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import (
    AggregateChatStats,
    ChatBody,
    ChatForm,
    ChatHistoryStats,
    ChatImportForm,
    ChatResponse,
    Chats,
    ChatsImportForm,
    ChatStatsExport,
    ChatTitleIdResponse,
    ChatUsageStatsListResponse,
    MessageStats,
    chat_search_content_query,
    chat_search_terms,
)
from open_webui.models.config import Config
from open_webui.models.folders import Folders
from open_webui.models.lorebooks import Lorebooks
from open_webui.models.shared_chats import ChatShareMode, ShareChatForm, SharedChatResponse, SharedChats
from open_webui.models.tags import TagModel, Tags
from open_webui.socket.main import get_event_emitter
from open_webui.tasks import get_response_streams_by_chat_id, has_active_tasks, stop_item_tasks
from open_webui.utils.access_control import filter_allowed_access_grants, has_permission
from open_webui.utils.access_control.folders import has_folder_write_access
from open_webui.utils.auth import bearer_security, get_admin_user, get_current_user, get_verified_user
from open_webui.utils.chat_fork import build_fork_history
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.context_compaction import compact_chat_branch, get_chat_context_usage
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_message_list
from open_webui.utils.models import get_all_models
from open_webui.utils.roleplay.assist import (
    clean_image_prompt,
    clean_impersonation,
    impersonate_prompt,
    lore_prompt,
    parse_lore_suggestions,
    scene_prompt,
    transcript,
)
from open_webui.utils.roleplay.character import DepthPrompt
from open_webui.utils.roleplay.filter import get_model_character, skip_character_system_prompt
from open_webui.utils.roleplay.memory import (
    branch_message_ids,
    latest_memory,
    memory_enabled_for_chat,
    memory_overrides,
)
from open_webui.utils.roleplay.persona import persona_description, resolve_persona
from open_webui.utils.roleplay.prompt import build_macro_context
from open_webui.utils.roleplay.task_model import run_task_prompt
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()

CHAT_CONFIG_KEYS = {
    'CONTEXT_COMPACTION_MODEL': 'chat.context_compaction.model',
    'ENABLE_CONTEXT_COMPACTION': 'chat.context_compaction.enable',
    'CONTEXT_COMPACTION_TOKEN_THRESHOLD': 'chat.context_compaction.token_threshold',
    'CONTEXT_COMPACTION_TOKEN_CAP': 'chat.context_compaction.token_cap',
    'CONTEXT_COMPACTION_RETENTION_PERCENTAGE': 'chat.context_compaction.retention_percentage',
    'CONTEXT_COMPACTION_PROMPT_TEMPLATE': 'chat.context_compaction.prompt_template',
    'ENABLE_TOOL_PERMISSIONS': 'chat.tool_permissions.enable',
    'ENABLE_TOOL_SEARCH': 'chat.tool_search.enable',
    'TOOL_SEARCH_DEFER_THRESHOLD': 'chat.tool_search.defer_threshold',
    'TOOL_SEARCH_ALWAYS_LOADED': 'chat.tool_search.always_loaded',
    'TOOL_SEARCH_DEFER_BUILTIN_TOOLS': 'chat.tool_search.defer_builtin_tools',
}


def overlay_response_streams(chat_data: dict, response_streams: list[dict]) -> dict:
    if not response_streams:
        return chat_data

    messages = chat_data.get('chat', {}).get('history', {}).get('messages')
    if isinstance(messages, dict):
        for stream in response_streams:
            message_id = stream.get('message_id')
            message = messages.get(message_id)
            if isinstance(message, dict):
                message['content'] = stream.get('content', '')
                message['output'] = stream.get('output') or []
                message['done'] = False

    legacy_messages = chat_data.get('chat', {}).get('messages')
    if isinstance(legacy_messages, list):
        streams_by_message_id = {stream.get('message_id'): stream for stream in response_streams}
        for message in legacy_messages:
            if isinstance(message, dict) and (stream := streams_by_message_id.get(message.get('id'))):
                message['content'] = stream.get('content', '')
                message['output'] = stream.get('output') or []
                message['done'] = False

    return chat_data


async def get_optional_verified_user(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_token: HTTPAuthorizationCredentials | None = Depends(bearer_security),
):
    try:
        user = await get_current_user(request, response, background_tasks, auth_token)
    except HTTPException:
        return None

    if user.role not in {'user', 'admin'}:
        return None
    return user


async def is_open_shared_chat(shared, db: AsyncSession) -> bool:
    return await AccessGrants.has_anyone_access(
        resource_type='shared_chat',
        resource_id=shared.chat_id,
        permission='read',
        db=db,
    )


async def can_read_shared_chat(user, shared, db: AsyncSession) -> bool:
    if user.role == 'pending':
        return False
    if user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS:
        return True
    if shared.user_id == user.id:
        return True
    return await AccessGrants.has_access(
        user_id=user.id,
        resource_type='shared_chat',
        resource_id=shared.chat_id,
        permission='read',
        db=db,
    )


async def shared_chat_response(chat, user=None, db=None):
    from open_webui.models.users import Users

    data = ChatResponse.model_validate(chat, from_attributes=True).model_dump()
    if user is None or chat.user_id != user.id:
        data['variables'] = {}
        for key in ('params', 'tool_servers', 'tool_ids', 'filter_ids', 'variables'):
            data['chat'].pop(key, None)
    messages = list((data['chat'].get('history', {}).get('messages') or {}).values())
    messages.extend(data['chat'].get('messages') or [])
    authors = {}
    for message in messages:
        if message.get('role') == 'user':
            author = message.get('user')
            author = author if isinstance(author, dict) else {}
            author_id = message.get('user_id') or author.get('id') or chat.user_id
            message['user_id'] = author_id
            if author.get('id') == author_id and author.get('name'):
                authors[author_id] = {'id': author_id, 'name': author['name']}
        if user is None or (message.get('user_id') or chat.user_id) != user.id:
            message.pop('meta', None)
    missing_ids = {message['user_id'] for message in messages if message.get('role') == 'user'} - authors.keys()
    if missing_ids:
        for author in await Users.get_users_by_user_ids(list(missing_ids), db=db):
            authors[author.id] = {'id': author.id, 'name': author.name}
    for message in messages:
        if message.get('role') == 'user':
            message['user'] = authors.get(message['user_id'], {'id': message['user_id'], 'name': ''})
    return data


async def add_active_state_to_chat_list(
    request: Request, chat_list: list[ChatTitleIdResponse]
) -> list[ChatTitleIdResponse]:
    for chat in chat_list:
        chat.active = False
        if not await has_active_tasks(request.app.state.redis, chat.id):
            continue

        chat.active = await ChatMessages.has_unfinished_assistant_by_chat_id(chat.id)

    return chat_list


async def get_folder_unread_counts(user_id: str, db: AsyncSession | None = None) -> dict[str, int]:
    user_folders = await Folders.get_folders_by_user_id(user_id, db=db)
    parent_by_id = {folder.id: folder.parent_id for folder in user_folders}
    unread_counts = dict.fromkeys(parent_by_id.keys(), 0)
    direct_unread_counts = await Chats.count_unread_by_folder_ids(user_id, list(parent_by_id.keys()), db=db)

    for unread_folder_id, unread_count in direct_unread_counts.items():
        current_id = unread_folder_id
        seen = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            if current_id in unread_counts:
                unread_counts[current_id] += unread_count
            current_id = parent_by_id.get(current_id)

    return unread_counts


class ChatConfigForm(BaseModel):
    CONTEXT_COMPACTION_MODEL: str | None = ''
    ENABLE_CONTEXT_COMPACTION: bool
    CONTEXT_COMPACTION_TOKEN_THRESHOLD: int | None = None
    CONTEXT_COMPACTION_TOKEN_CAP: int | None = None
    CONTEXT_COMPACTION_RETENTION_PERCENTAGE: int | None = None
    CONTEXT_COMPACTION_PROMPT_TEMPLATE: str
    ENABLE_TOOL_PERMISSIONS: bool = False
    ENABLE_TOOL_SEARCH: bool = False
    TOOL_SEARCH_DEFER_THRESHOLD: int = 400
    TOOL_SEARCH_ALWAYS_LOADED: list[str] = []
    TOOL_SEARCH_DEFER_BUILTIN_TOOLS: bool = True


class CompactChatForm(BaseModel):
    model: str | None = None


def chat_search_content_text(text: str) -> str:
    return chat_search_content_query(text)


def chat_search_snippet(chat: dict, search_text: str, max_length: int = 200) -> str | None:
    if not search_text:
        return None

    history = chat.get('history', {})
    messages = history.get('messages') if isinstance(history, dict) else None
    if not messages:
        messages = chat.get('messages', []) or []
    if isinstance(messages, dict):
        messages = messages.values()

    needles = list(dict.fromkeys([search_text, *chat_search_terms(search_text)]))
    for needle in needles:
        for message in messages:
            if not isinstance(message, dict):
                continue

            content = message.get('content')
            if not isinstance(content, str):
                continue

            index = content.lower().find(needle)
            if index == -1:
                continue

            start = max(index - max_length // 2, 0)
            end = min(start + max_length, len(content))
            if index + len(needle) > end:
                end = min(index + len(needle), len(content))
                start = max(end - max_length, 0)

            snippet = ' '.join(content[start:end].split())
            return f'{"..." if start else ""}{snippet}{"..." if end < len(content) else ""}'

    return None


async def get_chat_config_values() -> dict:
    values = await Config.get_many(*CHAT_CONFIG_KEYS.values())
    config = {field: values[storage_key] for field, storage_key in CHAT_CONFIG_KEYS.items() if storage_key in values}
    if config.get('CONTEXT_COMPACTION_MODEL') is None:
        config['CONTEXT_COMPACTION_MODEL'] = ''
    if config.get('CONTEXT_COMPACTION_TOKEN_CAP') is None:
        config['CONTEXT_COMPACTION_TOKEN_CAP'] = config.get('CONTEXT_COMPACTION_TOKEN_THRESHOLD', 80000)
    if config.get('CONTEXT_COMPACTION_RETENTION_PERCENTAGE') is None:
        config['CONTEXT_COMPACTION_RETENTION_PERCENTAGE'] = 40
    return config


def chat_config_updates(data: dict) -> dict:
    return {CHAT_CONFIG_KEYS[field]: value for field, value in data.items() if field in CHAT_CONFIG_KEYS}


async def require_chat_import_permission(request: Request, user, db: AsyncSession):
    if user.role != 'admin' and not await has_permission(
        user.id, 'chat.import', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


############################
# GetChatList
# Let the record outlive the session, so that what was
# learned here not need to be learned again.
############################


@router.get('/', response_model=list[ChatTitleIdResponse])
@router.get('/list', response_model=list[ChatTitleIdResponse])
async def get_session_user_chat_list(
    request: Request,
    user=Depends(get_verified_user),
    page: int | None = None,
    include_pinned: bool | None = False,
    include_folders: bool | None = False,
    sort_by: str = 'updated_at',
    sort_dir: str = 'desc',
    db: AsyncSession = Depends(get_async_session),
):
    try:
        if page is not None:
            limit = 60
            skip = (page - 1) * limit

            chats = await Chats.get_chat_title_id_list_by_user_id(
                user.id,
                include_folders=include_folders,
                include_pinned=include_pinned,
                sort_by=sort_by,
                sort_dir=sort_dir,
                skip=skip,
                limit=limit,
                db=db,
            )
        else:
            chats = await Chats.get_chat_title_id_list_by_user_id(
                user.id,
                include_folders=include_folders,
                include_pinned=include_pinned,
                sort_by=sort_by,
                sort_dir=sort_dir,
                db=db,
            )
        return await add_active_state_to_chat_list(request, chats)
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


@router.post('/read')
async def mark_chats_read_by_user_id(
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    return {
        'updated_count': await Chats.mark_chats_read_by_user_id(user.id, db=db),
        'folder_unread_counts': await get_folder_unread_counts(user.id, db=db),
    }


############################
# GetChatUsageStats
# EXPERIMENTAL: may be removed in future releases
############################


@router.get('/stats/usage', response_model=ChatUsageStatsListResponse)
async def get_session_user_chat_usage_stats(
    items_per_page: int | None = 50,
    page: int | None = 1,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        limit = items_per_page
        skip = (page - 1) * limit

        result = await Chats.get_chats_by_user_id(user.id, skip=skip, limit=limit, db=db)

        chats = result.items
        total = result.total

        chat_stats = []
        for chat in chats:
            messages_map = chat.chat.get('history', {}).get('messages', {})
            message_id = chat.chat.get('history', {}).get('currentId')

            if messages_map and message_id:
                try:
                    history_models = {}
                    history_message_count = len(messages_map)
                    history_user_messages = []
                    history_assistant_messages = []

                    for message in messages_map.values():
                        if message.get('role', '') == 'user':
                            history_user_messages.append(message)
                        elif message.get('role', '') == 'assistant':
                            history_assistant_messages.append(message)
                            model = message.get('model', None)
                            if model:
                                if model not in history_models:
                                    history_models[model] = 0
                                history_models[model] += 1

                    average_user_message_content_length = (
                        sum(len(message.get('content', '')) for message in history_user_messages)
                        / len(history_user_messages)
                        if len(history_user_messages) > 0
                        else 0
                    )
                    average_assistant_message_content_length = (
                        sum(len(message.get('content', '')) for message in history_assistant_messages)
                        / len(history_assistant_messages)
                        if len(history_assistant_messages) > 0
                        else 0
                    )

                    response_times = []
                    for message in history_assistant_messages:
                        user_message_id = message.get('parentId', None)
                        if user_message_id and user_message_id in messages_map:
                            user_message = messages_map[user_message_id]
                            response_time = message.get('timestamp', 0) - user_message.get('timestamp', 0)

                            response_times.append(response_time)

                    average_response_time = sum(response_times) / len(response_times) if len(response_times) > 0 else 0

                    message_list = get_message_list(messages_map, message_id)
                    message_count = len(message_list)

                    models = {}
                    for message in reversed(message_list):
                        if message.get('role') == 'assistant':
                            model = message.get('model', None)
                            if model:
                                if model not in models:
                                    models[model] = 0
                                models[model] += 1

                            annotation = message.get('annotation', {})

                    chat_stats.append(
                        {
                            'id': chat.id,
                            'models': models,
                            'message_count': message_count,
                            'history_models': history_models,
                            'history_message_count': history_message_count,
                            'history_user_message_count': len(history_user_messages),
                            'history_assistant_message_count': len(history_assistant_messages),
                            'average_response_time': average_response_time,
                            'average_user_message_content_length': average_user_message_content_length,
                            'average_assistant_message_content_length': average_assistant_message_content_length,
                            'tags': chat.meta.get('tags', []),
                            'last_message_at': message_list[-1].get('timestamp', None),
                            'updated_at': chat.updated_at,
                            'created_at': chat.created_at,
                        }
                    )
                except Exception as e:
                    pass

        return ChatUsageStatsListResponse(items=chat_stats, total=total)

    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# GetChatStatsExport
############################


CHAT_EXPORT_PAGE_ITEM_COUNT = 10


class ChatStatsExportList(BaseModel):
    type: str = 'chats'
    items: list[ChatStatsExport]
    total: int
    page: int


def _process_chat_for_export(chat) -> ChatStatsExport | None:
    try:

        def get_message_content_length(message):
            content = message.get('content', '')
            if isinstance(content, str):
                return len(content)
            elif isinstance(content, list):
                return sum(len(item.get('text', '')) for item in content if item.get('type') == 'text')
            return 0

        messages_map = chat.chat.get('history', {}).get('messages', {})
        message_id = chat.chat.get('history', {}).get('currentId')

        history_models = {}
        history_message_count = len(messages_map)
        history_user_messages = []
        history_assistant_messages = []

        export_messages = {}
        for key, message in messages_map.items():
            try:
                content_length = get_message_content_length(message)

                # Extract rating safely
                rating = message.get('annotation', {}).get('rating')
                tags = message.get('annotation', {}).get('tags')

                message_stat = MessageStats(
                    id=message.get('id'),
                    role=message.get('role'),
                    model=message.get('model'),
                    timestamp=message.get('timestamp'),
                    content_length=content_length,
                    token_count=None,  # Populate if available, e.g. message.get("info", {}).get("token_count")
                    rating=rating,
                    tags=tags,
                )

                export_messages[key] = message_stat

                # --- Aggregation Logic (copied/adapted from usage stats) ---
                role = message.get('role', '')
                if role == 'user':
                    history_user_messages.append(message)
                elif role == 'assistant':
                    history_assistant_messages.append(message)
                    model = message.get('model')
                    if model:
                        if model not in history_models:
                            history_models[model] = 0
                        history_models[model] += 1
            except Exception as e:
                log.debug('Error processing message %s: %s', key, e)
                continue

        # Calculate Averages
        average_user_message_content_length = (
            sum(get_message_content_length(m) for m in history_user_messages) / len(history_user_messages)
            if history_user_messages
            else 0
        )

        average_assistant_message_content_length = (
            sum(get_message_content_length(m) for m in history_assistant_messages) / len(history_assistant_messages)
            if history_assistant_messages
            else 0
        )

        # Response Times
        response_times = []
        for message in history_assistant_messages:
            user_message_id = message.get('parentId', None)
            if user_message_id and user_message_id in messages_map:
                user_message = messages_map[user_message_id]
                # Ensure timestamps exist
                t1 = message.get('timestamp')
                t0 = user_message.get('timestamp')
                if t1 and t0:
                    response_times.append(t1 - t0)

        average_response_time = sum(response_times) / len(response_times) if response_times else 0

        # Current Message List Logic (Main path)
        message_list = get_message_list(messages_map, message_id)
        message_count = len(message_list)
        models = {}
        for message in reversed(message_list):
            if message.get('role') == 'assistant':
                model = message.get('model')
                if model:
                    if model not in models:
                        models[model] = 0
                    models[model] += 1

        # Construct Aggregate Stats
        stats = AggregateChatStats(
            average_response_time=average_response_time,
            average_user_message_content_length=average_user_message_content_length,
            average_assistant_message_content_length=average_assistant_message_content_length,
            models=models,
            message_count=message_count,
            history_models=history_models,
            history_message_count=history_message_count,
            history_user_message_count=len(history_user_messages),
            history_assistant_message_count=len(history_assistant_messages),
        )

        # Construct Chat Body
        chat_body = ChatBody(history=ChatHistoryStats(messages=export_messages, currentId=message_id))

        return ChatStatsExport(
            id=chat.id,
            user_id=chat.user_id,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            tags=chat.meta.get('tags', []),
            stats=stats,
            chat=chat_body,
        )
    except Exception as e:
        log.exception(f'Error exporting stats for chat {chat.id}: {e}')
        return None


async def calculate_chat_stats(user_id, skip=0, limit=10, filter=None):
    if filter is None:
        filter = {}

    result = await Chats.get_chats_by_user_id(
        user_id,
        skip=skip,
        limit=limit,
        filter=filter,
    )

    chat_stats_export_list = []
    for chat in result.items:
        chat_stat = _process_chat_for_export(chat)
        if chat_stat:
            chat_stats_export_list.append(chat_stat)

    return chat_stats_export_list, result.total


async def generate_chat_stats_jsonl_generator(user_id, filter):
    """
    Async generator for streaming chat stats export.

    NOTE: We intentionally do NOT pass a shared db session here. Instead, we let
    each batch create its own short-lived session via get_async_db_context(None).
    This is critical for SQLite in low-resource environments because:
    1. SQLite uses file-level locking
    2. Holding a session open for the entire streaming duration blocks other requests
    3. Short-lived sessions release locks between batches, allowing other operations
    """
    skip = 0
    limit = CHAT_EXPORT_PAGE_ITEM_COUNT

    while True:
        # Each batch gets its own session that closes after the query
        result = await Chats.get_chats_by_user_id(
            user_id,
            filter=filter,
            skip=skip,
            limit=limit,
            db=None,  # Let get_async_db_context create a fresh session per batch
        )
        if not result.items:
            break

        for chat in result.items:
            try:
                chat_stat = _process_chat_for_export(chat)
                if chat_stat:
                    yield chat_stat.model_dump_json() + '\n'
            except Exception as e:
                log.exception(f'Error processing chat {chat.id}: {e}')

        skip += limit


@router.get('/stats/export', response_model=ChatStatsExportList)
async def export_chat_stats(
    request: Request,
    updated_at: int | None = None,
    page: int | None = 1,
    stream: bool = False,
    user=Depends(get_verified_user),
):
    # Check if the user has permission to share/export chats
    if (user.role != 'admin') and (not await Config.get('ui.enable_community_sharing')):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    try:
        # Fetch chats with date filtering
        filter = {'order_by': 'updated_at', 'direction': 'asc'}

        if updated_at:
            filter['updated_at'] = updated_at

        if stream:
            return StreamingResponse(
                generate_chat_stats_jsonl_generator(user.id, filter),
                media_type='application/x-ndjson',
                headers={'Content-Disposition': f'attachment; filename=chat-stats-export-{user.id}.jsonl'},
            )
        else:
            limit = CHAT_EXPORT_PAGE_ITEM_COUNT
            skip = (page - 1) * limit

            chat_stats_export_list, total = await calculate_chat_stats(user.id, skip, limit, filter)

            return ChatStatsExportList(items=chat_stats_export_list, total=total, page=page)

    except Exception as e:
        log.debug('Error exporting chat stats: %s', e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# GetSingleChatStatsExport
############################


@router.get('/stats/export/{chat_id}', response_model=ChatStatsExport | None)
async def export_single_chat_stats(
    request: Request,
    chat_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """
    Export stats for exactly one chat by ID.
    Returns ChatStatsExport for the specified chat.
    """
    # Check if the user has permission to share/export chats
    if (user.role != 'admin') and (not await Config.get('ui.enable_community_sharing')):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    try:
        chat = await Chats.get_chat_by_id(chat_id, db=db)

        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

        # Verify the chat belongs to the user (unless admin)
        if chat.user_id != user.id and not (user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )

        # Process the chat for export (pure computation, no DB)
        chat_stats = _process_chat_for_export(chat)

        if not chat_stats:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Failed to process chat stats',
            )

        return chat_stats

    except HTTPException:
        raise
    except Exception as e:
        log.debug('Error exporting single chat stats: %s', e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


@router.delete('/', response_model=bool)
async def delete_all_user_chats(
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role == 'user' and not await has_permission(user.id, 'chat.delete', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    tag_ids = [tag.id for tag in await Tags.get_tags_by_user_id(user.id, db=db)]
    result = await Chats.delete_chats_by_user_id(user.id, db=db)
    if result:
        await Chats.delete_orphan_tags_for_user(tag_ids, user.id, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_DELETED_ALL,
            actor=user,
            subject_id=user.id,
            subject_type='user',
        )
    return result


############################
# GetUserChatList
############################


@router.get('/list/user/{user_id}', response_model=list[ChatTitleIdResponse])
async def get_user_chat_list_by_user_id(
    request: Request,
    user_id: str,
    page: int | None = None,
    query: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """List chat summaries for a given user (admin-only endpoint)."""
    if not ENABLE_ADMIN_CHAT_ACCESS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    effective_page = page if page is not None else 1
    limit = 60
    skip = (effective_page - 1) * limit

    filter = {}
    if query:
        filter['query'] = query
    if order_by:
        filter['order_by'] = order_by
    if direction:
        filter['direction'] = direction

    chats = await Chats.get_chat_list_by_user_id(
        user_id, include_archived=True, filter=filter, skip=skip, limit=limit, db=db
    )
    return await add_active_state_to_chat_list(request, chats)


############################
# CreateNewChat
############################


@router.post('/new', response_model=ChatResponse | None)
async def create_new_chat(
    request: Request,
    form_data: ChatForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if form_data.folder_id is not None and not await has_folder_write_access(user.id, form_data.folder_id, db=db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    try:
        chat = await Chats.insert_new_chat(str(uuid4()), user.id, form_data, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_CREATED,
            actor=user,
            subject_id=chat.id,
            data={'title': chat.title, 'folder_id': chat.folder_id},
        )
        return ChatResponse.model_validate(chat, from_attributes=True)
    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# ImportChats
############################


@router.post('/import', response_model=list[ChatResponse])
async def import_chats(
    request: Request,
    form_data: ChatsImportForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await require_chat_import_permission(request, user, db)

    try:
        chats = await Chats.import_chats(user.id, form_data.chats, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_IMPORTED,
            actor=user,
            subject_type='chat.import',
            data={'count': len(chats), 'chat_ids': [chat.id for chat in chats]},
        )
        return chats
    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# ChatConfig
############################


@router.get('/config', response_model=ChatConfigForm)
async def get_chat_config(user=Depends(get_admin_user)):
    return await get_chat_config_values()


@router.post('/config', response_model=ChatConfigForm)
async def set_chat_config(form_data: ChatConfigForm, user=Depends(get_admin_user)):
    threshold = form_data.CONTEXT_COMPACTION_TOKEN_THRESHOLD
    if threshold is None:
        threshold = CONTEXT_COMPACTION_TOKEN_THRESHOLD
    threshold = max(1, int(threshold))
    token_cap = max(1, int(form_data.CONTEXT_COMPACTION_TOKEN_CAP or threshold))
    retention_percentage = form_data.CONTEXT_COMPACTION_RETENTION_PERCENTAGE
    if retention_percentage is None:
        retention_percentage = CONTEXT_COMPACTION_RETENTION_PERCENTAGE
    retention_percentage = min(50, max(10, int(retention_percentage)))
    tool_search_defer_threshold = max(0, int(form_data.TOOL_SEARCH_DEFER_THRESHOLD))
    tool_search_always_loaded = [item.strip() for item in form_data.TOOL_SEARCH_ALWAYS_LOADED if item.strip()]
    await Config.upsert(
        chat_config_updates(
            {
                **form_data.model_dump(),
                'CONTEXT_COMPACTION_MODEL': form_data.CONTEXT_COMPACTION_MODEL or '',
                'CONTEXT_COMPACTION_TOKEN_THRESHOLD': threshold,
                'CONTEXT_COMPACTION_TOKEN_CAP': token_cap,
                'CONTEXT_COMPACTION_RETENTION_PERCENTAGE': retention_percentage,
                'TOOL_SEARCH_DEFER_THRESHOLD': tool_search_defer_threshold,
                'TOOL_SEARCH_ALWAYS_LOADED': tool_search_always_loaded,
            }
        )
    )
    return await get_chat_config_values()


############################
# GetChats
############################


@router.get('/search', response_model=list[ChatTitleIdResponse])
async def search_user_chats(
    request: Request,
    text: str,
    page: int | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if page is None:
        page = 1

    limit = 60
    skip = (page - 1) * limit

    search_text = chat_search_content_text(text)
    chat_list = []
    for chat in await Chats.get_chats_by_user_id_and_search_text(user.id, text, skip=skip, limit=limit, db=db):
        # Explicit fields: model_dump() would deep-copy the entire chat blob per row
        chat_list.append(
            ChatTitleIdResponse(
                id=chat.id,
                title=chat.title,
                updated_at=chat.updated_at,
                created_at=chat.created_at,
                last_read_at=chat.last_read_at,
                snippet=chat_search_snippet(chat.chat, search_text),
                archived=chat.archived,
            )
        )

    # Delete tag if no chat is found
    words = text.strip().split(' ')
    if page == 1 and len(words) == 1 and words[0].startswith('tag:'):
        tag_id = words[0].replace('tag:', '')
        if len(chat_list) == 0:
            if await Tags.get_tag_by_name_and_user_id(tag_id, user.id, db=db):
                log.debug('deleting tag: %s', tag_id)
                await Tags.delete_tag_by_name_and_user_id(tag_id, user.id, db=db)

    return await add_active_state_to_chat_list(request, chat_list)


############################
# GetChatsByFolderId
############################


@router.get('/folder/{folder_id}', response_model=list[ChatResponse])
async def get_chats_by_folder_id(
    folder_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    folder_ids = [folder_id]
    children_folders = await Folders.get_children_folders_by_id_and_user_id(folder_id, user.id, db=db)
    if children_folders:
        folder_ids.extend([folder.id for folder in children_folders])

    return [
        ChatResponse.model_validate(chat, from_attributes=True)
        for chat in await Chats.get_chats_by_folder_ids_and_user_id(folder_ids, user.id, db=db)
    ]


@router.get('/folder/{folder_id}/list', response_model=list[ChatTitleIdResponse])
async def get_chat_list_by_folder_id(
    request: Request,
    folder_id: str,
    page: int | None = 1,
    sort_by: str = 'unread_updated_at',
    sort_dir: str = 'desc',
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        limit = 10
        skip = (page - 1) * limit

        chats = await Chats.get_chats_by_folder_id_and_user_id(
            folder_id,
            user.id,
            skip=skip,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            db=db,
        )
        return await add_active_state_to_chat_list(request, chats)

    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# GetPinnedChats
############################


@router.get('/pinned', response_model=list[ChatTitleIdResponse])
async def get_user_pinned_chats(
    request: Request, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    chats = await Chats.get_pinned_chats_by_user_id(user.id, db=db)
    return await add_active_state_to_chat_list(request, chats)


############################
# GetChats
############################

CHAT_EXPORT_BATCH_SIZE = 100


async def generate_chat_export_ndjson(user_id: str):
    """
    Async generator that streams all user chats as NDJSON (one JSON object per line).

    Uses short-lived DB sessions per batch to avoid holding locks for the
    entire duration, which is critical for SQLite environments.
    """
    skip = 0

    while True:
        result = await Chats.get_chats_by_user_id(
            user_id,
            skip=skip,
            limit=CHAT_EXPORT_BATCH_SIZE,
            db=None,
        )
        if not result.items:
            break

        for chat in result.items:
            try:
                yield ChatResponse.model_validate(chat, from_attributes=True).model_dump_json() + '\n'
            except Exception as e:
                log.exception(f'Error serializing chat {chat.id}: {e}')

        if len(result.items) < CHAT_EXPORT_BATCH_SIZE:
            break

        skip += CHAT_EXPORT_BATCH_SIZE


@router.get('/all')
async def get_user_chats(user=Depends(get_verified_user)):
    return StreamingResponse(
        generate_chat_export_ndjson(user.id),
        media_type='application/x-ndjson',
    )


############################
# GetArchivedChats
############################


@router.get('/all/archived', response_model=list[ChatResponse])
async def get_user_archived_chats(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    return [
        ChatResponse.model_validate(chat, from_attributes=True)
        for chat in await Chats.get_archived_chats_by_user_id(user.id, db=db)
    ]


############################
# GetAllTags
############################


@router.get('/all/tags', response_model=list[TagModel])
async def get_all_user_tags(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    try:
        tags = await Tags.get_tags_by_user_id(user.id, db=db)
        return tags
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT())


############################
# GetAllChatsInDB
############################


@router.get('/all/db', response_model=list[ChatResponse])
async def get_all_user_chats_in_db(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    if not ENABLE_ADMIN_EXPORT:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
    return [ChatResponse.model_validate(chat, from_attributes=True) for chat in await Chats.get_chats(db=db)]


############################
# GetArchivedChats
############################


@router.get('/archived', response_model=list[ChatTitleIdResponse])
async def get_archived_session_user_chat_list(
    request: Request,
    page: int | None = None,
    query: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if page is None:
        page = 1

    limit = 60
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter['query'] = query
    if order_by:
        filter['order_by'] = order_by
    if direction:
        filter['direction'] = direction

    chats = await Chats.get_archived_chat_list_by_user_id(
        user.id,
        filter=filter,
        skip=skip,
        limit=limit,
        db=db,
    )
    return await add_active_state_to_chat_list(request, chats)


############################
# GetArchivedChatsCount
############################


@router.get('/archived/count', response_model=int)
async def get_archived_session_user_chat_count(
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await Chats.count_archived_chats_by_user_id(user.id, db=db)


############################
# ArchiveAllChats
############################


@router.post('/archive/all', response_model=bool)
async def archive_all_chats(
    request: Request, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    result = await Chats.archive_all_chats_by_user_id(user.id, db=db)
    if result:
        await publish_event(request, EVENTS.CHAT_ARCHIVED, actor=user, subject_id=user.id, subject_type='user')
    return result


############################
# UnarchiveAllChats
############################


@router.post('/unarchive/all', response_model=bool)
async def unarchive_all_chats(
    request: Request, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    tag_ids = {
        tag_id
        for chat in await Chats.get_archived_chats_by_user_id(user.id, db=db)
        for tag_id in chat.meta.get('tags', [])
    }
    result = await Chats.unarchive_all_chats_by_user_id(user.id, db=db)
    if result:
        await Tags.ensure_tags_exist(list(tag_ids), user.id, db=db)
        await publish_event(request, EVENTS.CHAT_UNARCHIVED, actor=user, subject_id=user.id, subject_type='user')
    return result


############################
# UnshareAllChats
############################


@router.delete('/share/all', response_model=bool)
async def unshare_all_chats(
    request: Request, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    # Collect chat_ids that have shares so we can clear share_id and access grants
    shared_list = await SharedChats.get_by_user_id(user.id, db=db)
    chat_ids = [s.chat_id for s in shared_list]

    # Delete all shared_chat rows for this user
    result = await SharedChats.delete_all_by_user_id(user.id, db=db)

    # Clear share_id on the original chats and remove access grants
    for chat_id in chat_ids:
        await Chats.update_chat_share_id_by_id(chat_id, None, db=db)
        await AccessGrants.set_access_grants('shared_chat', chat_id, [], db=db)

    if result:
        await publish_event(
            request,
            EVENTS.CHAT_UNSHARED,
            actor=user,
            subject_id=user.id,
            subject_type='user',
            data={'count': len(chat_ids), 'chat_ids': chat_ids},
        )
    return result


@router.get('/shared', response_model=list[SharedChatResponse])
async def get_shared_session_user_chat_list(
    page: int | None = None,
    query: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if page is None:
        page = 1

    limit = 60
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter['query'] = query
    if order_by:
        filter['order_by'] = order_by
    if direction:
        filter['direction'] = direction

    return await SharedChats.get_by_user_id(
        user.id,
        filter=filter,
        skip=skip,
        limit=limit,
        db=db,
    )


############################
# GetSharedChatById
############################


@router.get('/share/{share_id}', response_model=ChatResponse | None)
async def get_shared_chat_by_id(
    share_id: str, user=Depends(get_optional_verified_user), db: AsyncSession = Depends(get_async_session)
):
    shared = await SharedChats.get_by_id(share_id, db=db)
    if shared:
        if await is_open_shared_chat(shared, db=db) or (
            user is not None and await can_read_shared_chat(user, shared, db=db)
        ):
            live = (
                shared.chat.get('share_mode') == 'continue'
                and user is not None
                and await Chats.get_accessible_chat_by_id(shared.chat_id, user, db=db, permission='write') is not None
            )
            chat = (
                await Chats.get_chat_by_id(shared.chat_id, db=db)
                if live
                else await Chats.get_chat_by_share_id(share_id, db=db)
            )
            if chat:
                data = await shared_chat_response(chat, user, db=db)
                data['chat']['share_mode'] = 'continue' if live else None
                return data

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED if user else ERROR_MESSAGES.INVALID_TOKEN,
        )

    # Fallback: admins can also access any chat directly by chat ID
    chat = None
    if user is not None and user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS:
        chat = await Chats.get_chat_by_id(share_id, db=db)
        if chat:
            data = await shared_chat_response(chat, user, db=db)
            data['chat']['share_mode'] = (
                'continue'
                if await Chats.get_accessible_chat_by_id(chat.id, user, db=db, permission='write', chat=chat)
                else None
            )
            return data

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)


############################
# GetChatsByTags
############################


class TagForm(BaseModel):
    name: str


class TagFilterForm(TagForm):
    skip: int | None = 0
    limit: int | None = 50


@router.post('/tags', response_model=list[ChatTitleIdResponse])
async def get_user_chat_list_by_tag_name(
    request: Request,
    form_data: TagFilterForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chats = await Chats.get_chat_list_by_user_id_and_tag_name(
        user.id, form_data.name, form_data.skip, form_data.limit, db=db
    )
    if len(chats) == 0:
        await Tags.delete_tag_by_name_and_user_id(form_data.name, user.id, db=db)

    return await add_active_state_to_chat_list(request, chats)


############################
# CompactChat
############################


@router.post('/{id}/compact')
async def compact_chat_by_id(
    request: Request,
    id: str,
    form_data: CompactChatForm | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)

    if await has_active_tasks(request.app.state.redis, id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Wait for the current response to finish before compacting.',
        )

    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    history = (chat.chat or {}).get('history') or {}
    messages_map = await Chats.get_messages_map_by_chat_id(id)
    current_message_id = chat.current_message_id or history.get('currentId')
    message_list = get_message_list(messages_map or history.get('messages') or {}, current_message_id)
    model_id = (form_data.model if form_data else None) or next(
        (message.get('model') for message in reversed(message_list) if message.get('model')),
        None,
    )

    if not model_id:
        chat_models = (chat.chat or {}).get('models') or []
        model_id = chat_models[0] if chat_models else None
    if not model_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='No model found for context compaction.')

    result = await compact_chat_branch(request, user, chat, model_id, request.app.state.MODELS)
    result['context_usage'] = await get_chat_context_usage(chat, model_id)
    if result.get('compacted'):
        await publish_event(
            request,
            EVENTS.CHAT_COMPACTED,
            actor=user,
            subject_id=id,
            data={'dropped_messages': result.get('dropped_messages')},
        )
    return result


############################
# GetChatById
############################


############################
# Roleplay settings (lorebooks attached to a chat, author's note)
############################


class ChatRoleplaySettings(BaseModel):
    lorebook_ids: list[str] = []
    authors_note: DepthPrompt | None = None
    # One of the user's persona ids; '' for none; None for their default persona.
    persona_id: str | None = None
    # Long-chat memory for this chat (character chats); on unless turned off.
    memory_enabled: bool = True


def _roleplay_settings(meta: dict) -> ChatRoleplaySettings:
    try:
        return ChatRoleplaySettings(
            lorebook_ids=meta.get('lorebook_ids') or [],
            authors_note=meta.get('authors_note') or None,
            persona_id=meta.get('persona_id'),
            memory_enabled=memory_enabled_for_chat(meta),
        )
    except ValueError:
        return ChatRoleplaySettings()


@router.get('/{id}/roleplay', response_model=ChatRoleplaySettings)
async def get_chat_roleplay_settings(
    id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    meta = await Chats.get_chat_meta_by_id_and_user_id(id, user.id, db=db)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return _roleplay_settings(meta)


@router.post('/{id}/roleplay', response_model=ChatRoleplaySettings)
async def update_chat_roleplay_settings(
    id: str,
    form_data: ChatRoleplaySettings,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    # Only lorebooks the user can read may be attached; others are dropped silently.
    readable = await Lorebooks.get_readable_lorebooks_by_ids(
        list(dict.fromkeys(form_data.lorebook_ids)), user.id, db=db
    )
    note = form_data.authors_note if form_data.authors_note and form_data.authors_note.text.strip() else None
    meta = await Chats.update_chat_meta_by_id_and_user_id(
        id,
        user.id,
        {
            'lorebook_ids': [lorebook.id for lorebook in readable],
            'authors_note': note.model_dump() if note else None,
            'persona_id': form_data.persona_id,
            'memory_enabled': form_data.memory_enabled,
        },
        db=db,
    )
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return _roleplay_settings(meta)


class ChatMemoryResponse(BaseModel):
    available: bool  # roleplay memory is enabled on this server
    enabled: bool  # ... and for this chat
    summary: str | None = None
    message_id: str | None = None  # the checkpoint: messages before it are covered by the summary
    summarized_messages: int = 0
    total_messages: int = 0
    token_threshold: int = 0


class ChatMemoryForm(BaseModel):
    summary: str = Field(default='', max_length=50_000)


async def _chat_branch(id: str, user, db):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    history = (chat.chat or {}).get('history') or {}
    messages_map = await Chats.get_messages_map_by_chat_id(id) or history.get('messages') or {}
    current_id = chat.current_message_id or history.get('currentId')
    return chat, messages_map, current_id


async def _chat_memory(chat, messages_map: dict, current_id: str | None) -> ChatMemoryResponse:
    try:
        threshold = int(await Config.get('characters.memory.token_threshold') or 0)
    except (TypeError, ValueError):
        threshold = 0
    return ChatMemoryResponse(
        available=bool(await Config.get('characters.memory.enable')),
        enabled=memory_enabled_for_chat(chat.meta or {}),
        token_threshold=threshold,
        **latest_memory(messages_map, current_id),
    )


@router.get('/{id}/roleplay/memory', response_model=ChatMemoryResponse)
async def get_chat_memory(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    """The story memory in effect on the chat's current branch."""
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    return await _chat_memory(chat, messages_map, current_id)


@router.post('/{id}/roleplay/memory', response_model=ChatMemoryResponse)
async def update_chat_memory(
    id: str,
    form_data: ChatMemoryForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Edit the current memory; an empty summary clears it, so the full history is sent again."""
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    memory = latest_memory(messages_map, current_id)
    if not memory['message_id']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail='This chat has no memory yet. Use Update now to create one.'
        )
    summary = form_data.summary.strip()
    if summary:
        targets = [memory['message_id']]
    else:
        # Clearing removes every checkpoint on the branch; otherwise an older one would take over.
        targets = [
            key
            for key in branch_message_ids(messages_map, current_id)
            if (messages_map[key] or {}).get('contextSummary') or (messages_map[key] or {}).get('context_summary')
        ]
    for message_id in targets:
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            id, message_id, {'contextSummary': summary, 'context_summary': None}, touch=False
        )
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    return await _chat_memory(chat, messages_map, current_id)


@router.post('/{id}/roleplay/memory/refresh', response_model=ChatMemoryResponse)
async def refresh_chat_memory(
    request: Request, id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    """Summarize the older part of the current branch into the story memory now."""
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    if await has_active_tasks(request.app.state.redis, id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Wait for the current response to finish first.'
        )
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)
    message_list = [messages_map[key] for key in branch_message_ids(messages_map, current_id)]
    model_id = next((m.get('model') for m in reversed(message_list) if m.get('model')), None) or next(
        iter((chat.chat or {}).get('models') or []), None
    )
    model = request.app.state.MODELS.get(model_id) if model_id else None
    if not model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='No model found for this chat.')

    character = get_model_character(model)
    char = character.display_name(model.get('name') or '') if character else model.get('name') or ''
    persona = resolve_persona(user, (chat.meta or {}).get('persona_id'))
    settings = await memory_overrides(char, (persona or {}).get('name') or user.name, {})
    if settings is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Roleplay memory is turned off.')
    settings.pop('label', None)
    # The summary request may go to the character model itself; it must not get the character prompt.
    skip_character_system_prompt(request, model_id)
    try:
        result = await compact_chat_branch(
            request, user, chat, model_id, request.app.state.MODELS, overrides=settings, keep_recent=True
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    if not result.get('compacted'):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='The chat is too short to summarize yet.')
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    return await _chat_memory(chat, messages_map, current_id)


############################
# Roleplay assists: impersonate, scene images, lorebook suggestions
############################


class RoleplayAssistForm(BaseModel):
    model_id: str
    # The current branch as the client has it (role and content), oldest first.
    messages: list[dict] = Field(default_factory=list, max_length=500)
    chat_id: str | None = None
    message_id: str | None = None
    persona_id: str | None = None
    draft: str = Field(default='', max_length=10_000)
    style: str = Field(default='', max_length=300)


async def _roleplay_context(request: Request, form_data: RoleplayAssistForm, user, db):
    """The character model (which the user may use), its character and the macro context."""
    from open_webui.routers.models import _get_character_model, _require_character_cards_enabled

    await _require_character_cards_enabled()
    model = await _get_character_model(form_data.model_id, user, db, 'read')
    if not request.app.state.MODELS or model.id not in request.app.state.MODELS:
        await get_all_models(request, user=user)
    chat_meta = {}
    if is_saved_chat_id(form_data.chat_id):
        chat_meta = await Chats.get_chat_meta_by_id_and_user_id(form_data.chat_id, user.id, db=db) or {}
    persona = resolve_persona(user, form_data.persona_id, chat_meta.get('persona_id'))
    character = model.meta.character
    ctx = build_macro_context(
        character,
        model_name=model.name,
        user_name=(persona or {}).get('name') or user.name,
        persona=persona_description(persona),
    )
    return model, character, ctx


@router.post('/roleplay/impersonate')
async def impersonate_user(
    request: Request,
    form_data: RoleplayAssistForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Write the user's next message (as their persona) for them to review before sending."""
    model, character, ctx = await _roleplay_context(request, form_data, user, db)
    prompt = impersonate_prompt(character, ctx, form_data.messages, form_data.draft)
    # The chat model writes it (task models are often too small to match a writing style), without
    # the character prompt, which would make it answer as the character.
    skip_character_system_prompt(request, model.id)
    try:
        reply = await run_task_prompt(request, user, model.id, prompt, 'character_impersonate', direct=True)
    except Exception as error:
        log.info('Impersonation failed for %s: %s', model.id, error)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='The model did not answer.') from error
    text = clean_impersonation(reply, ctx)
    if not text:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='The model returned an empty message.')
    return {'content': text}


async def _require_image_generation(user, db) -> None:
    from open_webui.routers.images import get_image_config

    image_config = await get_image_config()
    if not image_config.ENABLE_IMAGE_GENERATION:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Image generation is not set up.')
    if user.role != 'admin' and not await has_permission(
        user.id, 'features.image_generation', image_config.USER_PERMISSIONS, db=db
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)


@router.post('/roleplay/scene-image')
async def generate_scene_image(
    request: Request,
    form_data: RoleplayAssistForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Picture the current moment: the task model writes an image prompt from the characters'
    looks and the recent messages, then the configured image generator draws it."""
    from open_webui.routers.images import CreateImageForm, image_generations

    await _require_image_generation(user, db)
    model, character, ctx = await _roleplay_context(request, form_data, user, db)
    skip_character_system_prompt(request, model.id)
    try:
        reply = await run_task_prompt(
            request, user, model.id, scene_prompt(character, ctx, form_data.messages, form_data.style), 'scene_image'
        )
    except Exception as error:
        log.info('Scene prompt failed for %s: %s', model.id, error)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='The model did not answer.') from error
    image_prompt = clean_image_prompt(reply)
    if not image_prompt:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='The model returned an empty prompt.')

    metadata = {}
    if (
        is_saved_chat_id(form_data.chat_id)
        and form_data.message_id
        and await Chats.get_chat_by_id_and_user_id(form_data.chat_id, user.id, db=db)
    ):
        # Linked to the message, like images generated in chat.
        metadata = {'chat_id': form_data.chat_id, 'message_id': form_data.message_id}
    images = await image_generations(request, CreateImageForm(prompt=image_prompt), metadata=metadata, user=user)
    return {'prompt': image_prompt, 'images': images}


class LoreSuggestionsResponse(BaseModel):
    suggestions: list[dict]
    source: str  # 'memory' or 'chat': what the suggestions were drawn from


@router.post('/{id}/roleplay/lore/suggest', response_model=LoreSuggestionsResponse)
async def suggest_lore_entries(
    request: Request, id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    """New lorebook entries for lasting facts in the chat's story memory (or, before there is one,
    its recent messages), leaving out what the chat's and character's lorebooks already cover."""
    chat, messages_map, current_id = await _chat_branch(id, user, db)
    message_list = [messages_map[key] for key in branch_message_ids(messages_map, current_id)]
    model_id = next((m.get('model') for m in reversed(message_list) if m.get('model')), None) or next(
        iter((chat.chat or {}).get('models') or []), None
    )
    if not model_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='No model found for this chat.')
    model, character, ctx = await _roleplay_context(
        request, RoleplayAssistForm(model_id=model_id, chat_id=id), user, db
    )

    memory = latest_memory(messages_map, current_id)['summary']
    source = 'memory' if memory else 'chat'
    text = memory or transcript(message_list, ctx.char, ctx.user)
    if not text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='The chat is empty.')

    lorebook_ids = list(
        dict.fromkeys([*(model.meta.lorebook_ids or []), *((chat.meta or {}).get('lorebook_ids') or [])])
    )
    books = [
        lorebook.as_book() for lorebook in await Lorebooks.get_readable_lorebooks_by_ids(lorebook_ids, user.id, db=db)
    ]
    if character.character_book:
        books.append(character.character_book)
    existing = [entry for book in books for entry in (book.get('entries') or []) if isinstance(entry, dict)]

    skip_character_system_prompt(request, model.id)
    try:
        reply = await run_task_prompt(request, user, model.id, lore_prompt(text, existing, ctx), 'lore_suggestions')
    except Exception as error:
        log.info('Lorebook suggestions failed for %s: %s', model.id, error)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='The model did not answer.') from error
    return LoreSuggestionsResponse(suggestions=parse_lore_suggestions(reply, existing, ctx), source=source)


@router.get('/{id}', response_model=ChatResponse | None)
async def get_chat_by_id(
    id: str,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_accessible_chat_by_id(
        id,
        user,
        db=db,
    )

    if chat:
        data = await shared_chat_response(chat, user, db=db)
        data['chat']['share_mode'] = (
            'continue'
            if await Chats.get_accessible_chat_by_id(id, user, db=db, permission='write', chat=chat)
            else None
        )
        data = overlay_response_streams(
            data,
            await get_response_streams_by_chat_id(request.app.state.redis, id),
        )
        data['context_usage'] = await get_chat_context_usage(chat)
        return data

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)


############################
# UpdateChatById
############################


@router.post('/{id}', response_model=ChatResponse | None)
async def update_chat_by_id(
    request: Request,
    id: str,
    form_data: ChatForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        touch = 'history' in form_data.chat or 'messages' in form_data.chat
        chat = await Chats.update_chat_by_id(id, form_data.chat, db=db, touch=touch)
        if form_data.variables is not None:
            chat = (
                await Chats.update_chat_variables_by_id(
                    id,
                    form_data.variables,
                    db=db,
                    touch=False,
                )
                or chat
            )

        await publish_event(
            request,
            EVENTS.CHAT_UPDATED,
            actor=user,
            subject_id=id,
            data={'title': chat.title},
        )
        return ChatResponse.model_validate(chat, from_attributes=True)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


############################
# UpdateChatMessageById
############################
class MessageForm(BaseModel):
    content: str | None = None
    voice: dict | None = None


@router.post('/{id}/messages/{message_id}', response_model=ChatResponse | None)
async def update_chat_message_by_id(
    request: Request,
    id: str,
    message_id: str,
    form_data: MessageForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id(id, db=db)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if chat.user_id != user.id and not (user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    updates = {}
    if form_data.content is not None:
        updates['content'] = form_data.content
    if form_data.voice is not None:
        if len(JSONCodec.dumps(form_data.voice)) > 100000:
            raise HTTPException(400, 'Voice metadata is too large')
        if not await Chats.get_message_by_id_and_message_id(id, message_id):
            raise HTTPException(404, ERROR_MESSAGES.NOT_FOUND)
        updates['meta'] = {'voice': form_data.voice}
    if not updates:
        raise HTTPException(400, 'No message changes supplied')
    chat = await Chats.upsert_message_to_chat_by_id_and_message_id(id, message_id, updates)

    event_emitter = await get_event_emitter(
        {
            'user_id': chat.user_id,
            'chat_id': id,
            'message_id': message_id,
        },
        False,
    )

    if event_emitter:
        await event_emitter(
            {
                'type': 'chat:message' if form_data.content is not None else 'chat:message:voice',
                'data': {
                    'chat_id': id,
                    'message_id': message_id,
                    **({'content': form_data.content} if form_data.content is not None else {'voice': form_data.voice}),
                },
            }
        )

    await publish_event(
        request,
        EVENTS.MESSAGE_UPDATED,
        actor=user,
        subject_id=message_id,
        data={'chat_id': id, 'content_preview': (form_data.content or '')[:300]},
    )
    return ChatResponse.model_validate(chat, from_attributes=True)


@router.delete('/{id}/messages/{message_id}', response_model=ChatResponse | None)
async def delete_chat_message_by_id(
    request: Request,
    id: str,
    message_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id(id, db=db)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if chat.user_id != user.id and not (user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    chat = await Chats.delete_message_from_chat_by_id_and_message_id(id, message_id)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    await publish_event(
        request,
        EVENTS.MESSAGE_DELETED,
        actor=user,
        subject_id=message_id,
        data={'chat_id': id},
    )
    return ChatResponse.model_validate(chat, from_attributes=True)


############################
# SendChatMessageEventById
############################
class EventForm(BaseModel):
    type: str
    data: dict


@router.post('/{id}/messages/{message_id}/event', response_model=bool | None)
async def send_chat_message_event_by_id(
    request: Request,
    id: str,
    message_id: str,
    form_data: EventForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id(id, db=db)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if chat.user_id != user.id and not (user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    event_emitter = await get_event_emitter(
        {
            'user_id': chat.user_id,
            'chat_id': id,
            'message_id': message_id,
        }
    )

    try:
        if event_emitter:
            await event_emitter(form_data.model_dump())
        else:
            return False
        await publish_event(
            request,
            EVENTS.MESSAGE_EVENT_RECEIVED,
            actor=user,
            subject_id=message_id,
            data={'chat_id': id, 'event_type': form_data.type},
        )
        return True
    except Exception:
        return False


############################
# DeleteChatById
############################


@router.delete('/{id}', response_model=bool)
async def delete_chat_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    # Authorize before any side effect: cancelling a chat's in-flight tasks must
    # not be reachable for a chat the caller may not delete.
    if user.role == 'admin':
        chat = await Chats.get_chat_by_id(id, db=db)
        if chat and chat.user_id != user.id and not ENABLE_ADMIN_CHAT_ACCESS:
            chat = None
    else:
        if not await has_permission(user.id, 'chat.delete', await Config.get('user.permissions')):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    # Cancel any in-flight LLM tasks (streaming, title/tags generation) before
    # deleting the chat to prevent orphaned requests.
    await stop_item_tasks(request.app.state.redis, id)
    await Chats.delete_orphan_tags_for_user(chat.meta.get('tags', []), chat.user_id, threshold=1, db=db)

    # Cascade to internal child chats spawned from this one.
    for child_id in await Chats.get_internal_chat_ids_by_parent_id(id, chat.user_id):
        await stop_item_tasks(request.app.state.redis, child_id)
        await Chats.delete_chat_by_id_and_user_id(child_id, chat.user_id)

    if user.role == 'admin':
        result = await Chats.delete_chat_by_id(id, db=db)
    else:
        result = await Chats.delete_chat_by_id_and_user_id(id, user.id, db=db)

    if result:
        await publish_event(
            request,
            EVENTS.CHAT_DELETED,
            actor=user,
            subject_id=id,
            data={'owner_id': chat.user_id},
        )
    return result


############################
# GetPinnedStatusById
############################


@router.get('/{id}/pinned', response_model=bool | None)
async def get_pinned_status_by_id(
    id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        return chat.pinned
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


############################
# PinChatById
############################


@router.post('/{id}/pin', response_model=ChatResponse | None)
async def pin_chat_by_id(
    request: Request, id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        chat = await Chats.toggle_chat_pinned_by_id(id, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_PINNED if chat.pinned else EVENTS.CHAT_UNPINNED,
            actor=user,
            subject_id=id,
            subject_type='chat',
        )
        return chat
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


############################
# CloneChat
############################


class CloneForm(BaseModel):
    title: str | None = None


class ForkForm(BaseModel):
    message_id: str | None = None


@router.post('/{id}/fork', response_model=ChatResponse | None)
async def fork_chat_by_id(
    request: Request,
    id: str,
    form_data: ForkForm | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await require_chat_import_permission(request, user, db)

    chat = await Chats.get_accessible_chat_by_id(id, user, db=db)
    if not chat:
        chat = ChatResponse.model_validate(await get_shared_chat_by_id(id, user=user, db=db))

    is_snapshot = chat.id == chat.share_id
    is_owner = chat.user_id == user.id

    if not is_snapshot and await has_active_tasks(request.app.state.redis, chat.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Wait for the current response to finish before forking.',
        )

    history = (chat.chat or {}).get('history') or {}
    # A share token grants access to its snapshot, not later messages in the original chat.
    messages_map = history.get('messages') or {}
    if not is_snapshot:
        messages_map = await Chats.get_messages_map_by_chat_id(chat.id) or messages_map

    source_message_id = (
        (form_data.message_id if form_data else None) or chat.current_message_id or history.get('currentId')
    )
    if not source_message_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='chat has no messages to fork')

    try:
        fork_history, fork_messages = build_fork_history(messages_map, source_message_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if detail == 'message not found' else status.HTTP_400_BAD_REQUEST,
            detail=detail,
        ) from exc

    for message in fork_history['messages'].values():
        author = (history.get('messages', {}).get(message['id']) or {}).get('user')
        if isinstance(author, dict) and author.get('id') == message.get('user_id'):
            message['user'] = author
        if message.get('role') != 'assistant' or message.get('done') is not False:
            continue

        # An unfinished message is stale unless it is awaiting tool approval
        output = message.get('output')
        if isinstance(output, list) and any(
            isinstance(item, dict)
            and item.get('type') == 'function_call'
            and item.get('status') in {'pending', 'queued', 'requires_approval'}
            for item in output
        ):
            continue

        message['done'] = True

    updated_chat = {**(chat.chat or {})}
    updated_chat.pop('currentId', None)
    updated_chat.pop('share_mode', None)
    updated_chat.update(
        {
            'originalChatId': chat.id,
            'branchPointMessageId': source_message_id,
            'title': f'{chat.title} (fork)',
            'history': fork_history,
            'messages': fork_messages,
        }
    )
    if not is_owner:
        updated_chat = (await shared_chat_response(chat.model_copy(update={'chat': updated_chat}), user, db=db))['chat']
    meta = {
        **((chat.meta or {}) if is_owner else {}),
        'forked_from': chat.id,
        'forked_from_message_id': source_message_id,
    }

    # The source chat's folder may no longer be writable by the caller.
    folder_id = chat.folder_id if is_owner else None
    if folder_id is not None and not await has_folder_write_access(user.id, folder_id, db=db):
        folder_id = None

    fork = await Chats.insert_new_chat(
        str(uuid4()),
        user.id,
        ChatForm(chat=updated_chat, folder_id=folder_id),
        db=db,
        internal_meta=meta,
    )

    if fork and is_owner and chat.variables:
        fork = await Chats.update_chat_variables_by_id(fork.id, chat.variables, db=db, touch=False) or fork

    if not fork:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=ERROR_MESSAGES.DEFAULT())

    if is_owner and chat.pinned:
        fork = await Chats.toggle_chat_pinned_by_id(fork.id, db=db) or fork

    await publish_event(
        request,
        EVENTS.CHAT_CLONED,
        actor=user,
        subject_id=fork.id,
        data={'original_chat_id': id, 'forked_from_message_id': source_message_id},
    )
    return ChatResponse.model_validate(fork, from_attributes=True)


@router.post('/{id}/clone', response_model=ChatResponse | None)
async def clone_chat_by_id(
    request: Request,
    form_data: CloneForm,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await require_chat_import_permission(request, user, db)

    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        updated_chat = {
            **chat.chat,
            'originalChatId': chat.id,
            'branchPointMessageId': chat.chat['history']['currentId'],
            'title': form_data.title if form_data.title else f'Clone of {chat.title}',
        }

        chats = await Chats.import_chats(
            user.id,
            [
                ChatImportForm(
                    **{
                        'chat': updated_chat,
                        'meta': chat.meta,
                        'variables': chat.variables or {},
                        'pinned': chat.pinned,
                        'folder_id': chat.folder_id,
                    }
                )
            ],
            db=db,
        )

        if chats:
            chat = chats[0]
            await publish_event(
                request,
                EVENTS.CHAT_CLONED,
                actor=user,
                subject_id=chat.id,
                data={'original_chat_id': id},
            )
            return ChatResponse.model_validate(chat, from_attributes=True)
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


############################
# CloneSharedChatById
############################


@router.post('/{id}/clone/shared', response_model=ChatResponse | None)
async def clone_shared_chat_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await require_chat_import_permission(request, user, db)

    # Enforce access grants (owner and admins bypass)
    shared = await SharedChats.get_by_id(id, db=db)
    if shared and not (user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS) and shared.user_id != user.id:
        has_grant = await is_open_shared_chat(shared, db=db) or await AccessGrants.has_access(
            user_id=user.id,
            resource_type='shared_chat',
            resource_id=shared.chat_id,
            permission='read',
            db=db,
        )
        if not has_grant:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )

    chat = await Chats.get_chat_by_share_id(id, db=db) if shared else None
    if shared and shared.chat.get('share_mode') == 'continue' and await can_read_shared_chat(user, shared, db=db):
        chat = await Chats.get_chat_by_id(shared.chat_id, db=db)

    # Fallback: admins can also access any chat directly by chat ID
    if not chat and user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS:
        chat = await Chats.get_chat_by_id(id, db=db)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    updated_chat = {
        **(await shared_chat_response(chat, user, db=db))['chat'],
        'originalChatId': chat.id,
        'branchPointMessageId': chat.chat['history']['currentId'],
        'title': f'Clone of {chat.title}',
    }

    chats = await Chats.import_chats(
        user.id,
        [
            ChatImportForm(
                **{
                    'chat': updated_chat,
                    'meta': chat.meta,
                    'variables': {},
                    'pinned': chat.pinned,
                    'folder_id': None,
                }
            )
        ],
        db=db,
    )

    if chats:
        chat = chats[0]
        return ChatResponse.model_validate(chat, from_attributes=True)
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ERROR_MESSAGES.DEFAULT(),
        )


############################
# ArchiveChat
############################


@router.post('/{id}/archive', response_model=ChatResponse | None)
async def archive_chat_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        chat = await Chats.toggle_chat_archive_by_id(id, db=db)

        tag_ids = chat.meta.get('tags', [])
        if chat.archived:
            # Cancel any in-flight LLM tasks before archiving
            await stop_item_tasks(request.app.state.redis, id)
        else:
            # Unarchived — ensure tag rows exist
            await Tags.ensure_tags_exist(tag_ids, user.id, db=db)

        await publish_event(
            request,
            EVENTS.CHAT_ARCHIVED if chat.archived else EVENTS.CHAT_UNARCHIVED,
            actor=user,
            subject_id=id,
            subject_type='chat',
        )
        return ChatResponse.model_validate(chat, from_attributes=True)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


# --- Share Chat ---


@router.post('/{id}/share', response_model=ChatResponse | None)
async def share_chat_by_id(
    request: Request,
    id: str,
    form_data: ShareChatForm | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role != 'admin' and not await has_permission(user.id, 'chat.share', await Config.get('user.permissions')):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    # If a share already exists, re-snapshot it
    if chat.share_id:
        shared = await SharedChats.update(chat.share_id, form_data, db=db)
        if shared:
            chat = await Chats.get_chat_by_id(id, db=db)
            await publish_event(
                request,
                EVENTS.CHAT_SHARED,
                actor=user,
                subject_id=id,
                data={'share_id': chat.share_id, 'updated': True},
            )
            return ChatResponse.model_validate(chat, from_attributes=True)

    # Create a new share
    shared = await SharedChats.create(id, user.id, db=db, share_mode=form_data.share_mode if form_data else None)
    if not shared:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=ERROR_MESSAGES.DEFAULT())

    chat = await Chats.update_chat_share_id_by_id(id, shared.id, db=db)
    if not chat:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=ERROR_MESSAGES.DEFAULT())

    await publish_event(
        request,
        EVENTS.CHAT_SHARED,
        actor=user,
        subject_id=id,
        data={'share_id': shared.id},
    )
    return ChatResponse.model_validate(chat, from_attributes=True)


# --- Delete Shared Chat ---


@router.delete('/{id}/share', response_model=bool | None)
async def delete_shared_chat_by_id(
    request: Request, id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    await SharedChats.delete_by_chat_id(id, db=db)

    if chat.share_id:
        await Chats.update_chat_share_id_by_id(id, None, db=db)

    await AccessGrants.set_access_grants('shared_chat', id, [], db=db)

    await publish_event(
        request,
        EVENTS.CHAT_UNSHARED,
        actor=user,
        subject_id=id,
        data={'share_id': chat.share_id},
    )
    return True


############################
# UpdateSharedChatAccessById
############################


class ChatAccessGrantsForm(BaseModel):
    access_grants: list[dict]
    share_mode: ChatShareMode = None


@router.post('/shared/{id}/access/update', response_model=ChatResponse | None)
async def update_shared_chat_access_by_id(
    request: Request,
    id: str,
    form_data: ChatAccessGrantsForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS:
        chat = await Chats.get_chat_by_id(id, db=db)
    else:
        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_chats',
        'sharing.open_chats',
        db=db,
    )

    await AccessGrants.set_access_grants('shared_chat', id, form_data.access_grants, db=db)
    if 'share_mode' in form_data.model_fields_set and chat.share_id:
        await SharedChats.set_share_mode(chat.share_id, form_data.share_mode, db=db)
    from open_webui.socket.main import refresh_chat_access

    await refresh_chat_access(id)

    return ChatResponse.model_validate(chat, from_attributes=True)


############################
# GetSharedChatAccessById
############################


@router.get('/shared/{id}/access', response_model=list)
async def get_shared_chat_access_by_id(
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role == 'admin' and ENABLE_ADMIN_CHAT_ACCESS:
        chat = await Chats.get_chat_by_id(id, db=db)
    else:
        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    grants = await AccessGrants.get_grants_by_resource('shared_chat', id, db=db)
    return [
        {
            'id': g.id,
            'principal_type': g.principal_type,
            'principal_id': g.principal_id,
            'permission': g.permission,
        }
        for g in grants
    ]


############################
# UpdateChatFolderIdById
############################


class ChatFolderIdForm(BaseModel):
    folder_id: str | None = None


@router.post('/{id}/unread')
async def mark_chat_unread_by_id(
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.mark_chat_unread_by_id(id, user.id, db=db)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    folder_id = await Chats.get_chat_folder_id(id, user.id, db=db)
    return {
        'chat_id': id,
        'last_read_at': chat.last_read_at,
        'folder_id': folder_id,
        'folder_unread_counts': await get_folder_unread_counts(user.id, db=db),
    }


@router.post('/{id}/folder', response_model=ChatResponse | None)
async def update_chat_folder_id_by_id(
    request: Request,
    id: str,
    form_data: ChatFolderIdForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        # None is allowed: it moves the chat out of any folder.
        if form_data.folder_id is not None and not await has_folder_write_access(user.id, form_data.folder_id, db=db):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

        chat = await Chats.update_chat_folder_id_by_id_and_user_id(id, user.id, form_data.folder_id, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_FOLDER_UPDATED,
            actor=user,
            subject_id=id,
            data={'folder_id': form_data.folder_id},
        )
        return ChatResponse.model_validate(chat, from_attributes=True)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


############################
# GetChatTagsById
############################


@router.get('/{id}/tags', response_model=list[TagModel])
async def get_chat_tags_by_id(id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    chat = await Chats.get_accessible_chat_by_id(
        id,
        user,
        db=db,
    )

    if chat:
        tags = chat.meta.get('tags', [])
        return await Tags.get_tags_by_ids_and_user_id(tags, chat.user_id, db=db)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)


############################
# AddChatTagById
############################


@router.post('/{id}/tags', response_model=list[TagModel])
async def add_tag_by_id_and_tag_name(
    request: Request,
    id: str,
    form_data: TagForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        tags = chat.meta.get('tags', [])
        tag_id = form_data.name.replace(' ', '_').lower()

        if tag_id == 'none':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Tag name cannot be 'None'"),
            )

        if tag_id not in tags:
            await Chats.add_chat_tag_by_id_and_user_id_and_tag_name(id, user.id, form_data.name, db=db)
            await publish_event(
                request,
                EVENTS.CHAT_TAG_ADDED,
                actor=user,
                subject_id=id,
                data={'tag': form_data.name},
            )

        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
        tags = chat.meta.get('tags', [])
        return await Tags.get_tags_by_ids_and_user_id(tags, user.id, db=db)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT())


############################
# DeleteChatTagById
############################


@router.delete('/{id}/tags', response_model=list[TagModel])
async def delete_tag_by_id_and_tag_name(
    request: Request,
    id: str,
    form_data: TagForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        await Chats.delete_tag_by_id_and_user_id_and_tag_name(id, user.id, form_data.name, db=db)
        await publish_event(
            request,
            EVENTS.CHAT_TAG_REMOVED,
            actor=user,
            subject_id=id,
            data={'tag': form_data.name},
        )

        if await Chats.count_chats_by_tag_name_and_user_id(form_data.name, user.id, db=db) == 0:
            await Tags.delete_tag_by_name_and_user_id(form_data.name, user.id, db=db)

        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)
        tags = chat.meta.get('tags', [])
        return await Tags.get_tags_by_ids_and_user_id(tags, user.id, db=db)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)
