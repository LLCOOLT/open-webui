"""Character card endpoints against a real (temporary SQLite) database."""

import base64
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_webui.models.config import Config
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.routers import models as models_router
from open_webui.utils.auth import get_current_user
from open_webui.utils.roleplay.cards import parse_card_bytes, read_png_chunks

pytestmark = pytest.mark.asyncio(loop_scope='session')

CARD = {
    'spec': 'chara_card_v2',
    'spec_version': '2.0',
    'data': {
        'name': 'Seraphina',
        'description': '{{char}} guards the forest.',
        'personality': 'gentle',
        'scenario': '',
        'first_mes': 'Welcome, {{user}}.',
        'mes_example': '',
        'creator_notes': 'A kind guardian.',
        'system_prompt': '',
        'post_history_instructions': '',
        'alternate_greetings': ['Back again, {{user}}?'],
        'tags': ['fantasy'],
        'creator': 'tester',
        'character_version': '1',
        'extensions': {'kept': True},
    },
}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def users():
    suffix = uuid.uuid4().hex[:8]
    admin = await Users.insert_new_user(f'admin-{suffix}', 'Admin', f'admin-{suffix}@example.com', role='admin')
    member = await Users.insert_new_user(f'user-{suffix}', 'Member', f'user-{suffix}@example.com', role='user')
    return {'admin': admin, 'user': member}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def app(users):
    app = FastAPI()
    app.include_router(models_router.router, prefix='/api/v1/models')
    app.state.MODELS = {'base-model': {'id': 'base-model', 'name': 'Base', 'owned_by': 'openai'}}
    app.state.current_user = users['admin']
    app.dependency_overrides[get_current_user] = lambda: app.state.current_user
    return app


@pytest_asyncio.fixture(loop_scope='session')
async def client(app, users):
    app.state.current_user = users['admin']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


def card_png(png_factory) -> bytes:
    return png_factory(width=8, height=12, text_chunks={'chara': base64.b64encode(json.dumps(CARD).encode()).decode()})


async def import_card(client, png_factory, **form):
    return await client.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.png', card_png(png_factory), 'image/png'))],
        data={'base_model_id': 'base-model', **form},
    )


async def test_non_admin_cannot_import(app, client, users, png_factory):
    app.state.current_user = users['user']
    response = await import_card(client, png_factory)
    assert response.status_code == 401
    assert not [model for model in await Models.get_all_models() if model.meta.character]


async def test_unknown_base_model_is_rejected(client, png_factory):
    response = await client.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.png', card_png(png_factory), 'image/png'))],
        data={'base_model_id': 'missing'},
    )
    assert response.status_code == 400


async def test_invalid_card_is_rejected(client):
    response = await client.post(
        '/api/v1/models/characters/import',
        files=[('files', ('bad.json', b'{"foo": 1}', 'application/json'))],
        data={'base_model_id': 'base-model'},
    )
    assert response.status_code == 400
    assert 'bad.json' in response.json()['detail']


async def test_admin_import_export_and_greetings(app, client, users, png_factory):
    response = await import_card(client, png_factory)
    assert response.status_code == 200, response.text
    [created] = response.json()
    assert created['id'].startswith('seraphina-')
    assert created['base_model_id'] == 'base-model'
    assert created['access_grants'] == []

    model = await Models.get_model_by_id(created['id'])
    character = model.meta.character
    assert character.name == 'Seraphina'
    assert character.avatar_file_id
    assert model.meta.profile_image_url.startswith('data:image/webp;base64,')
    assert model.meta.description == 'A kind guardian.'
    assert [tag['name'] for tag in model.meta.tags] == ['fantasy']
    assert model.params.model_dump()['system'] == "Seraphina guards the forest.\n\nSeraphina's personality: gentle"

    response = await client.get('/api/v1/models/model/character/greetings', params={'id': created['id']})
    assert response.json() == ['Welcome, Admin.', 'Back again, Admin?']

    response = await client.get('/api/v1/models/model/character/export', params={'id': created['id']})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    exported = response.content
    keywords = [data.split(b'\x00', 1)[0] for chunk_type, data in read_png_chunks(exported) if chunk_type == b'tEXt']
    assert sorted(keywords) == [b'ccv3', b'chara']
    reimported = parse_card_bytes(exported)
    assert reimported.character.spec_version == '3.0'
    assert reimported.character.extensions == {'kept': True}
    assert reimported.avatar_content_type == 'image/png'

    response = await client.get('/api/v1/models/model/character/export', params={'id': created['id'], 'format': 'json'})
    assert response.json()['data']['first_mes'] == 'Welcome, {{user}}.'

    # Private by default: other users can neither read greetings nor export.
    app.state.current_user = users['user']
    for path in ('greetings', 'export'):
        response = await client.get(f'/api/v1/models/model/character/{path}', params={'id': created['id']})
        assert response.status_code == 404


