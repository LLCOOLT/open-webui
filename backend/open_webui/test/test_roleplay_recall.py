"""Character memory across chats (recall): storage, prompt, parsing, injection and the endpoints."""

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_webui.models.character_memories import MAX_ENTRIES, CharacterMemories, normalize_entries
from open_webui.models.chats import ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.routers import chats as chats_router
from open_webui.routers import models as models_router
from open_webui.test.test_roleplay_router import CARD
from open_webui.utils.auth import get_current_user
from open_webui.utils.roleplay.filter import apply_character_system
from open_webui.utils.roleplay.recall import (
    DEFAULT_RECALL_PROMPT,
    build_recall_prompt,
    messages_since,
    parse_recall_reply,
    recall_block,
    recall_source,
)

pytestmark = pytest.mark.asyncio(loop_scope='session')


####################
# Entries, prompt, parsing
####################


def test_normalize_entries_cleans_dedupes_and_caps():
    entries = normalize_entries(
        ['  Robin   lives in Portland. ', 'robin lives in portland.', '', 7, {'content': 'x' * 400}],
        chat_id='c1',
        now=5,
    )
    assert [entry['content'] for entry in entries] == ['Robin lives in Portland.', 'x' * 300]
    assert all(entry['chat_id'] == 'c1' and entry['created_at'] == 5 and len(entry['id']) == 12 for entry in entries)
    # Existing entries keep their id and times.
    kept = normalize_entries([{'id': 'abc', 'content': 'Old', 'created_at': 1, 'updated_at': 2, 'chat_id': 'c0'}])
    assert kept == [{'id': 'abc', 'content': 'Old', 'created_at': 1, 'updated_at': 2, 'chat_id': 'c0'}]
    assert len(normalize_entries([f'fact {i}' for i in range(MAX_ENTRIES + 10)])) == MAX_ENTRIES


def test_recall_prompt_and_source():
    existing = [{'id': 'a', 'content': 'Robin has a dog named Rex.'}]
    messages = [{'role': 'user', 'content': 'My sister is visiting.'}, {'role': 'assistant', 'content': '"Lovely."'}]
    source = recall_source('Story so far: they met.', messages, 'Sera', 'Robin')
    assert source.startswith('Story memory:\nStory so far: they met.')
    assert 'Recent messages:\nRobin: My sister is visiting.\n\nSera: "Lovely."' in source
    prompt = build_recall_prompt(DEFAULT_RECALL_PROMPT, existing, source, 'Sera', 'Robin')
    assert "Sera's long-term memory of Robin" in prompt
    assert '- Robin has a dog named Rex.' in prompt and source in prompt and f'At most {MAX_ENTRIES}' in prompt
    assert '{{' not in prompt.replace('{"entries": ["..."]}', '')
    assert '(nothing yet)' in build_recall_prompt(DEFAULT_RECALL_PROMPT, [], '', 'Sera', 'Robin')
    assert recall_source(None, [], 'Sera', 'Robin') == ''


def test_parse_recall_reply_keeps_known_entries_and_never_wipes():
    existing = [
        {'id': 'a', 'content': 'Robin has a dog named Rex.', 'chat_id': 'c0', 'created_at': 1, 'updated_at': 1},
        {'id': 'b', 'content': 'Robin dislikes rain.', 'chat_id': 'c0', 'created_at': 1, 'updated_at': 1},
    ]
    reply = (
        'Here you go:\n'
        '{"entries": ["robin has a dog named rex.", {"content": "Robin is visiting with her sister."}, ""]}'
    )
    entries = parse_recall_reply(reply, existing, chat_id='c1', now=9)
    assert [entry['content'] for entry in entries] == [
        'Robin has a dog named Rex.',
        'Robin is visiting with her sister.',
    ]
    assert entries[0]['id'] == 'a' and entries[0]['created_at'] == 1  # matched: keeps its identity
    assert entries[1]['chat_id'] == 'c1' and entries[1]['created_at'] == 9  # new: from this chat
    # An empty list keeps the memory; garbage raises.
    assert parse_recall_reply('{"entries": []}', existing, 'c1') == existing
    with pytest.raises(ValueError):
        parse_recall_reply('no json here', existing, 'c1')
    with pytest.raises(ValueError):
        parse_recall_reply('{"entries": "x"}', existing, 'c1')


