"""Reply cleanup, impersonate, scene images, generated expressions and lorebook suggestions."""

import io
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image

from open_webui.models.chats import ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.lorebooks import LorebookForm, Lorebooks
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.routers import chats as chats_router
from open_webui.routers import images as images_router
from open_webui.routers import models as models_router
from open_webui.test.test_roleplay_router import CARD
from open_webui.utils.auth import get_current_user
from open_webui.utils.roleplay.assist import (
    clean_impersonation,
    impersonate_prompt,
    lore_prompt,
    parse_lore_suggestions,
    scene_prompt,
    transcript,
)
from open_webui.utils.roleplay.character import CharacterMeta
from open_webui.utils.roleplay.cleanup import (
    CleanupRule,
    ReplyCleanup,
    add_stop_sequences,
    clean_output,
    clean_reply,
    trim_incomplete_sentence,
)
from open_webui.utils.roleplay.filter import apply_character_system, clean_character_reply
from open_webui.utils.roleplay.prompt import MacroContext

pytestmark = pytest.mark.asyncio(loop_scope='session')

CTX = MacroContext(char='Sera', user='Robin', persona='A wandering bard.')


####################
# Reply cleanup
####################


def test_cut_where_the_model_writes_for_the_user():
    settings = ReplyCleanup()
    text = 'Sera: *She smiles.* "Welcome."\n\nRobin: Thanks!\n\nSera: Anytime.'
    assert clean_reply(text, settings, 'Sera', 'Robin') == '*She smiles.* "Welcome."'
    assert clean_reply('Hi.\n**Robin:** hello', settings, 'Sera', 'Robin') == 'Hi.'
    # Only at the start of a line, and never the whole reply.
    assert clean_reply('She calls out to Robin: "Hey!"', settings, 'Sera', 'Robin') == 'She calls out to Robin: "Hey!"'
    assert clean_reply('Robin: hi', settings, 'Sera', 'Robin') == 'Robin: hi'
    assert clean_reply('x\n{{user}}: hi', settings, 'Sera', 'Robin') == 'x'
    off = ReplyCleanup(stop_at_user=False)
    assert clean_reply('Hi.\nRobin: hi', off, 'Sera', 'Robin') == 'Hi.\nRobin: hi'


def test_trim_incomplete_sentence():
    assert trim_incomplete_sentence('She waves. Then she turns and') == 'She waves.'
    assert trim_incomplete_sentence('"Come here," she says. *She walks') == '"Come here," she says.'
    assert trim_incomplete_sentence('*She nods*') == '*She nods*'
    assert trim_incomplete_sentence('no sentence end at all') == 'no sentence end at all'
    assert trim_incomplete_sentence("It's fine. Don't") == "It's fine."


def test_rules():
    rules = [
        CleanupRule(find='{{char}} smirks', replace='{{char}} smiles'),
        CleanupRule(find=r'/\bvery (\w+)/i', replace='$1!', regex=True),
        CleanupRule(find='(skip me)', replace='', enabled=False),
        CleanupRule(find='[unclosed', replace='x', regex=True),  # invalid: skipped
        CleanupRule(find=r'/(?<name>\w+)@/', replace='$<name> at ', regex=True),
    ]
    settings = ReplyCleanup(stop_at_user=False, rules=rules)
    text = 'Sera smirks. Very good (skip me) [unclosed robin@home'
    assert clean_reply(text, settings, 'Sera', 'Robin') == 'Sera smiles. good! (skip me) [unclosed robin at home'
    # Macros in a regex are matched literally.
    settings = ReplyCleanup(stop_at_user=False, rules=[CleanupRule(find='/{{user}}/', replace='you', regex=True)])
    assert clean_reply('R.b R.b', settings, 'Sera', 'R.b') == 'you you'
    assert clean_reply('Rxb', settings, 'Sera', 'R.b') == 'Rxb'


