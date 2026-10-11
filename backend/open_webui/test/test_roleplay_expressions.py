"""Character expressions: names, picking one for a reply, card import and the expression endpoints."""

import io
import json
import zipfile

import pytest
from PIL import Image

from open_webui.models.models import Models
from open_webui.routers import models as models_router
from open_webui.test.test_roleplay_router import CARD, app, client, users  # noqa: F401 (fixtures)
from open_webui.utils.roleplay.cards import parse_card_bytes
from open_webui.utils.roleplay.character import CharacterMeta, character_summary
from open_webui.utils.roleplay.expressions import (
    classify_prompt,
    clean_expression_name,
    expression_name_from_filename,
    pick_expression,
)
from open_webui.utils.roleplay.images import expression_image

pytestmark = pytest.mark.asyncio(loop_scope='session')


def image_bytes(color=(255, 0, 0), size=(40, 60), fmt='PNG') -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def close(pixel, expected, tolerance=8) -> bool:
    """WebP is lossy; colors come back within a few units."""
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], expected))


def test_names():
    assert clean_expression_name('  Joy ') == 'joy'
    assert clean_expression_name('Very  Happy!!') == 'very happy'
    assert clean_expression_name('웃음') == '웃음'
    assert clean_expression_name('x' * 80) == 'x' * 40
    assert clean_expression_name('???') == ''
    assert expression_name_from_filename('sprites/Seraphina/Joy.PNG') == 'joy'
    assert expression_name_from_filename('C:\\pack\\anger_2.webp') == 'anger_2'


def test_pick_expression():
    names = ['joy', 'sadness', 'very happy', 'neutral']
    assert pick_expression('joy', names) == 'joy'
    assert pick_expression('Sadness.', names) == 'sadness'
    assert pick_expression('<think>maybe joy?</think>neutral', names) == 'neutral'
    assert pick_expression('She looks very happy here', names) == 'very happy'
    assert pick_expression('enjoyment', names) is None  # whole names only
    assert pick_expression('', names) is None


def test_classify_prompt_keeps_the_end_of_long_replies():
    prompt = classify_prompt('Sera', ['joy', 'anger'], 'start ' + 'x' * 5000 + ' she laughs')
    assert 'Choose exactly one of: joy, anger' in prompt
    assert 'she laughs' in prompt and 'start' not in prompt
    assert 'how Sera looks' in prompt


def test_expression_image_is_fitted_webp():
    data, content_type = expression_image(image_bytes(size=(3000, 1500)))
    assert content_type == 'image/webp'
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1024, 512)
    with pytest.raises(ValueError):
        expression_image(b'not an image')


def test_meta_cleans_expressions_and_summary_lists_names():
    meta = CharacterMeta(name='Sera', expressions={'Joy': 'f1', '!!': 'f2', 'anger': 3, 'calm': 'f4'})
    assert meta.expressions == {'joy': 'f1', 'calm': 'f4'}
    assert character_summary(meta.model_dump())['expressions'] == ['calm', 'joy']
    assert CharacterMeta(name='Sera', expressions=None).expressions == {}