def test_recall_block_and_messages_since():
    assert recall_block([], 'Sera', 'Robin') == ''
    block = recall_block([{'content': 'Robin has a dog.'}, {'content': 'Robin dislikes rain.'}], 'Sera', 'Robin')
    assert block == (
        'What Sera remembers about Robin from earlier conversations:\n- Robin has a dog.\n- Robin dislikes rain.'
    )
    ids = ['m0', 'm1', 'm2', 'm3']
    assert messages_since(ids, 'm1') == ['m2', 'm3']
    assert messages_since(ids, None) == ids
    assert messages_since(ids, 'gone') == ids  # another branch: everything counts


####################
# Injection into the character block
####################


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def people():
    suffix = uuid.uuid4().hex[:8]
    admin = await Users.insert_new_user(f'rc-{suffix}', 'Robin', f'rc-{suffix}@example.com', role='admin')
    other = await Users.insert_new_user(f'rd-{suffix}', 'Other', f'rd-{suffix}@example.com', role='user')
    return {'admin': admin, 'other': other}


@pytest_asyncio.fixture(loop_scope='session')
async def recall_config():
    await Config.upsert({'characters.enable': True, 'characters.recall.enable': True, 'characters.recall.prompt': ''})
    yield
    await Config.upsert({'characters.recall.enable': True})


def character_model(model_id: str, **character) -> dict:
    return {
        'id': model_id,
        'name': 'Sera',
        'info': {'meta': {'character': {'name': 'Sera', 'description': '{{char}} guards the forest.', **character}}},
    }


async def test_recall_goes_into_the_system_block(recall_config, people):
    user = people['admin']
    model_id = f'sera-{uuid.uuid4().hex[:6]}'
    await CharacterMemories.set_entries(user.id, model_id, ['Robin has a dog named Rex.'], chat_id='c0')
    try:

        async def system_for(model, metadata=None, text='Hi'):
            request = SimpleNamespace(state=SimpleNamespace())
            form = {'messages': [{'role': 'system', 'content': 'orig'}, {'role': 'user', 'content': text}]}
            form = await apply_character_system(request, form, metadata or {}, user, model)
            return form['messages'][0]['content']

        system = await system_for(character_model(model_id))
        assert 'What Sera remembers about Robin from earlier conversations:\n- Robin has a dog named Rex.' in system
        # After the description, before the example dialogue.
        assert system.index('guards the forest') < system.index('Robin has a dog')

        # Another user of the same character gets nothing of it.
        request = SimpleNamespace(state=SimpleNamespace())
        form = {'messages': [{'role': 'user', 'content': 'Hi'}]}
        form = await apply_character_system(request, form, {}, people['other'], character_model(model_id))
        assert 'remembers' not in form['messages'][0]['content']

        # Off for the character, the chat (meta) or the server: not injected.
        assert 'remembers' not in await system_for(character_model(model_id, recall_enabled=False))
        chat_id = str(uuid.uuid4())
        await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={'title': 'RP', 'history': {}}))
        await Chats.update_chat_meta_by_id_and_user_id(chat_id, user.id, {'recall_enabled': False})
        assert 'remembers' not in await system_for(character_model(model_id), {'chat_id': chat_id})
        await Config.upsert({'characters.recall.enable': False})
        assert 'remembers' not in await system_for(character_model(model_id))
        await Config.upsert({'characters.recall.enable': True})
        # Out of character: the reference block carries no memory either.
        assert 'remembers' not in await system_for(character_model(model_id), text='(OOC: what is the plan?)')
    finally:
        await CharacterMemories.delete_memory(user.id, model_id)


####################
# Endpoints
####################


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def recall_app(people):
    app = FastAPI()
    app.include_router(models_router.router, prefix='/api/v1/models')
    app.include_router(chats_router.router, prefix='/api/v1/chats')
    app.state.MODELS = {'base-model': {'id': 'base-model', 'name': 'Base', 'owned_by': 'openai'}}
    app.state.redis = None
    app.state.current_user = people['admin']
    app.dependency_overrides[get_current_user] = lambda: app.state.current_user
    return app