def test_catastrophic_regex_times_out():
    settings = ReplyCleanup(stop_at_user=False, rules=[CleanupRule(find='/(a+)+$/', replace='', regex=True)])
    text = 'a' * 40 + '!'
    assert clean_reply(text, settings, 'Sera', 'Robin') == text


def test_clean_output_cuts_the_last_text_part():
    output = [
        {'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'Robin: thinking'}]},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'One smirk.\nRobin: no'}]},
        {'type': 'function_call', 'arguments': '{}'},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Two smirk.\nRobin: hi'}]},
    ]
    settings = ReplyCleanup(rules=[CleanupRule(find='smirk', replace='smile')])
    clean_output(output, settings, 'Sera', 'Robin')
    assert output[0]['content'][0]['text'] == 'Robin: thinking'
    assert output[1]['content'][0]['text'] == 'One smile.\nRobin: no'
    assert output[3]['content'][0]['text'] == 'Two smile.'


def test_stop_sequences():
    form = {}
    add_stop_sequences(form, {'info': {'params': {'stop': ['<|end|>', '\\nEND']}}}, 'Robin')
    assert form['stop'] == ['<|end|>', '\nEND', '\nRobin:']
    form = {'stop': ['a', 'b', 'c', 'd']}
    add_stop_sequences(form, {}, 'Robin')
    assert form['stop'] == ['a', 'b', 'c', 'd']
    form = {}
    add_stop_sequences(form, {}, '  ')
    assert form == {}


def test_character_meta_cleanup_defaults():
    assert CharacterMeta(name='Sera').reply_cleanup.stop_at_user is True
    assert CharacterMeta(name='Sera', reply_cleanup=None).reply_cleanup == ReplyCleanup()
    meta = CharacterMeta(name='Sera', reply_cleanup={'rules': [{'find': 'x'}] * 50})
    assert len(meta.reply_cleanup.rules) == 32


async def test_pipeline_adds_stops_and_cleans_replies():
    await Config.upsert({'characters.enable': True})
    user = SimpleNamespace(id='u', name='Robin', settings=None)
    model = {
        'id': 'sera',
        'name': 'Sera',
        'info': {
            'meta': {'character': {'name': 'Sera', 'reply_cleanup': {'trim_incomplete': True}}},
            'params': {},
        },
    }
    request = SimpleNamespace(state=SimpleNamespace())
    form = await apply_character_system(request, {'messages': [{'role': 'user', 'content': 'Hi'}]}, {}, user, model)
    assert form['stop'] == ['\nRobin:']
    output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello. And\nRobin: hi'}]}]
    assert clean_character_reply(request, output) is True
    assert output[0]['content'][0]['text'] == 'Hello.'
    assert clean_character_reply(request, output) is False

    # Out of character: no stop sequences, no cleanup.
    ooc_request = SimpleNamespace(state=SimpleNamespace())
    form = await apply_character_system(
        ooc_request, {'messages': [{'role': 'user', 'content': '(OOC: hi)'}]}, {}, user, model
    )
    assert 'stop' not in form
    assert clean_character_reply(ooc_request, output) is False
    # Plain models: nothing.
    assert clean_character_reply(SimpleNamespace(state=SimpleNamespace()), output) is False


####################
# Prompts
####################

HISTORY = [
    {'role': 'system', 'content': 'ignored'},
    {'role': 'assistant', 'content': '*Sera waves.* "Hello there."'},
    {'role': 'user', 'content': [{'type': 'text', 'text': '*I bow.* "Hi."'}, {'type': 'image_url'}]},
    {
        'role': 'assistant',
        'content': '<details type="reasoning"><summary>Thought</summary>secret</details>"Come in."',
    },
]


def test_transcript():
    text = transcript(HISTORY, 'Sera', 'Robin')
    assert text == 'Sera: *Sera waves.* "Hello there."\n\nRobin: *I bow.* "Hi."\n\nSera: "Come in."'
    # Too long: the newest messages are kept.
    assert transcript(HISTORY, 'Sera', 'Robin', max_chars=30) == 'Sera: "Come in."'


def test_impersonate_prompt_and_cleanup():
    character = CharacterMeta(name='Sera', description='{{char}} is an elf.', scenario='A forest inn.')
    prompt = impersonate_prompt(character, CTX, HISTORY, draft='ask about the map')
    assert "Write Robin's next message" in prompt
    assert 'Sera is an elf.' in prompt and 'A wandering bard.' in prompt and 'A forest inn.' in prompt
    assert '<idea>\nask about the map\n</idea>' in prompt
    assert '{{' not in prompt
    assert 'idea' not in impersonate_prompt(character, CTX, HISTORY)
    assert clean_impersonation('<think>hm</think>Robin: *I nod.* "Sure."\nSera: "Great."', CTX) == '*I nod.* "Sure."'


def test_scene_prompt():
    character = CharacterMeta(name='Sera', description='Silver hair, green cloak.')
    prompt = scene_prompt(character, CTX, HISTORY, style='watercolor')
    assert 'Silver hair, green cloak.' in prompt and 'Art style: watercolor.' in prompt
    assert '"Come in."' in prompt and '{{' not in prompt


def test_lore_suggestions_skip_known_facts():
    existing = [{'keys': ['Old Mill'], 'comment': 'mill', 'content': 'A ruin.'}]
    prompt = lore_prompt('They found the Old Mill and met Bram.', existing, CTX)
    assert '- mill [Old Mill]: A ruin.' in prompt
    reply = """<think>…</think>{"entries": [
        {"name": "Bram", "keys": ["Bram", "the smith"], "content": "Bram is the village smith."},
        {"name": "The mill", "keys": ["old mill"], "content": "Duplicate."},
        {"name": "Sera", "keys": ["elf"], "content": "Sera herself."},
        {"name": "Map", "keys": [], "content": "The map shows the pass."},
        {"name": "Empty", "keys": ["x"], "content": ""},
        {"name": "Bram again", "keys": ["bram"], "content": "Duplicate of the first."}
    ]}"""
    assert parse_lore_suggestions(reply, existing, CTX) == [
        {'name': 'Bram', 'keys': ['Bram', 'the smith'], 'content': 'Bram is the village smith.'},
        {'name': 'Map', 'keys': ['Map'], 'content': 'The map shows the pass.'},
    ]
    assert parse_lore_suggestions('no json', existing, CTX) == []


####################
# Endpoints
####################


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def people():
    suffix = uuid.uuid4().hex[:8]
    admin = await Users.insert_new_user(f'ra-{suffix}', 'Admin', f'ra-{suffix}@example.com', role='admin')
    other = await Users.insert_new_user(f'rb-{suffix}', 'Other', f'rb-{suffix}@example.com', role='user')
    return {'admin': admin, 'other': other}


@pytest_asyncio.fixture(scope='module', loop_scope='session')
async def assist_app(people):
    app = FastAPI()
    app.include_router(models_router.router, prefix='/api/v1/models')
    app.include_router(chats_router.router, prefix='/api/v1/chats')
    app.state.MODELS = {'base-model': {'id': 'base-model', 'name': 'Base', 'owned_by': 'openai'}}
    app.state.redis = None
    app.state.current_user = people['admin']
    app.dependency_overrides[get_current_user] = lambda: app.state.current_user
    return app


@pytest_asyncio.fixture(loop_scope='session')
async def api(assist_app, people):
    await Config.upsert({'characters.enable': True})
    assist_app.state.current_user = people['admin']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=assist_app), base_url='http://test') as client:
        yield client