async def test_public_import_shows_only_a_summary_to_readers(app, client, users, png_factory):
    response = await import_card(client, png_factory, public='true')
    created = response.json()[0]

    app.state.current_user = users['user']
    response = await client.get('/api/v1/models/model', params={'id': created['id']})
    assert response.status_code == 200
    data = response.json()
    assert data['params'] == {}
    assert data['meta']['character']['description'] == ''
    assert data['meta']['character']['greeting_count'] == 2

    response = await client.get('/api/v1/models/model/character/greetings', params={'id': created['id']})
    assert response.json() == ['Welcome, Member.', 'Back again, Member?']
    # Exporting needs write access.
    response = await client.get('/api/v1/models/model/character/export', params={'id': created['id']})
    assert response.status_code == 404


async def test_disabled_feature(client, png_factory):
    await Config.upsert({'characters.enable': False})
    try:
        response = await import_card(client, png_factory)
        assert response.status_code == 403
    finally:
        await Config.upsert({'characters.enable': True})


async def test_editing_character_fields_recompiles_the_system_prompt(client, png_factory):
    created = (await import_card(client, png_factory)).json()[0]
    model = (await client.get('/api/v1/models/model', params={'id': created['id']})).json()
    model['meta']['character']['personality'] = 'stern'
    model['params'] = {'system': 'ignored, compiled from the character'}
    response = await client.post('/api/v1/models/model/update', json=model)
    assert response.status_code == 200, response.text
    assert response.json()['params']['system'].endswith("Seraphina's personality: stern")


async def test_chat_pipeline_stages(users):
    from types import SimpleNamespace

    from open_webui.utils.misc import merge_system_messages
    from open_webui.utils.roleplay.filter import (
        apply_character_messages,
        apply_character_system,
        character_system_prompt_applied,
    )

    model = {
        'id': 'sera',
        'name': 'Sera',
        'info': {
            'meta': {
                'character': {
                    'name': 'Seraphina',
                    'description': '{{char}} meets {{user}}.',
                    'post_history_instructions': 'Be brief.',
                }
            }
        },
    }
    request = SimpleNamespace(state=SimpleNamespace())
    metadata = {'chat_id': 'chat-1'}
    form_data = {'messages': [{'role': 'system', 'content': 'My own prompt.'}, {'role': 'user', 'content': 'Hello'}]}

    form_data = await apply_character_system(request, form_data, metadata, users['user'], model)
    form_data = apply_character_messages(request, form_data, model)

    assert form_data['messages'] == [
        {'role': 'system', 'content': 'My own prompt.\n\nSeraphina meets Member.'},
        {'role': 'user', 'content': 'Hello\n\nBe brief.'},
    ]
    # The pipeline folds all system messages into the first before sending; nothing may move.
    assert merge_system_messages(form_data['messages']) == form_data['messages']
    assert metadata['roleplay'] == {'model_id': 'sera', 'char': 'Seraphina'}
    assert character_system_prompt_applied(request, 'sera')
    assert not character_system_prompt_applied(request, 'other-model')
    # Client-supplied metadata cannot switch off a model's system prompt.
    assert not character_system_prompt_applied(SimpleNamespace(state=SimpleNamespace()), 'sera')

    # Without a user system prompt the configured default stands in for {{original}}.
    request = SimpleNamespace(state=SimpleNamespace())
    form_data = await apply_character_system(
        request, {'messages': [{'role': 'user', 'content': 'Hi'}]}, {}, users['user'], model
    )
    assert form_data['messages'][0]['content'].startswith("Write Seraphina's next reply")
    assert 'Member' in form_data['messages'][0]['content']

    # Plain models pass through untouched.
    plain = {'id': 'plain', 'info': {'meta': {}}}
    untouched = {'messages': [{'role': 'system', 'content': 'S'}]}
    assert await apply_character_system(request, dict(untouched), {}, users['user'], plain) == untouched


