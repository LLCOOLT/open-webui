"""Lorebook endpoints against a real (temporary SQLite) database."""

import base64
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.routers import lorebooks as lorebooks_router
from open_webui.utils.auth import get_current_user

pytestmark = pytest.mark.asyncio(loop_scope='session')

BOOK = {
    'spec': 'lorebook_v3',
    'data': {
        'name': 'Forest',
        'description': 'Places in the forest',
        'scan_depth': 3,
        'entries': [{'keys': ['glade'], 'content': 'The glade is warded.', 'use_regex': False}],
    },
}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def people():
    suffix = uuid.uuid4().hex[:8]
    admin = await Users.insert_new_user(f'lb-admin-{suffix}', 'Admin', f'lb-admin-{suffix}@example.com', role='admin')
    member = await Users.insert_new_user(f'lb-user-{suffix}', 'Member', f'lb-user-{suffix}@example.com', role='user')
    other = await Users.insert_new_user(f'lb-other-{suffix}', 'Other', f'lb-other-{suffix}@example.com', role='user')
    return {'admin': admin, 'user': member, 'other': other}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def app(people):
    app = FastAPI()
    app.include_router(lorebooks_router.router, prefix='/api/v1/lorebooks')
    app.state.current_user = people['admin']
    app.dependency_overrides[get_current_user] = lambda: app.state.current_user
    return app


@pytest_asyncio.fixture(loop_scope='session')
async def client(app, people):
    app.state.current_user = people['admin']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def set_lorebook_permission(enabled: bool):
    permissions = await Config.get('user.permissions')
    permissions = {**permissions, 'workspace': {**permissions.get('workspace', {}), 'lorebooks': enabled}}
    await Config.upsert({'user.permissions': permissions})


async def test_import_is_admin_only(app, client, people):
    app.state.current_user = people['user']
    files = {'file': ('forest.json', json.dumps(BOOK).encode(), 'application/json')}
    assert (await client.post('/api/v1/lorebooks/import', files=files)).status_code == 401

    app.state.current_user = people['admin']
    response = await client.post('/api/v1/lorebooks/import', files=files)
    assert response.status_code == 200, response.text
    imported = response.json()
    assert imported['name'] == 'Forest'
    assert imported['data']['scan_depth'] == 3
    assert imported['data']['entries'][0]['keys'] == ['glade']

    exported = (await client.get(f'/api/v1/lorebooks/{imported["id"]}/export')).json()
    assert exported['spec'] == 'lorebook_v3'
    assert exported['data']['entries'][0]['content'] == 'The glade is warded.'


async def test_import_from_card_and_world_info(client, png_factory):
    card = {
        'spec': 'chara_card_v2',
        'data': {'name': 'Sera', 'character_book': {'entries': [{'keys': ['x'], 'content': 'y'}]}},
    }
    png = png_factory(text_chunks={'chara': base64.b64encode(json.dumps(card).encode()).decode()})
    response = await client.post('/api/v1/lorebooks/import', files={'file': ('sera.png', png, 'image/png')})
    assert response.status_code == 200, response.text
    assert response.json()['name'] == 'Sera lorebook'

    world_info = {'entries': {'0': {'key': ['a'], 'content': 'b'}}}
    response = await client.post(
        '/api/v1/lorebooks/import', files={'file': ('world.json', json.dumps(world_info).encode(), 'application/json')}
    )
    assert response.json()['name'] == 'world'

    empty = {'spec': 'chara_card_v2', 'data': {'name': 'Nobody'}}
    response = await client.post(
        '/api/v1/lorebooks/import', files={'file': ('n.json', json.dumps(empty).encode(), 'application/json')}
    )
    assert response.status_code == 400


async def test_create_needs_the_workspace_permission(app, client, people):
    app.state.current_user = people['user']
    form = {'name': 'Mine', 'data': {'entries': [{'keys': ['k'], 'content': 'v'}]}}

    await set_lorebook_permission(False)
    assert (await client.post('/api/v1/lorebooks/create', json=form)).status_code == 401

    await set_lorebook_permission(True)
    try:
        response = await client.post('/api/v1/lorebooks/create', json=form)
        assert response.status_code == 200, response.text
        created = response.json()
        assert created['user_id'] == people['user'].id
        assert created['data']['entries'][0]['priority'] == 10
    finally:
        await set_lorebook_permission(False)

    # The owner can read, update and delete it; another user cannot even see it.
    response = await client.get(f'/api/v1/lorebooks/{created["id"]}')
    assert response.json()['write_access'] is True
    form['name'] = 'Renamed'
    assert (await client.post(f'/api/v1/lorebooks/{created["id"]}/update', json=form)).json()['name'] == 'Renamed'

    app.state.current_user = people['other']
    assert (await client.get(f'/api/v1/lorebooks/{created["id"]}')).status_code == 404
    assert created['id'] not in [item['id'] for item in (await client.get('/api/v1/lorebooks/')).json()['items']]
    assert (await client.delete(f'/api/v1/lorebooks/{created["id"]}/delete')).status_code == 404

    app.state.current_user = people['user']
    assert (await client.delete(f'/api/v1/lorebooks/{created["id"]}/delete')).json() is True


async def test_shared_lorebooks_are_readable_not_writable(app, client, people):
    form = {
        'name': 'Shared',
        'data': {'entries': [{'keys': ['k'], 'content': 'v'}]},
        'access_grants': [{'principal_type': 'user', 'principal_id': people['other'].id, 'permission': 'read'}],
    }
    shared = (await client.post('/api/v1/lorebooks/create', json=form)).json()

    app.state.current_user = people['other']
    response = await client.get(f'/api/v1/lorebooks/{shared["id"]}')
    assert response.status_code == 200
    assert response.json()['write_access'] is False
    assert shared['id'] in [item['id'] for item in (await client.get('/api/v1/lorebooks/')).json()['items']]
    assert (await client.post(f'/api/v1/lorebooks/{shared["id"]}/update', json=form)).status_code == 404


async def test_preview_endpoint(app, client, people):
    form = {
        'data': {
            'entries': [{'keys': ['glade'], 'content': 'Warded.'}, {'keys': ['x'], 'content': '@@depth 1\nDeep.'}]
        },
        'text': 'The glade and x.',
    }
    result = (await client.post('/api/v1/lorebooks/test', json=form)).json()
    assert result == {
        'before_char': ['Warded.'],
        'after_char': [],
        'depth': [{'text': 'Deep.', 'depth': 1, 'role': 'system'}],
    }

    app.state.current_user = people['other']
    assert (await client.post('/api/v1/lorebooks/test', json=form)).status_code == 401
