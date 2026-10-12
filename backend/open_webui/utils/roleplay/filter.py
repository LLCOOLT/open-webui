"""Built-in roleplay stage of the chat pipeline.

Core code, not a Function: it cannot be uninstalled or disabled per chat. ``process_chat_payload``
calls ``apply_character_system`` before context compaction, so the character block becomes the
system message that every later stage (folders, memory, RAG, tool loops) builds on, and
``apply_character_messages`` right after the inlet filters. The provider routers then skip the
model's stored ``params.system`` (see ``character_system_prompt_applied``), which would otherwise
add the character prompt a second time. That marker lives on ``request.state``, which clients
cannot set, never in the client-supplied request metadata.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.models.lorebooks import Lorebooks
from open_webui.utils.chat_id import is_saved_chat_id
from open_webui.utils.misc import get_content_from_message, get_last_user_message, get_system_message
from open_webui.utils.roleplay.character import CharacterMeta, DepthPrompt
from open_webui.utils.roleplay.cleanup import add_stop_sequences, clean_output
from open_webui.utils.roleplay.lorebook import Activation, activate
from open_webui.utils.roleplay.memory import memory_overrides
from open_webui.utils.roleplay.persona import persona_description, resolve_persona
from open_webui.utils.roleplay.prompt import (
    MacroContext,
    apply_character_history,
    apply_character_system_prompt,
    apply_world_info,
    build_macro_context,
    lore_depth_prompts,
    render_ooc_system_prompt,
)
from pydantic import ValidationError

log = logging.getLogger(__name__)


# Roleplay convention: "(OOC: ...)", "((...))" or "OOC: ..." marks an out-of-character message.
OOC_PREFIX_RE = re.compile(r'^\s*(?:\(\(|\(\s*ooc\b|\[\s*ooc\b|ooc\s*:)', re.IGNORECASE)


def is_ooc_text(text: str) -> bool:
    return bool(OOC_PREFIX_RE.match(text))


def get_model_character(model: dict) -> CharacterMeta | None:
    character = ((model or {}).get('info') or {}).get('meta', {}).get('character')
    if not isinstance(character, dict):
        return None
    try:
        return CharacterMeta(**character)
    except ValidationError:
        log.warning('Ignoring invalid character data on model %s', model.get('id'))
        return None


def skip_character_system_prompt(request, model_id: str | None) -> None:
    """For background tasks (expression picking, memory) sent to a character model: the provider
    routers must not add the character's stored system prompt, or the model would answer in character."""
    if model_id:
        request.state.roleplay_model_id = model_id


def roleplay_memory_settings(request) -> dict | None:
    """Compaction overrides for this request's character chat, if any."""
    state = getattr(getattr(request, 'state', None), 'roleplay_state', None)
    return state.memory if state is not None else None


def clean_character_reply(request, output: list) -> bool:
    """Apply the character's reply cleanup to a finished reply's output, in place. True if it changed."""
    state = getattr(getattr(request, 'state', None), 'roleplay_state', None)
    if state is None or state.character is None or state.ooc or not state.character.reply_cleanup.active():
        return False
    before = repr(output)
    try:
        clean_output(output, state.character.reply_cleanup, state.ctx.char, state.ctx.user)
    except Exception:
        log.exception('Reply cleanup failed')
        return False
    return repr(output) != before


def character_system_prompt_applied(request, model_id: str | None) -> bool:
    """True when this request's system message already carries ``model_id``'s character prompt."""
    applied = getattr(getattr(request, 'state', None), 'roleplay_model_id', None)
    return applied is not None and applied == model_id


@dataclass
class _RoleplayState:
    model_id: str
    ctx: MacroContext
    character: CharacterMeta | None
    books: list[dict]
    authors_note: DepthPrompt | None
    lore: Activation | None = None
    ooc: bool = False
    memory: dict | None = None  # compaction settings for this chat (see ``memory_overrides``)


def _authors_note(chat_meta: dict) -> DepthPrompt | None:
    note = chat_meta.get('authors_note')
    try:
        note = DepthPrompt(**note) if isinstance(note, dict) else None
    except ValidationError:
        return None
    return note if note and note.text.strip() else None