@pytest_asyncio.fixture(loop_scope='session')
async def character_id(api, assist_app):
    import json

    response = await api.post(
        '/api/v1/models/characters/import',
        files=[('files', ('sera.json', json.dumps(CARD).encode(), 'application/json'))],
        data={'base_model_id': 'base-model'},
    )
    assert response.status_code == 200, response.text
    model_id = response.json()[0]['id']
    assist_app.state.MODELS[model_id] = {'id': model_id, 'name': 'Seraphina'}
    yield model_id
    await Models.delete_model_by_id(model_id)


def image_config(**overrides):
    return SimpleNamespace(**{'ENABLE_IMAGE_GENERATION': True, 'ENABLE_IMAGE_EDIT': False, 'USER_PERMISSIONS': {}, **overrides})


async def test_impersonate_endpoint(api, character_id, assist_app, people, monkeypatch):
    calls = []

    async def fake_task(request, user, model_id, prompt, task, direct=False):
        calls.append({'model': model_id, 'task': task, 'direct': direct, 'prompt': prompt})
        calls[-1]['skip'] = getattr(request.state, 'roleplay_model_id', None)
        return 'Admin: *I nod.* "Lead the way."\nSeraphina: "Follow me."'

    monkeypatch.setattr(chats_router, 'run_task_prompt', fake_task)
    response = await api.post(
        '/api/v1/chats/roleplay/impersonate',
        json={'model_id': character_id, 'messages': HISTORY, 'draft': 'agree'},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {'content': '*I nod.* "Lead the way."'}
    assert calls[0]['direct'] is True and calls[0]['model'] == character_id
    assert calls[0]['skip'] == character_id  # no character prompt on this request
    assert 'Seraphina guards the forest.' in calls[0]['prompt']

    # Only characters the user may use.
    assist_app.state.current_user = people['other']
    response = await api.post('/api/v1/chats/roleplay/impersonate', json={'model_id': character_id})
    assert response.status_code == 404


async def test_scene_image_endpoint(api, character_id, people, monkeypatch):
    async def fake_task(request, user, model_id, prompt, task, direct=False):
        assert task == 'scene_image' and not direct
        return 'Prompt: "an elf in a forest, watercolor"'

    generated = []

    async def fake_generate(request, form_data, metadata=None, user=None):
        generated.append((form_data.prompt, metadata))
        return [{'id': 'f1', 'url': '/api/v1/files/f1/content'}]

    monkeypatch.setattr(chats_router, 'run_task_prompt', fake_task)
    monkeypatch.setattr(images_router, 'image_generations', fake_generate)

    async def config():
        return image_config()

    monkeypatch.setattr(images_router, 'get_image_config', config)
    chat_id = str(uuid.uuid4())
    await Chats.insert_new_chat(chat_id, people['admin'].id, ChatForm(chat={'title': 'RP', 'history': {}}))
    response = await api.post(
        '/api/v1/chats/roleplay/scene-image',
        json={'model_id': character_id, 'messages': HISTORY, 'chat_id': chat_id, 'message_id': 'm1'},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        'prompt': 'an elf in a forest, watercolor',
        'images': [{'id': 'f1', 'url': '/api/v1/files/f1/content'}],
    }
    assert generated == [('an elf in a forest, watercolor', {'chat_id': chat_id, 'message_id': 'm1'})]

    async def disabled():
        return image_config(ENABLE_IMAGE_GENERATION=False)

    monkeypatch.setattr(images_router, 'get_image_config', disabled)
    response = await api.post('/api/v1/chats/roleplay/scene-image', json={'model_id': character_id})
    assert response.status_code == 400


def png(color) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buffer, format='PNG')
    return buffer.getvalue()


