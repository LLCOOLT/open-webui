"""Emotion-aware speech: expression names mapped to voice-engine hints, and the speech endpoint."""

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_webui.models.config import Config
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.routers import audio as audio_router
from open_webui.routers import models as models_router
from open_webui.test.test_roleplay_router import CARD
from open_webui.utils.auth import get_current_user
from open_webui.utils.roleplay.emotion import (
    DEFAULT_EMOTIONS,
    azure_style,
    canonical_emotion,
    elevenlabs_settings,
    elevenlabs_tag,
    speech_instruction,
)

pytestmark = pytest.mark.asyncio(loop_scope='session')


####################
# Mapping
####################


def test_canonical_emotions_from_expression_names():
    assert canonical_emotion('joy') == 'joy'
    assert canonical_emotion('Joy_2') == 'joy'
    assert canonical_emotion('smirk') == 'amusement'
    assert canonical_emotion('crying') == 'sadness'
    assert canonical_emotion('very-angry') == 'anger'
    assert canonical_emotion('blushing hard') == 'embarrassment'
    assert canonical_emotion('neutral') == 'neutral'
    assert canonical_emotion('xyzzy') is None
    assert canonical_emotion('') is None and canonical_emotion(None) is None
    assert all(canonical_emotion(name) == name for name in DEFAULT_EMOTIONS)


def test_engine_hints():
    assert speech_instruction('joy') == 'The speaker is happy and warm, with a lively pace and a smile in the voice.'
    assert speech_instruction('neutral') == '' and speech_instruction('') == ''
    assert speech_instruction('brooding') == 'The speaker feels "brooding"; let that show in the voice.'
    assert azure_style('crying') == 'sad' and azure_style('neutral') is None and azure_style('xyzzy') is None
    assert elevenlabs_settings('anger') == {'stability': 0.3, 'similarity_boost': 0.5, 'style': 0.6}
    assert elevenlabs_settings('xyzzy') == {'stability': 0.5, 'similarity_boost': 0.5}
    assert elevenlabs_tag('fear') == 'nervous' and elevenlabs_tag('neutral') is None


def test_openai_models_that_take_instructions():
    assert audio_router._openai_takes_instructions('gpt-4o-mini-tts')
    assert audio_router._openai_takes_instructions('kokoro')
    assert not audio_router._openai_takes_instructions('tts-1')
    assert not audio_router._openai_takes_instructions('TTS-1-HD')


####################
# Speech endpoint
####################


class FakeResponse:
    def __init__(self):
        self.headers = {'Content-Type': 'audio/mpeg'}

    def raise_for_status(self):
        pass

    async def read(self):
        return b'ID3fake-audio'


class FakePost:
    """``await session.post(...)`` and ``async with session.post(...)`` both work."""

    def __init__(self, record):
        self.record = record

    def __await__(self):
        async def result():
            return FakeResponse()

        return result().__await__()

    async def __aenter__(self):
        return FakeResponse()

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url=None, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return FakePost(self.calls[-1])


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def admin():
    suffix = uuid.uuid4().hex[:8]
    return await Users.insert_new_user(f'sp-{suffix}', 'Admin', f'sp-{suffix}@example.com', role='admin')