def charx_with_emotions() -> bytes:
    card = {
        'spec': 'chara_card_v3',
        'spec_version': '3.0',
        'data': {
            **CARD['data'],
            'assets': [
                {'type': 'icon', 'uri': 'ccdefault:', 'name': 'main', 'ext': 'png'},
                {'type': 'emotion', 'uri': 'embeded://assets/emotion/joy.png', 'name': 'Joy', 'ext': 'png'},
                {'type': 'emotion', 'uri': 'embeded://assets/emotion/sad.png', 'name': 'sadness', 'ext': 'png'},
                {'type': 'emotion', 'uri': 'embeded://assets/missing.png', 'name': 'gone', 'ext': 'png'},
            ],
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('card.json', json.dumps(card))
        archive.writestr('assets/emotion/joy.png', image_bytes((0, 255, 0)))
        archive.writestr('assets/emotion/sad.png', image_bytes((0, 0, 255)))
    return buffer.getvalue()


def test_card_emotion_assets_are_parsed():
    parsed = parse_card_bytes(charx_with_emotions(), 'sera.charx')
    assert [(name, content_type) for name, _, content_type in parsed.expressions] == [
        ('Joy', 'image/png'),
        ('sadness', 'image/png'),
    ]


async def test_expression_endpoints(app, client, users, monkeypatch):  # noqa: F811
    response = await client.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.charx', charx_with_emotions(), 'application/zip'))],
        data={'base_model_id': 'base-model'},
    )
    assert response.status_code == 200, response.text
    model_id = response.json()[0]['id']
    model = await Models.get_model_by_id(model_id)
    assert sorted(model.meta.character.expressions) == ['joy', 'sadness']

    # Served per model, as normalized WebP.
    response = await client.get(f'/api/v1/models/model/character/expression?id={model_id}&name=Joy')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/webp'
    with Image.open(io.BytesIO(response.content)) as image:
        assert close(image.getpixel((5, 5)), (0, 255, 0))
    assert (await client.get(f'/api/v1/models/model/character/expression?id={model_id}&name=nope')).status_code == 404

    # Upload single images and a sprite pack; same names replace, junk is skipped.
    pack = io.BytesIO()
    with zipfile.ZipFile(pack, 'w') as archive:
        archive.writestr('pack/anger.png', image_bytes((10, 10, 10)))
        archive.writestr('pack/readme.txt', 'hello')
        archive.writestr('__MACOSX/pack/._anger.png', 'junk')
    response = await client.post(
        f'/api/v1/models/model/character/expressions?id={model_id}',
        files=[
            ('files', ('Joy.webp', image_bytes((1, 2, 3), fmt='WEBP'), 'image/webp')),
            ('files', ('sprites.zip', pack.getvalue(), 'application/zip')),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json() == {'expressions': ['anger', 'joy', 'sadness'], 'skipped': ['pack/readme.txt']}
    response = await client.get(f'/api/v1/models/model/character/expression?id={model_id}&name=joy')
    with Image.open(io.BytesIO(response.content)) as image:
        assert close(image.getpixel((5, 5)), (1, 2, 3))

    response = await client.delete(f'/api/v1/models/model/character/expression?id={model_id}&name=sadness')
    assert response.json()['expressions'] == ['anger', 'joy']

    # File references are server-managed: a save from the editor cannot point them at other files.
    model = await Models.get_model_by_id(model_id)
    stored = dict(model.meta.character.expressions)
    data = model.model_dump()
    data['meta']['character'] = {
        **data['meta']['character'],
        'expressions': {'joy': 'someone-elses-file', 'stolen': 'another-file'},
        'avatar_file_id': 'someone-elses-avatar',
        'description': 'Edited.',
    }
    response = await client.post(f'/api/v1/models/model/update?id={model_id}', json=data)
    assert response.status_code == 200, response.text
    model = await Models.get_model_by_id(model_id)
    assert model.meta.character.description == 'Edited.'
    assert model.meta.character.expressions == stored
    assert model.meta.character.avatar_file_id != 'someone-elses-avatar'

    # Classification asks the task model to choose among the character's names.
    prompts = []

    async def fake_task(request, user, model_id_, prompt, task, direct=False):
        prompts.append((model_id_, task, prompt, getattr(request.state, 'roleplay_model_id', None)))
        return 'Anger'

    monkeypatch.setattr(models_router, 'run_task_prompt', fake_task)
    response = await client.post(
        '/api/v1/models/model/character/expression/classify',
        json={'model_id': model_id, 'text': '*She slams the table.* "Enough!"'},
    )
    assert response.json() == {'name': 'anger'}
    assert prompts[0][0] == model_id and prompts[0][1] == 'character_expression'
    assert 'Choose exactly one of: anger, joy' in prompts[0][2]
    # The character's own system prompt is kept out of the task request.
    assert prompts[0][3] == model_id

    # Other users cannot change a private character's expressions or see them.
    app.state.current_user = users['user']
    assert (await client.delete(f'/api/v1/models/model/character/expression?id={model_id}&name=joy')).status_code == 404
    assert (await client.get(f'/api/v1/models/model/character/expression?id={model_id}&name=joy')).status_code == 404
    app.state.current_user = users['admin']
    await Models.delete_model_by_id(model_id)


async def test_card_preview_endpoint(client, monkeypatch):  # noqa: F811
    async def fake_download(source_id, card_id):
        assert (source_id, card_id) == ('risu', 'abc')
        return charx_with_emotions(), 'card.charx'

    monkeypatch.setattr(models_router, 'download_card', fake_download)
    response = await client.get('/api/v1/models/characters/sources/risu/card?id=abc')
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['name'] == 'Seraphina'
    assert preview['first_mes'] == 'Welcome, {{user}}.'
    assert preview['alternate_greetings'] == ['Back again, {{user}}?']
    assert preview['expressions'] == ['Joy', 'sadness']
    assert preview['lorebook_entries'] == 0