async def test_generate_expression_endpoint(api, character_id, assist_app, people, monkeypatch):
    prompts = []

    async def fake_task(request, user, model_id, prompt, task, direct=False):
        prompts.append(prompt)
        return 'an elf, smiling, upper body'

    async def fake_generate(request, form_data, metadata=None, user=None):
        prompts.append(form_data.prompt)
        uploaded = await models_router._upload_character_image(request, png((0, 200, 0)), 'gen.png', user, None)
        return [{'id': uploaded.id}]

    async def config():
        return image_config()

    monkeypatch.setattr(models_router, 'run_task_prompt', fake_task)
    monkeypatch.setattr(images_router, 'image_generations', fake_generate)
    monkeypatch.setattr(images_router, 'get_image_config', config)

    response = await api.post(
        f'/api/v1/models/model/character/expressions/generate?id={character_id}', json={'name': 'Joy', 'style': 'anime'}
    )
    assert response.status_code == 200, response.text
    assert response.json()['expressions'] == ['joy']
    assert 'clear joy expression' in prompts[0] and 'Art style: anime.' in prompts[0]
    assert prompts[1] == 'an elf, smiling, upper body'
    response = await api.get(f'/api/v1/models/model/character/expression?id={character_id}&name=joy')
    assert response.status_code == 200 and response.headers['content-type'] == 'image/webp'

    # With image editing on, the avatar is redrawn instead.
    edits = []

    async def fake_edit(request, form_data, metadata=None, user=None):
        edits.append(form_data)
        uploaded = await models_router._upload_character_image(request, png((200, 0, 0)), 'edit.png', user, None)
        return [{'id': uploaded.id}]

    async def edit_config():
        return image_config(ENABLE_IMAGE_EDIT=True)

    model = await Models.get_model_by_id(character_id)
    model.meta.profile_image_url = 'data:image/png;base64,' + __import__('base64').b64encode(png((1, 1, 1))).decode()
    await models_router._save_character_meta(model, None)
    monkeypatch.setattr(images_router, 'image_edits', fake_edit)
    monkeypatch.setattr(images_router, 'get_image_config', edit_config)
    response = await api.post(
        f'/api/v1/models/model/character/expressions/generate?id={character_id}', json={'name': 'joy'}
    )
    assert response.status_code == 200, response.text
    assert edits[0].image.startswith('data:image/png;base64,') and 'clear joy expression' in edits[0].prompt

    # Read access is not enough.
    assist_app.state.current_user = people['other']
    response = await api.post(
        f'/api/v1/models/model/character/expressions/generate?id={character_id}', json={'name': 'anger'}
    )
    assert response.status_code == 404