@pytest_asyncio.fixture(loop_scope='session')
async def api(recall_app, people, recall_config):
    recall_app.state.current_user = people['admin']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=recall_app), base_url='http://test') as client:
        yield client


@pytest_asyncio.fixture(loop_scope='session')
async def character_id(api, recall_app):
    response = await api.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.json', json.dumps(CARD).encode(), 'application/json'))],
        data={'base_model_id': 'base-model'},
    )
    assert response.status_code == 200, response.text
    model_id = response.json()[0]['id']
    recall_app.state.MODELS[model_id] = {'id': model_id, 'name': 'Seraphina'}
    yield model_id
    await Models.delete_model_by_id(model_id)


def chat_history(count: int, model_id: str) -> dict:
    messages = {}
    for index in range(count):
        messages[f'm{index}'] = {
            'id': f'm{index}',
            'parentId': f'm{index - 1}' if index else None,
            'childrenIds': [f'm{index + 1}'] if index < count - 1 else [],
            'role': 'user' if index % 2 == 0 else 'assistant',
            'content': f'Line {index}: my dog Rex says hi.' if index % 2 == 0 else f'Line {index}: "Hello, Rex."',
            **({'model': model_id} if index % 2 else {}),
        }
    return {'messages': messages, 'currentId': f'm{count - 1}'}