BOOK_CARD = {
    'spec': 'chara_card_v2',
    'data': {
        **CARD['data'],
        'name': 'Warden',
        'character_book': {
            'name': 'Warden lore',
            'entries': [
                {'keys': ['glade'], 'content': 'The glade is warded.', 'extensions': {}},
                {'keys': ['storm'], 'content': '@@depth 0\nA storm gathers.', 'extensions': {}},
            ],
        },
    },
}


async def test_embedded_book_becomes_a_linked_lorebook(client, png_factory):
    from open_webui.models.lorebooks import Lorebooks

    png = png_factory(text_chunks={'chara': base64.b64encode(json.dumps(BOOK_CARD).encode()).decode()})
    response = await client.post(
        '/api/v1/models/characters/import',
        files=[('files', ('warden.png', png, 'image/png'))],
        data={'base_model_id': 'base-model', 'public': 'true'},
    )
    assert response.status_code == 200, response.text
    model = await Models.get_model_by_id(response.json()[0]['id'])
    assert model.meta.character.character_book is None
    [lorebook_id] = model.meta.lorebook_ids

    lorebook = await Lorebooks.get_lorebook_by_id(lorebook_id)
    assert lorebook.name == 'Warden lore'
    assert len(lorebook.data['entries']) == 2
    # Shared like the model it came with.
    assert [(grant.principal_id, grant.permission) for grant in lorebook.access_grants] == [('*', 'read')]

    exported = await client.get('/api/v1/models/model/character/export', params={'id': model.id, 'format': 'json'})
    book = exported.json()['data']['character_book']
    assert [entry['content'] for entry in book['entries']] == ['The glade is warded.', '@@depth 0\nA storm gathers.']


async def test_pipeline_uses_model_and_chat_lorebooks(users):
    from types import SimpleNamespace

    from open_webui.models.chats import ChatForm, Chats
    from open_webui.models.lorebooks import LorebookForm, Lorebooks
    from open_webui.utils.roleplay.filter import apply_character_messages, apply_character_system

    member = users['user']
    lore = await Lorebooks.insert_new_lorebook(
        member.id,
        LorebookForm(
            name='Mine',
            data={
                'entries': [
                    {'keys': ['glade'], 'content': 'Glade lore for {{user}}.'},
                    {'keys': ['glade'], 'content': 'Later lore.', 'position': 'after_char'},
                    {'keys': ['storm'], 'content': '@@depth 0\nA storm gathers.'},
                ]
            },
        ),
    )
    private = await Lorebooks.insert_new_lorebook(
        users['admin'].id, LorebookForm(name='Private', data={'entries': [{'constant': True, 'content': 'SECRET'}]})
    )
    chat_lore = await Lorebooks.insert_new_lorebook(
        member.id, LorebookForm(name='Chat', data={'entries': [{'keys': ['glade'], 'content': 'Chat lore.'}]})
    )
    chat_id = str(uuid.uuid4())
    await Chats.insert_new_chat(chat_id, member.id, ChatForm(chat={'title': 'x', 'history': {'messages': {}}}))
    await Chats.update_chat_meta_by_id_and_user_id(
        chat_id,
        member.id,
        {'lorebook_ids': [chat_lore.id], 'authors_note': {'text': 'Note for {{char}}.', 'depth': 1, 'role': 'system'}},
    )

    model = {
        'id': 'sera-lore',
        'name': 'Sera',
        'info': {
            'meta': {
                'character': {'name': 'Seraphina', 'description': 'Guardian.'},
                'lorebook_ids': [lore.id, private.id],
            }
        },
    }
    messages = [
        {'role': 'system', 'content': 'Base.'},
        {'role': 'assistant', 'content': 'Hi.'},
        {'role': 'user', 'content': 'Is this the glade? A storm?'},
    ]
    request = SimpleNamespace(state=SimpleNamespace())
    form_data = await apply_character_system(request, {'messages': messages}, {'chat_id': chat_id}, member, model)
    form_data = apply_character_messages(request, form_data, model)

    assert form_data['messages'] == [
        {
            'role': 'system',
            'content': 'Base.\n\nGlade lore for Member.\n\nChat lore.\n\nGuardian.\n\nLater lore.',
        },
        {'role': 'assistant', 'content': 'Hi.'},
        {'role': 'user', 'content': 'Note for Seraphina.\n\nIs this the glade? A storm?\n\nA storm gathers.'},
    ]
    # The admin's private lorebook is attached to the model but the member cannot read it.
    assert 'SECRET' not in str(form_data['messages'])

    # A plain model gets the chat's lorebooks and author's note too.
    plain = {'id': 'plain-lore', 'name': 'Plain', 'info': {'meta': {}}}
    request = SimpleNamespace(state=SimpleNamespace())
    form_data = await apply_character_system(request, {'messages': list(messages)}, {'chat_id': chat_id}, member, plain)
    form_data = apply_character_messages(request, form_data, plain)
    assert form_data['messages'][0] == {'role': 'system', 'content': 'Base.\n\nChat lore.'}
    assert form_data['messages'][-1]['content'] == 'Note for Plain.\n\nIs this the glade? A storm?'