async def test_lore_suggestion_endpoint(api, character_id, people, monkeypatch):
    lorebook = await Lorebooks.insert_new_lorebook(
        people['admin'].id,
        LorebookForm(name='World', data={'entries': [{'keys': ['Old Mill'], 'content': 'A ruin.'}]}),
    )
    chat_id = str(uuid.uuid4())
    history = {
        'messages': {
            'a': {'id': 'a', 'parentId': None, 'role': 'user', 'content': 'We reach the mill.'},
            'b': {'id': 'b', 'parentId': 'a', 'role': 'assistant', 'content': 'Bram waits.', 'model': character_id},
        },
        'currentId': 'b',
    }
    await Chats.insert_new_chat(chat_id, people['admin'].id, ChatForm(chat={'title': 'RP', 'history': history}))
    await Chats.update_chat_meta_by_id_and_user_id(chat_id, people['admin'].id, {'lorebook_ids': [lorebook.id]})
    prompts = []

    async def fake_task(request, user, model_id, prompt, task, direct=False):
        prompts.append(prompt)
        return '{"entries": [{"name": "Bram", "keys": ["Bram"], "content": "Bram waits at the mill."},' \
            '{"name": "Mill", "keys": ["old mill"], "content": "dup"}]}'

    monkeypatch.setattr(chats_router, 'run_task_prompt', fake_task)
    response = await api.post(f'/api/v1/chats/{chat_id}/roleplay/lore/suggest')
    assert response.status_code == 200, response.text
    assert response.json() == {
        'suggestions': [{'name': 'Bram', 'keys': ['Bram'], 'content': 'Bram waits at the mill.'}],
        'source': 'chat',
    }
    assert 'Seraphina: Bram waits.' in prompts[0] and '[Old Mill]: A ruin.' in prompts[0]

    # With a story memory, the memory is what is read.
    await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, 'a', {'contextSummary': 'They met Bram.'})
    response = await api.post(f'/api/v1/chats/{chat_id}/roleplay/lore/suggest')
    assert response.json()['source'] == 'memory'
    assert '<story_memory>\nThey met Bram.\n</story_memory>' in prompts[1]
    await Lorebooks.delete_lorebook_by_id(lorebook.id)