async def test_recall_endpoints(api, character_id, recall_app, people, monkeypatch):
    user = people['admin']
    calls = []

    async def fake_task(request, user, model_id, prompt, task, direct=False):
        calls.append({'task': task, 'prompt': prompt, 'skip': getattr(request.state, 'roleplay_model_id', None)})
        return '{"entries": ["Robin has a dog named Rex.", "Robin greets Seraphina warmly."]}'

    async def no_tasks(redis, chat_id):
        return False

    monkeypatch.setattr(chats_router, 'run_task_prompt', fake_task)
    monkeypatch.setattr(chats_router, 'has_active_tasks', no_tasks)
    chat_id = str(uuid.uuid4())
    await Chats.insert_new_chat(
        chat_id,
        user.id,
        ChatForm(chat={'title': 'RP', 'models': [character_id], 'history': chat_history(6, character_id)}),
    )
    base = f'/api/v1/chats/{chat_id}/roleplay/recall'

    # Nothing yet.
    state = (await api.get(base)).json()
    assert state == {
        'available': True,
        'enabled': True,
        'character_enabled': True,
        'model_id': character_id,
        'char': 'Seraphina',
        'entries': [],
        'updated': False,
        'new_messages': 6,
    }

    # Automatic: too few new messages, no model call.
    state = (await api.post(f'{base}/refresh', json={'auto': True})).json()
    assert state['updated'] is False and state['entries'] == [] and not calls

    # On demand: the task model gets the transcript and no character prompt; the memory is saved and
    # the chat remembers where it left off.
    response = await api.post(f'{base}/refresh', json={'auto': False})
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['updated'] is True and state['new_messages'] == 0
    assert [entry['content'] for entry in state['entries']] == [
        'Robin has a dog named Rex.',
        'Robin greets Seraphina warmly.',
    ]
    assert all(entry['chat_id'] == chat_id for entry in state['entries'])
    assert calls[0]['task'] == 'character_recall' and calls[0]['skip'] == character_id
    assert 'Robin: Line 0: my dog Rex says hi.' in calls[0]['prompt'] and '(nothing yet)' in calls[0]['prompt']
    assert (await Chats.get_chat_meta_by_id_and_user_id(chat_id, user.id))['recall_message_id'] == 'm5'
    assert (await api.get(base)).json()['new_messages'] == 0

    # The memory follows the user into a new chat with the same character.
    other_chat = str(uuid.uuid4())
    await Chats.insert_new_chat(
        other_chat,
        user.id,
        ChatForm(chat={'title': 'RP 2', 'models': [character_id], 'history': chat_history(2, character_id)}),
    )
    assert len((await api.get(f'/api/v1/chats/{other_chat}/roleplay/recall')).json()['entries']) == 2

    # Automatic again once enough messages were added: existing entries go into the prompt.
    for index in range(6, 16):
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            f'm{index}',
            {
                'parentId': f'm{index - 1}',
                'role': 'user' if index % 2 == 0 else 'assistant',
                'content': f'Line {index}',
                **({'model': character_id} if index % 2 else {}),
            },
        )
    state = (await api.post(f'{base}/refresh', json={'auto': True})).json()
    assert len(calls) == 2 and '- Robin has a dog named Rex.' in calls[1]['prompt']
    assert 'Line 0' not in calls[1]['prompt'] and 'Line 15' in calls[1]['prompt']
    assert state['updated'] is False  # same entries came back

    # Edit: known ids keep their identity, new lines are added, dropped ones are forgotten.
    first = state['entries'][0]
    state = (
        await api.post(
            base,
            json={
                'entries': [
                    {'id': first['id'], 'content': 'Robin has a dog named Rex (a corgi).'},
                    {'content': 'Robin bakes.'},
                ]
            },
        )
    ).json()
    assert [entry['content'] for entry in state['entries']] == ['Robin has a dog named Rex (a corgi).', 'Robin bakes.']
    assert state['entries'][0]['id'] == first['id'] and state['entries'][0]['created_at'] == first['created_at']

    # Another user sees nothing of it.
    recall_app.state.current_user = people['other']
    assert (await api.get(base)).status_code == 404  # not their chat
    recall_app.state.current_user = user
    assert await CharacterMemories.get_entries(people['other'].id, character_id) == []

    # Off for this chat: automatic updates stop quietly, on-demand ones say why.
    settings = (await api.get(f'/api/v1/chats/{chat_id}/roleplay')).json()
    assert settings['recall_enabled'] is True
    await api.post(f'/api/v1/chats/{chat_id}/roleplay', json={**settings, 'recall_enabled': False})
    assert (await api.post(f'{base}/refresh', json={'auto': True})).json()['enabled'] is False
    assert (await api.post(f'{base}/refresh', json={'auto': False})).status_code == 400
    await api.post(f'/api/v1/chats/{chat_id}/roleplay', json={**settings, 'recall_enabled': True})

    # Nothing new since the last update: on demand says so, automatic stays quiet.
    assert (await api.post(f'{base}/refresh', json={'auto': False})).status_code == 400
    assert (await api.post(f'{base}/refresh', json={'auto': True})).json()['updated'] is False

    # A model that does not answer leaves the memory alone.
    async def broken(request, user, model_id, prompt, task, direct=False):
        return 'I cannot.'

    monkeypatch.setattr(chats_router, 'run_task_prompt', broken)
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, 'm16', {'parentId': 'm15', 'role': 'user', 'content': 'Line 16'}
    )
    assert (await api.post(f'{base}/refresh', json={'auto': False})).status_code == 502
    assert len(await CharacterMemories.get_entries(user.id, character_id)) == 2

    # Forget everything.
    assert (await api.post(base, json={'entries': []})).json()['entries'] == []

    # Server switch off: hidden from the chat.
    await Config.upsert({'characters.recall.enable': False})
    assert (await api.get(base)).json()['available'] is False
    assert (await api.post(f'{base}/refresh', json={'auto': False})).status_code == 400
    await Config.upsert({'characters.recall.enable': True})


async def test_deleting_a_character_forgets_everyone(api, recall_app, people):
    response = await api.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.json', json.dumps(CARD).encode(), 'application/json'))],
        data={'base_model_id': 'base-model'},
    )
    model_id = response.json()[0]['id']
    await CharacterMemories.set_entries(people['admin'].id, model_id, ['Robin bakes.'])
    await CharacterMemories.set_entries(people['other'].id, model_id, ['Other sings.'])
    assert (await api.post('/api/v1/models/model/delete', json={'id': model_id})).status_code == 200
    assert await CharacterMemories.get_entries(people['admin'].id, model_id) == []
    assert await CharacterMemories.get_entries(people['other'].id, model_id) == []


def test_character_summary_carries_the_recall_switch():
    from open_webui.utils.roleplay.character import CharacterMeta, character_summary

    assert character_summary(CharacterMeta(name='Sera').model_dump())['recall_enabled'] is True
    assert character_summary({'name': 'Sera', 'recall_enabled': False})['recall_enabled'] is False
