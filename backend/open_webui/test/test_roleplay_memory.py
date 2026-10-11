"""Long-chat memory: settings, compaction overrides, and the chat memory endpoints."""

import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_webui.models.chats import ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.routers import chats as chats_router
from open_webui.utils import context_compaction
from open_webui.utils.auth import get_current_user
from open_webui.utils.roleplay.filter import apply_character_system, roleplay_memory_settings
from open_webui.utils.roleplay.memory import (
    DEFAULT_MEMORY_PROMPT,
    MEMORY_LABEL,
    branch_message_ids,
    latest_memory,
    memory_overrides,
)

pytestmark = pytest.mark.asyncio(loop_scope='session')

CHARACTER_MODEL = {
    'id': 'mira-model',
    'name': 'Mira',
    'info': {'meta': {'character': {'name': 'Mira', 'description': '{{char}} cooks.'}}},
}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def user():
    suffix = uuid.uuid4().hex[:8]
    return await Users.insert_new_user(f'rp-{suffix}', 'Robin', f'rp-{suffix}@example.com', role='user')


@pytest_asyncio.fixture(loop_scope='session')
async def memory_config():
    await Config.upsert(
        {
            'characters.enable': True,
            'characters.memory.enable': True,
            'characters.memory.token_threshold': 1000,
            'characters.memory.prompt': '',
        }
    )
    yield
    await Config.upsert({'characters.memory.token_threshold': 12000})


def history(count: int, size: int = 0) -> dict:
    """A linear chat of ``count`` alternating messages, ``m0`` .. ``m{count-1}``."""
    messages = {}
    for index in range(count):
        messages[f'm{index}'] = {
            'id': f'm{index}',
            'parentId': f'm{index - 1}' if index else None,
            'childrenIds': [f'm{index + 1}'] if index < count - 1 else [],
            'role': 'user' if index % 2 == 0 else 'assistant',
            'content': f'Message {index}. ' + 'x' * size,
            **({'model': 'mira-model'} if index % 2 else {}),
        }
    return {'messages': messages, 'currentId': f'm{count - 1}'}


def test_latest_memory_follows_the_current_branch():
    messages = history(6)['messages']
    messages['m2']['contextSummary'] = 'old'
    messages['m4']['contextSummary'] = 'new'
    messages['b3'] = {'parentId': 'm2', 'contextSummary': 'other branch'}
    assert branch_message_ids(messages, 'm5') == ['m0', 'm1', 'm2', 'm3', 'm4', 'm5']
    assert latest_memory(messages, 'm5') == {
        'summary': 'new',
        'message_id': 'm4',
        'summarized_messages': 4,
        'total_messages': 6,
    }
    assert latest_memory(messages, 'b3')['summary'] == 'other branch'
    assert latest_memory(messages, 'm1')['summary'] is None


async def test_memory_overrides(memory_config):
    settings = await memory_overrides('Mira', 'Robin', {})
    assert settings['enable'] is True
    assert settings['token_threshold'] == settings['token_cap'] == 1000
    assert settings['label'] == MEMORY_LABEL
    assert 'between Mira and Robin' in settings['prompt_template']
    assert '{{char}}' not in settings['prompt_template'] and '{{COMPACTED_MESSAGES}}' in settings['prompt_template']

    # Turned off for one chat: no summarizing there, whatever compaction is set to.
    assert await memory_overrides('Mira', 'Robin', {'memory_enabled': False}) == {'enable': False}

    await Config.upsert({'characters.memory.prompt': 'Remember {{char}} and {{user}}: {{COMPACTED_MESSAGES}}'})
    assert (await memory_overrides('Mira', 'Robin', {}))['prompt_template'] == (
        'Remember Mira and Robin: {{COMPACTED_MESSAGES}}'
    )
    await Config.upsert({'characters.memory.prompt': '', 'characters.memory.enable': False})
    assert await memory_overrides('Mira', 'Robin', {}) is None
    await Config.upsert({'characters.memory.enable': True})


async def test_character_requests_carry_memory_settings(memory_config, user):
    request = SimpleNamespace(state=SimpleNamespace())
    form_data = {'messages': [{'role': 'user', 'content': 'Hi'}]}
    await apply_character_system(request, form_data, {}, user, CHARACTER_MODEL)
    settings = roleplay_memory_settings(request)
    assert settings['enable'] is True and 'between Mira and Robin' in settings['prompt_template']

    # Plain models get no roleplay memory; compaction keeps its own settings.
    plain = SimpleNamespace(state=SimpleNamespace())
    await apply_character_system(plain, {'messages': []}, {}, user, {'id': 'plain', 'info': {'meta': {}}})
    assert roleplay_memory_settings(plain) is None