@pytest_asyncio.fixture(loop_scope='session')
async def speech_api(admin, monkeypatch):
    app = FastAPI()
    app.include_router(audio_router.router, prefix='/api/v1/audio')
    app.state.MODELS = {}
    app.state.redis = None
    app.dependency_overrides[get_current_user] = lambda: admin
    session = FakeSession()

    async def fake_get_session():
        return session

    monkeypatch.setattr(audio_router, 'get_session', fake_get_session)
    monkeypatch.setattr(audio_router, 'transcode_audio_to_mp3', lambda *args: False)
    await Config.upsert(
        {
            'audio.tts.engine': 'openai',
            'audio.tts.model': 'gpt-4o-mini-tts',
            'audio.tts.voice': 'alloy',
            'audio.tts.expressive': True,
            'audio.tts.openai.api_base_url': 'https://tts.example',
            'audio.tts.openai.api_key': 'k',
            'audio.tts.openai.params': {},
            'audio.tts.api_key': 'k',
        }
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client, session
    await Config.upsert({'audio.tts.engine': '', 'audio.tts.model': 'tts-1', 'audio.tts.expressive': True})


def unique(text: str) -> str:
    return f'{text} {uuid.uuid4().hex[:8]}'


async def test_openai_speech_gets_an_instruction(speech_api):
    client, session = speech_api
    response = await client.post(
        '/api/v1/audio/speech', json={'input': unique('Hello'), 'voice': 'alloy', 'emotion': 'Joy!'}
    )
    assert response.status_code == 200, response.text
    sent = session.calls[-1]['json']
    assert sent['instructions'] == 'The speaker is happy and warm, with a lively pace and a smile in the voice.'
    assert 'emotion' not in sent and sent['model'] == 'gpt-4o-mini-tts'

    # Admin parameters that set instructions keep them, with the emotion after.
    await Config.upsert({'audio.tts.openai.params': {'instructions': 'Read slowly.'}})
    await client.post('/api/v1/audio/speech', json={'input': unique('Hello'), 'voice': 'alloy', 'emotion': 'sad'})
    assert (
        session.calls[-1]['json']['instructions'] == 'Read slowly.\nThe speaker is sad and subdued, slower and softer.'
    )
    await Config.upsert({'audio.tts.openai.params': {}})

    # Neutral and unset: no instruction at all.
    for emotion in ('neutral', None):
        await client.post('/api/v1/audio/speech', json={'input': unique('Hello'), 'voice': 'alloy', 'emotion': emotion})
        assert 'instructions' not in session.calls[-1]['json'] and 'emotion' not in session.calls[-1]['json']

    # The tts-1 models reject instructions, so they get none.
    await Config.upsert({'audio.tts.model': 'tts-1'})
    await client.post('/api/v1/audio/speech', json={'input': unique('Hello'), 'voice': 'alloy', 'emotion': 'joy'})
    assert 'instructions' not in session.calls[-1]['json']
    await Config.upsert({'audio.tts.model': 'gpt-4o-mini-tts'})

    # Expressive speech turned off by the admin.
    await Config.upsert({'audio.tts.expressive': False})
    await client.post('/api/v1/audio/speech', json={'input': unique('Hello'), 'voice': 'alloy', 'emotion': 'joy'})
    assert 'instructions' not in session.calls[-1]['json']
    await Config.upsert({'audio.tts.expressive': True})


async def test_elevenlabs_and_azure_speech_styles(speech_api, monkeypatch):
    client, session = speech_api

    async def no_voices(request):
        return {}

    monkeypatch.setattr(audio_router, 'get_available_voices', no_voices)
    await Config.upsert({'audio.tts.engine': 'elevenlabs', 'audio.tts.model': 'eleven_multilingual_v2'})
    await client.post('/api/v1/audio/speech', json={'input': unique('Go away'), 'voice': 'v1', 'emotion': 'angry'})
    sent = session.calls[-1]['json']
    assert sent['voice_settings'] == {'stability': 0.3, 'similarity_boost': 0.5, 'style': 0.6}
    assert sent['text'].startswith('Go away')  # audio tags are for the v3 model only
    await Config.upsert({'audio.tts.model': 'eleven_v3'})
    await client.post('/api/v1/audio/speech', json={'input': unique('Go away'), 'voice': 'v1', 'emotion': 'angry'})
    assert session.calls[-1]['json']['text'].startswith('[angry] Go away')
    await client.post('/api/v1/audio/speech', json={'input': unique('Fine'), 'voice': 'v1'})
    assert session.calls[-1]['json']['voice_settings'] == {'stability': 0.5, 'similarity_boost': 0.5}

    await Config.upsert(
        {
            'audio.tts.engine': 'azure',
            'audio.tts.voice': 'en-US-JennyNeural',
            'audio.tts.azure.speech_region': 'eastus',
            'audio.tts.azure.speech_base_url': '',
            'audio.tts.azure.speech_output_format': 'audio-24khz-160kbitrate-mono-mp3',
        }
    )
    await client.post(
        '/api/v1/audio/speech',
        json={'input': unique('I miss <you>'), 'voice': 'en-US-JennyNeural', 'emotion': 'crying'},
    )
    ssml = session.calls[-1]['data']
    assert 'xmlns:mstts="https://www.w3.org/2001/mstts"' in ssml
    assert '<mstts:express-as style="sad">I miss &lt;you&gt;' in ssml and ssml.count('</mstts:express-as>') == 1
    await client.post('/api/v1/audio/speech', json={'input': unique('Fine'), 'voice': 'en-US-JennyNeural'})
    assert 'express-as' not in session.calls[-1]['data']
    await Config.upsert(
        {'audio.tts.engine': 'openai', 'audio.tts.model': 'gpt-4o-mini-tts', 'audio.tts.voice': 'alloy'}
    )


####################
# Classification for characters without expression images
####################


@pytest_asyncio.fixture(loop_scope='session')
async def classify_api(admin):
    app = FastAPI()
    app.include_router(models_router.router, prefix='/api/v1/models')
    app.state.MODELS = {'base-model': {'id': 'base-model', 'name': 'Base', 'owned_by': 'openai'}}
    app.state.redis = None
    app.dependency_overrides[get_current_user] = lambda: admin
    await Config.upsert({'characters.enable': True, 'audio.tts.expressive': True})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/api/v1/models/characters/import',
            files=[('files', ('sera.json', json.dumps(CARD).encode(), 'application/json'))],
            data={'base_model_id': 'base-model'},
        )
        assert response.status_code == 200, response.text
        model_id = response.json()[0]['id']
        app.state.MODELS[model_id] = {'id': model_id, 'name': 'Seraphina'}
        yield client, model_id
        await Models.delete_model_by_id(model_id)


async def test_classify_for_speech_uses_default_emotions(classify_api, monkeypatch):
    client, model_id = classify_api
    prompts = []

    async def fake_task(request, user, model_id, prompt, task, direct=False):
        prompts.append(prompt)
        return 'surprise'

    monkeypatch.setattr(models_router, 'run_task_prompt', fake_task)
    body = {'model_id': model_id, 'text': '*She gasps.* "You came back?"'}
    # Without expression images, a plain classification has nothing to choose from.
    assert (await client.post('/api/v1/models/model/character/expression/classify', json=body)).json() == {'name': None}
    assert not prompts
    # For speech, the default emotions stand in.
    response = await client.post(
        '/api/v1/models/model/character/expression/classify', json={**body, 'for_speech': True}
    )
    assert response.json() == {'name': 'surprise'}
    assert ', '.join(DEFAULT_EMOTIONS) in prompts[0]
    # Not when the admin turned expressive speech off.
    await Config.upsert({'audio.tts.expressive': False})
    assert (
        await client.post('/api/v1/models/model/character/expression/classify', json={**body, 'for_speech': True})
    ).json() == {'name': None}
    await Config.upsert({'audio.tts.expressive': True})