async def _load_lore(model: dict, character: CharacterMeta | None, metadata: dict, user) -> tuple[list[dict], dict]:
    """Lorebooks for this request (model's, then chat's) the user may read, plus the chat's meta."""
    chat_id = metadata.get('chat_id')
    chat_meta = {}
    if is_saved_chat_id(chat_id) and user is not None:
        chat_meta = await Chats.get_chat_meta_by_id_and_user_id(chat_id, user.id) or {}

    model_ids = ((model.get('info') or {}).get('meta') or {}).get('lorebook_ids') or []
    ids = list(dict.fromkeys([*model_ids, *(chat_meta.get('lorebook_ids') or [])]))
    lorebooks = await Lorebooks.get_readable_lorebooks_by_ids(ids, user.id) if ids and user is not None else []
    books = [lorebook.as_book() for lorebook in lorebooks]
    if character is not None and character.character_book:
        # Cards imported before lorebooks existed keep their book on the character.
        books.insert(0, character.character_book)
    return books, chat_meta


async def apply_character_system(request, form_data: dict, metadata: dict, user, model: dict) -> dict:
    """Build the request's roleplay state; for characters, also swap in the character block.

    The user's own system prompt becomes ``{{original}}``. Lorebook entries activated against the
    conversation go into the system block (before/after the description) or, with ``@@depth``, into
    the history in ``apply_character_messages``.
    """
    if not await Config.get('characters.enable'):
        return form_data
    character = get_model_character(model)
    books, chat_meta = await _load_lore(model, character, metadata, user)
    authors_note = _authors_note(chat_meta)
    if character is None and not books and authors_note is None:
        return form_data

    messages = form_data.get('messages', [])
    model_name = model.get('name') or model.get('id') or ''
    persona = resolve_persona(user, metadata.get('persona_id'), chat_meta.get('persona_id'))
    user_name = (persona or {}).get('name') or getattr(user, 'name', '') or ''
    persona_text = persona_description(persona)
    state = _RoleplayState(
        model_id=model.get('id'),
        ctx=build_macro_context(
            character or CharacterMeta(), model_name=model_name, user_name=user_name, persona=persona_text
        ),
        character=character,
        books=books,
        authors_note=authors_note,
    )

    if character is not None:
        system_message = get_system_message(messages[:1])
        original = (get_content_from_message(system_message) or '') if system_message else ''
        if not original.strip():
            original = await Config.get('characters.default_system_prompt') or ''
        state.ctx = build_macro_context(
            character,
            model_name=model_name,
            user_name=user_name,
            original=original,
            persona=persona_text,
            chat_id=metadata.get('chat_id') or '',
        )
        state.ooc = bool((metadata.get('features') or {}).get('ooc')) or is_ooc_text(
            get_last_user_message(messages) or ''
        )
        if state.ooc:
            # Out of character: no lorebooks, depth prompts or post-history for this turn.
            ooc_prompt = await Config.get('characters.ooc_prompt') or ''
            system = render_ooc_system_prompt(character, state.ctx, ooc_prompt)
            rest = messages[1:] if messages and messages[0].get('role') == 'system' else messages
            form_data['messages'] = [{'role': 'system', 'content': system}, *rest]
        else:
            state.lore = activate(books, messages) if books else None
            form_data['messages'] = apply_character_system_prompt(messages, character, state.ctx, state.lore)
            if character.reply_cleanup.stop_at_user:
                add_stop_sequences(form_data, model, state.ctx.user)
        request.state.roleplay_model_id = model.get('id')
        state.memory = await memory_overrides(state.ctx.char, state.ctx.user, chat_meta)

    # For filters and tools further down the pipeline; informational only.
    metadata['roleplay'] = {'model_id': model.get('id'), 'char': state.ctx.char if character else None}
    request.state.roleplay_state = state
    return form_data


def apply_character_messages(request, form_data: dict, model: dict) -> dict:
    """Example dialogue, depth prompts (character note, lorebook, author's note) and post-history."""
    state: _RoleplayState | None = getattr(request.state, 'roleplay_state', None)
    if state is None or state.model_id != model.get('id') or state.ooc:
        return form_data
    messages = form_data.get('messages', [])
    extra = [*([state.authors_note] if state.authors_note else [])]
    if state.character is not None and character_system_prompt_applied(request, state.model_id):
        form_data['messages'] = apply_character_history(
            messages, state.character, state.ctx, [*lore_depth_prompts(state.lore), *extra]
        )
    elif state.character is None:
        lore = activate(state.books, messages) if state.books else None
        form_data['messages'] = apply_world_info(messages, lore, state.ctx, extra)
    return form_data