async def test_compaction_uses_the_overrides(memory_config, monkeypatch):
    captured = {}

    async def fake_summary(request, user, model_id, models, compacted, recent, previous, template):
        captured.update(template=template, compacted=len(compacted), recent=len(recent))
        return 'Mira and Robin met.'

    monkeypatch.setattr(context_compaction, '_generate_summary', fake_summary)
    messages = [
        {'role': 'system', 'content': 'Character block'},
        *[
            {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'{i} ' + 'x' * 800, 'id': f'm{i}'}
            for i in range(10)
        ],
    ]
    settings = await memory_overrides('Mira', 'Robin', {})
    settings.pop('label')

    # Global compaction is off (its default), yet the character chat is summarized.
    kept, summary, compacted = await context_compaction.compact_messages_for_request(
        None, None, messages, {}, 'mira-model', {}, 'Character block', overrides=settings
    )
    assert compacted is True and summary == 'Mira and Robin met.'
    assert 'between Mira and Robin' in captured['template']
    assert kept[0]['content'] == 'Character block'
    assert len(kept) == 1 + captured['recent'] and captured['compacted'] + captured['recent'] == 10

    # Under the threshold nothing happens; with memory off for the chat nothing happens either.
    short = messages[:3]
    assert (
        await context_compaction.compact_messages_for_request(
            None, None, short, {}, 'mira-model', {}, '', overrides=settings
        )
    )[2] is False
    assert (
        await context_compaction.compact_messages_for_request(
            None, None, messages, {}, 'mira-model', {}, '', overrides={'enable': False}
        )
    )[2] is False


def test_merge_history_keeps_server_written_summaries():
    stored = history(4)
    stored['messages']['m2']['contextSummary'] = 'Story so far'
    incoming = history(5)  # the client's copy never had the summary
    merged = Chats.merge_history(stored, incoming)
    assert merged['messages']['m2']['contextSummary'] == 'Story so far'
    assert merged['messages']['m4']['content'] == 'Message 4. '

    cleared = history(4)
    cleared['messages']['m2']['contextSummary'] = ''
    assert Chats.merge_history(stored, cleared)['messages']['m2']['contextSummary'] == ''


@pytest_asyncio.fixture(loop_scope='session')
async def chat_client(user):
    app = FastAPI()
    app.include_router(chats_router.router, prefix='/api/v1/chats')
    app.state.MODELS = {'mira-model': CHARACTER_MODEL}
    app.state.redis = None
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def test_memory_endpoints(memory_config, user, chat_client, monkeypatch):
    calls = []

    async def fake_summary(request, user, model_id, models, compacted, recent, previous, template):
        calls.append({'template': template, 'compacted': len(compacted), 'previous': previous})
        return f'Summary {len(calls)}'

    async def no_tasks(redis, chat_id):
        return False

    monkeypatch.setattr(context_compaction, '_generate_summary', fake_summary)
    monkeypatch.setattr(chats_router, 'has_active_tasks', no_tasks)
    chat_id = str(uuid.uuid4())
    await Chats.insert_new_chat(
        chat_id, user.id, ChatForm(chat={'title': 'RP', 'models': ['mira-model'], 'history': history(12)})
    )

    response = await chat_client.get(f'/api/v1/chats/{chat_id}/roleplay/memory')
    assert response.status_code == 200
    assert response.json() == {
        'available': True,
        'enabled': True,
        'summary': None,
        'message_id': None,
        'summarized_messages': 0,
        'total_messages': 12,
        'token_threshold': 1000,
    }
    # Nothing to edit yet.
    assert (
        await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay/memory', json={'summary': 'x'})
    ).status_code == 400

    # Update now: the older messages are summarized with the story prompt; recent ones stay.
    memory = (await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay/memory/refresh')).json()
    assert memory['summary'] == 'Summary 1'
    assert 0 < memory['summarized_messages'] < 12
    assert memory['message_id'] == f'm{memory["summarized_messages"]}'
    assert 'between Mira and Robin' in calls[0]['template']

    # Edit it.
    memory = (
        await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay/memory', json={'summary': 'Mira burned the stew.'})
    ).json()
    assert memory['summary'] == 'Mira burned the stew.'

    # A second refresh builds on the edited memory.
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, 'm12', {'parentId': 'm11', 'role': 'user', 'content': 'More ' + 'y' * 50}
    )
    memory = (await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay/memory/refresh')).json()
    assert calls[1]['previous'] == 'Mira burned the stew.'
    assert memory['summary'] == 'Summary 2'

    # Clearing removes every checkpoint, so the full history is sent again.
    memory = (await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay/memory', json={'summary': ''})).json()
    assert memory['summary'] is None and memory['message_id'] is None

    # The per-chat switch is part of the roleplay settings.
    settings = (await chat_client.get(f'/api/v1/chats/{chat_id}/roleplay')).json()
    assert settings['memory_enabled'] is True
    settings = (
        await chat_client.post(f'/api/v1/chats/{chat_id}/roleplay', json={**settings, 'memory_enabled': False})
    ).json()
    assert settings['memory_enabled'] is False
    assert (await chat_client.get(f'/api/v1/chats/{chat_id}/roleplay/memory')).json()['enabled'] is False