async def test_convert_system_prompt(app, client, users, monkeypatch):
    prompt = 'You are Kai, a calm monk.\n\nPersonality: patient\n\nRules:\n- Speak softly.'
    response = await client.post('/api/v1/models/characters/convert', json={'system': prompt, 'name': 'monk'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['method'] == 'parser'
    assert body['character']['name'] == 'Kai'
    assert body['character']['personality'] == 'patient'
    assert body['character']['system_prompt'] == '- Speak softly.'
    assert body['detected'] == ['personality', 'system_prompt']

    # Model-assisted: the task model's JSON reply becomes the character.
    sent = {}

    async def fake_completion(request, form_data, user, **kwargs):
        sent.update(form_data)
        return {'choices': [{'message': {'content': '{"name": "Kai", "description": "{{char}} is a monk."}'}}]}

    import open_webui.utils.chat

    monkeypatch.setattr(open_webui.utils.chat, 'generate_chat_completion', fake_completion)
    response = await client.post(
        '/api/v1/models/characters/convert',
        json={'system': 'A calm monk named Kai.', 'base_model_id': 'base-model', 'use_ai': True},
    )
    assert response.status_code == 200, response.text
    assert response.json()['method'] == 'ai'
    assert response.json()['character']['description'] == '{{char}} is a monk.'
    assert 'A calm monk named Kai.' in sent['messages'][0]['content']

    async def bad_completion(request, form_data, user, **kwargs):
        return {'choices': [{'message': {'content': 'I cannot do that.'}}]}

    monkeypatch.setattr(open_webui.utils.chat, 'generate_chat_completion', bad_completion)
    response = await client.post(
        '/api/v1/models/characters/convert',
        json={'system': 'x', 'base_model_id': 'base-model', 'use_ai': True},
    )
    assert response.status_code == 502

    # Needs the models workspace permission (or admin).
    app.state.current_user = users['user']
    response = await client.post('/api/v1/models/characters/convert', json={'system': prompt})
    assert response.status_code == 401


async def test_story_endpoints(app, client, users, monkeypatch):
    import open_webui.utils.chat
    from open_webui.models.lorebooks import Lorebooks

    async def fake_completion(request, form_data, user, **kwargs):
        prompt = form_data['messages'][0]['content']
        if prompt.startswith('Write the opening message'):
            content = '*Elena looks up from her map.* "You found me, {{user}}."'
        else:
            content = json.dumps(
                {
                    'characters': [{'name': 'Elena', 'description': 'A cartographer.', 'quotes': ['Maps never lie.']}],
                    'world': [{'name': 'Port Vey', 'kind': 'place', 'content': 'A harbor.'}],
                    'events': ['Elena boards the Gull.'],
                }
            )
        return {'choices': [{'message': {'content': content}}]}

    monkeypatch.setattr(open_webui.utils.chat, 'generate_chat_completion', fake_completion)

    response = await client.post(
        '/api/v1/models/characters/story/analyze',
        json={'text': 'Once upon a time.', 'title': 'Tides', 'base_model_id': 'base-model'},
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['characters'][0]['name'] == 'Elena'
    assert draft['lorebook']['entries'][0]['keys'] == ['Port Vey']

    response = await client.post(
        '/api/v1/models/characters/story/greeting',
        json={'base_model_id': 'base-model', 'character': draft['characters'][0], 'scenario': 'On the Gull.'},
    )
    first_mes = response.json()['first_mes']
    assert first_mes.startswith('*Elena looks up')

    character = {**draft['characters'][0], 'first_mes': first_mes}
    response = await client.post(
        '/api/v1/models/characters/story/create',
        json={
            'title': 'Tides',
            'base_model_id': 'base-model',
            'scenario': '{{user}} joins the crew of the Gull.',
            'characters': [character],
            'lorebook': draft['lorebook'],
        },
    )
    assert response.status_code == 200, response.text
    [created] = response.json()
    model = await Models.get_model_by_id(created['id'])
    assert model.meta.character.scenario == '{{user}} joins the crew of the Gull.'
    assert model.meta.character.first_mes == first_mes
    assert model.meta.character.source == ['Tides']
    lorebook = await Lorebooks.get_lorebook_by_id(model.meta.lorebook_ids[0])
    assert lorebook.name == 'Tides world'

    # Admin only, like card import.
    app.state.current_user = users['user']
    response = await client.post(
        '/api/v1/models/characters/story/analyze',
        json={'text': 'x', 'base_model_id': 'base-model'},
    )
    assert response.status_code == 401


async def test_personas_and_out_of_character(users):
    from types import SimpleNamespace

    from open_webui.utils.roleplay.filter import apply_character_messages, apply_character_system

    personas = [
        {'id': 'p-knight', 'name': 'Sir Aldo', 'description': 'A tired knight.', 'is_default': True},
        {'id': 'p-bard', 'name': 'Wren', 'description': 'A wandering bard.'},
    ]
    member = await Users.update_user_settings_by_id(users['user'].id, {'ui': {'personas': personas}})
    model = {
        'id': 'sera-persona',
        'name': 'Sera',
        'info': {
            'meta': {
                'character': {
                    'name': 'Seraphina',
                    'description': '{{char}} greets {{user}}.',
                    'post_history_instructions': 'Be brief.',
                }
            }
        },
    }

    async def run(metadata, messages=None):
        request = SimpleNamespace(state=SimpleNamespace())
        form_data = {'messages': messages or [{'role': 'user', 'content': 'Hello'}]}
        form_data = await apply_character_system(request, form_data, metadata, member, model)
        return apply_character_messages(request, form_data, model)['messages']

    # Default persona: name for {{user}}, description in the system block.
    messages = await run({})
    assert 'Seraphina greets Sir Aldo.' in messages[0]['content']
    assert messages[0]['content'].endswith('A tired knight.')

    # A persona chosen for the message wins; '' means no persona at all.
    assert 'greets Wren.' in (await run({'persona_id': 'p-bard'}))[0]['content']
    assert 'greets Member.' in (await run({'persona_id': ''}))[0]['content']
    # Someone else's or unknown ids fall back to the default.
    assert 'greets Sir Aldo.' in (await run({'persona_id': 'nope'}))[0]['content']

    # Out of character: OOC instruction first, the character only as reference, no post-history.
    messages = await run({'features': {'ooc': True}})
    assert messages[0]['content'].startswith('Sir Aldo is speaking out of character')
    assert 'Roleplay context, for reference only' in messages[0]['content']
    assert messages[-1] == {'role': 'user', 'content': 'Hello'}
