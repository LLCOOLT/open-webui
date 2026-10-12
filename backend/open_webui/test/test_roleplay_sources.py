import base64
import json
from collections import OrderedDict
from urllib.parse import parse_qs, urlparse

import pytest

from open_webui.utils.roleplay import sources
from open_webui.utils.roleplay.cards import PNG_SIGNATURE, parse_card_bytes, write_png_text_chunks
from open_webui.utils.roleplay.sources import (
    SourceError,
    _allowed,
    download_card,
    parse_card_link,
    search_cards,
    unflatten_devalue,
)

pytestmark = pytest.mark.asyncio(loop_scope='session')

# A 1x1 PNG.
TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
)


def flatten(value):
    """A minimal devalue encoder, enough to build the sites' ``__data.json`` payloads."""
    data: list = []

    def add(item):
        index = len(data)
        data.append(None)
        if isinstance(item, dict):
            data[index] = {key: add(child) for key, child in item.items()}
        elif isinstance(item, list):
            data[index] = [add(child) for child in item]
        else:
            data[index] = item
        return index

    add(value)
    return data


def sveltekit(*roots):
    nodes = [{'type': 'data', 'data': flatten(root)} for root in roots]
    return (json.dumps({'type': 'data', 'nodes': [None, *nodes]}) + '\n{"type":"chunk","id":1}').encode()


@pytest.fixture
def fake_get(monkeypatch):
    calls = []
    responses = {}
    monkeypatch.setattr(sources, '_chub_search_variant', None)
    monkeypatch.setattr(sources, '_download_cache', OrderedDict())

    async def get(url, hosts, max_bytes, **kwargs):
        calls.append(url)
        if not _allowed(url, hosts):
            raise SourceError('blocked')
        for prefix, body in responses.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body, ''
        raise SourceError('The site answered 404.')

    monkeypatch.setattr(sources, '_get', get)
    return calls, responses


def test_unflatten_devalue_handles_references_dates_and_promises():
    data = [{'a': 1, 'b': 2, 'c': 4, 'd': 5}, 'x', [3, 1], 7, ['Date', '2024-01-01'], ['Promise', 1]]
    assert unflatten_devalue(data) == {'a': 'x', 'b': [7, 'x'], 'c': '2024-01-01', 'd': 'x'}
    assert unflatten_devalue([{'a': -1}]) == {'a': None}


def test_parse_card_link():
    assert parse_card_link('https://chub.ai/characters/someone/a-knight-1234') == ('chub', 'someone/a-knight-1234')
    assert parse_card_link('https://www.characterhub.org/characters/x/y?tab=1') == ('chub', 'x/y')
    assert parse_card_link('https://realm.risuai.net/character/fad65b7c-d924-4086-80ce-352a91779e1f') == (
        'risu',
        'fad65b7c-d924-4086-80ce-352a91779e1f',
    )
    assert parse_card_link('https://character-tavern.com/character/author/name_1') == ('tavern', 'author/name_1')
    for bad in ('https://example.com/characters/a/b', 'https://chub.ai/users/a', 'not a url', ''):
        assert parse_card_link(bad) is None


def test_allowed_hosts():
    assert _allowed('https://avatars.charhub.io/x.png', sources.CHUB_HOSTS)
    assert not _allowed('http://api.chub.ai/x', sources.CHUB_HOSTS)
    assert not _allowed('https://evilchub.ai/x', sources.CHUB_HOSTS)
    assert not _allowed('https://chub.ai.evil.com/x', sources.CHUB_HOSTS)


async def test_risu_search_maps_cards_and_hides_nsfw(fake_get):
    calls, responses = fake_get
    cards = [
        {
            'name': 'Yrel',
            'desc': 'A paladin.',
            'id': 'a988c75b-070d-4519-8052-1d64286c8160',
            'img': 'abc123',
            'tags': ['knight', 'fantasy'],
            'authorname': 'kim',
            'haslore': True,
            'download': '10.6k',
            'type': 'normal',
        },
        {
            'name': 'Lewd',
            'desc': '',
            'id': 'b988c75b-070d-4519-8052-1d64286c8160',
            'img': 'x',
            'tags': ['nsfw'],
            'type': 'normal',
        },
        {'name': 'A preset', 'id': 'c988c75b-070d-4519-8052-1d64286c8160', 'type': 'preset'},
    ]
    responses['https://realm.risuai.net/__data.json'] = sveltekit({'cards': cards, 'page': 1})
    page = await search_cards('risu', 'knight', 1, 'popular', nsfw=False)
    assert calls[-1] == 'https://realm.risuai.net/__data.json?q=knight&page=1&sort=downloads'
    assert [item.name for item in page.items] == ['Yrel']
    yrel = page.items[0]
    assert yrel.image_url == 'https://sv.risuai.xyz/resource/abc123'
    assert yrel.page_url == 'https://realm.risuai.net/character/a988c75b-070d-4519-8052-1d64286c8160'
    assert (yrel.creator, yrel.has_lorebook, yrel.downloads, yrel.tags) == ('kim', True, '10.6k', ['knight', 'fantasy'])

    page = await search_cards('risu', '', 2, 'newest', nsfw=True)
    assert calls[-1] == 'https://realm.risuai.net/__data.json?page=2&nsfw=true'
    assert [item.name for item in page.items] == ['Yrel', 'Lewd']
    assert page.items[1].nsfw is True


async def test_tavern_search_reads_page_data(fake_get):
    calls, responses = fake_get
    hits = [
        {
            'id': 'CT_1',
            'name': 'Remus',
            'tagline': 'A kenku knight',
            'path': 'canine/remus',
            'author': 'canine',
            'contentWarnings': [],
            'permanentTokens': 1292,
        },
        {'id': 'CT_2', 'name': 'Edgy', 'tagline': '', 'path': 'a/edgy', 'author': 'a', 'contentWarnings': ['gore']},
        {'id': 'CT_3', 'name': 'Bad path', 'path': '../../etc'},
    ]
    responses['https://character-tavern.com/search/cards/__data.json'] = sveltekit(
        {'user': None}, {'searchResults': {'hits': hits, 'totalPages': 3}, 'page': 1}
    )
    page = await search_cards('tavern', 'knight', 1, 'newest')
    assert calls[-1] == 'https://character-tavern.com/search/cards/__data.json?query=knight&sort=newest'
    assert [item.name for item in page.items] == ['Remus']
    assert page.items[0].tokens == 1292
    assert page.has_more is True
    page = await search_cards('tavern', '', 3, 'popular', nsfw=True)
    assert [item.name for item in page.items] == ['Remus', 'Edgy']
    assert page.has_more is False


async def test_chub_search_and_download_rebuild_the_card(fake_get):
    calls, responses = fake_get
    # The shape gateway.chub.ai returns: {"data": {"count": .., "nodes": [..]}}.
    responses['https://gateway.chub.ai/search'] = json.dumps(
        {
            'data': {
                'count': 2,
                'nodes': [
                    {
                        'fullPath': 'anon/sera',
                        'name': 'Seraphina',
                        'tagline': 'Guardian',
                        'topics': ['Fantasy'],
                        'nTokens': 900,
                    },
                    {'fullPath': 'anon/spicy', 'name': 'Spicy', 'topics': ['NSFW']},
                ],
            },
        }
    ).encode()
    page = await search_cards('chub', 'sera', 1)
    assert len(calls) == 1 and calls[0].startswith('https://gateway.chub.ai/search?first=24&page=1&sort=download_count')
    assert 'search=sera' in calls[0] and 'nsfw=false' in calls[0] and 'namespace=characters' in calls[0]
    assert [item.name for item in page.items] == ['Seraphina']
    assert page.items[0].image_url == 'https://avatars.charhub.io/avatars/anon/sera/avatar.webp'

    old_card = base64.b64encode(json.dumps({'name': 'Old', 'description': 'stale'}).encode()).decode()
    responses['https://api.chub.ai/api/characters/anon/sera'] = json.dumps(
        {
            'node': {
                'name': 'Seraphina',
                'topics': ['Fantasy'],
                'max_res_url': 'https://avatars.charhub.io/avatars/anon/sera/chara_card_v2.png',
                'definition': {
                    'name': 'Seraphina',
                    'personality': 'A guardian of the forest.',
                    'tavern_personality': 'Kind.',
                    'first_message': '*She smiles.*',
                    'example_dialogs': '<START>',
                    'alternate_greetings': ['Hello again.'],
                    'embedded_lorebook': {'entries': [{'keys': ['glade'], 'content': 'A quiet glade.'}]},
                },
            }
        }
    ).encode()
    responses['https://avatars.charhub.io/avatars/anon/sera/chara_card_v2.png'] = write_png_text_chunks(
        TINY_PNG, {'chara': old_card}
    )
    data, filename = await download_card('chub', 'anon/sera')
    assert filename == 'card.png' and data.startswith(PNG_SIGNATURE)
    character = parse_card_bytes(data, filename).character
    assert character.name == 'Seraphina'
    assert character.description == 'A guardian of the forest.'
    assert character.personality == 'Kind.'
    assert character.alternate_greetings == ['Hello again.']
    assert character.character_book['entries'][0]['content'] == 'A quiet glade.'

    # Without a usable avatar the card comes back as JSON.
    sources._download_cache.clear()
    responses['https://avatars.charhub.io/avatars/anon/sera/chara_card_v2.png'] = SourceError('gone')
    data, filename = await download_card('chub', 'anon/sera')
    assert filename == 'card.json'
    assert parse_card_bytes(data, filename).character.name == 'Seraphina'


async def test_risu_download_falls_back_to_allowed_formats(fake_get):
    calls, responses = fake_get
    card_id = 'fad65b7c-d924-4086-80ce-352a91779e1f'
    responses[f'https://realm.risuai.net/api/v1/download/png-v3/{card_id}'] = SourceError('The site answered 403.')
    responses[f'https://realm.risuai.net/api/v1/download/json-v3/{card_id}'] = json.dumps(
        {'spec': 'chara_card_v3', 'spec_version': '3.0', 'data': {'name': 'Endfield'}}
    ).encode()
    data, filename = await download_card('risu', card_id)
    assert filename == 'card.json'
    assert [call.split('/download/')[1].split('/')[0] for call in calls] == ['png-v3', 'json-v3']


async def test_invalid_ids_and_sources_are_rejected(fake_get):
    calls, _ = fake_get
    for source, card_id in (('risu', '../admin'), ('tavern', 'a/b/c'), ('chub', 'x'), ('chub', 'a/b?c=d')):
        with pytest.raises(SourceError, match='valid card id'):
            await download_card(source, card_id)
    with pytest.raises(SourceError, match='Unknown card source'):
        await search_cards('nowhere')
    assert calls == []


async def test_chub_search_reports_why_every_endpoint_failed(fake_get):
    calls, responses = fake_get
    rejected = SourceError('The site answered 422: sort: Input should be valid')
    for prefix in ('https://gateway.chub.ai/search', 'https://api.chub.ai/search'):
        responses[prefix] = rejected
    with pytest.raises(SourceError, match='Chub search failed: The site answered 422: sort: Input should be valid'):
        await search_cards('chub', 'x')
    assert len(calls) == 4

    # A regional or bot block stops at once: other endpoints are behind the same wall.
    calls.clear()
    responses['https://gateway.chub.ai/search'] = SourceError('The site is not available from this server’s location.')
    with pytest.raises(SourceError, match='location'):
        await search_cards('chub', 'x')
    assert len(calls) == 1


def test_error_detail():
    detail = json.dumps({'detail': [{'loc': ['query', 'sort'], 'msg': 'Input should be one of x, y', 'type': 'enum'}]})
    assert sources._error_detail(detail) == 'sort: Input should be one of x, y'
    assert sources._error_detail('{"message": "Bad page"}') == 'Bad page'
    assert sources._error_detail('Rate limited, slow down') == 'Rate limited, slow down'
    assert sources._error_detail('<!DOCTYPE html><html>...</html>') == ''


async def test_chub_search_falls_back_and_remembers(fake_get):
    calls, responses = fake_get
    # The gateway rejects the full parameter set; the basic one works and is used from then on.
    full = 'https://gateway.chub.ai/search?first=24&page=1&sort='
    basic = 'https://gateway.chub.ai/search?first=24&page=1&nsfw='
    responses[full] = SourceError('The site answered 422: sort: Input should be valid')
    responses[basic] = json.dumps({'data': {'nodes': [{'fullPath': 'a/b', 'name': 'B'}]}}).encode()
    page = await search_cards('chub', '', 1)
    assert [item.name for item in page.items] == ['B']
    assert [call.split('&')[2].split('=')[0] for call in calls] == ['sort', 'nsfw']
    calls.clear()
    await search_cards('chub', '', 1)
    assert len(calls) == 1 and calls[0].startswith(basic)


def poe_page(bot: dict | None) -> bytes:
    data = {'props': {'pageProps': {'data': {'mainQuery': {'bot': bot}}}}}
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script></body></html>'
    ).encode()


async def test_poe_bot_import(fake_get):
    calls, responses = fake_get
    assert parse_card_link('https://poe.com/Tavern-Keeper') == ('poe', 'Tavern-Keeper')
    for not_a_bot in ('https://poe.com/chat/abc', 'https://poe.com/explore', 'https://poe.com/'):
        assert parse_card_link(not_a_bot) is None

    responses['https://poe.com/Tavern-Keeper'] = poe_page(
        {
            'handle': 'Tavern-Keeper',
            'displayName': 'Tavern Keeper',
            'description': 'A grumpy innkeeper.',
            'introduction': 'What will it be, traveler?',
            'isPromptPublic': True,
            'promptPlaintext': 'You are Bram, the keeper of the Drowned Rat inn.\n\nPersonality: gruff but kind\n\n'
            'Rules: Never break character.',
            'creator': {'handle': 'someone'},
            'model': 'claude_haiku_4_5',
            'picture': {'url': 'https://qph.cf2.poecdn.net/bram.png'},
        }
    )
    responses['https://qph.cf2.poecdn.net/bram.png'] = TINY_PNG
    data, filename = await download_card('poe', 'Tavern-Keeper')
    assert filename == 'card.png'
    character = parse_card_bytes(data, filename).character
    assert character.name == 'Bram'
    assert character.first_mes == 'What will it be, traveler?'
    assert character.personality == 'gruff but kind'
    assert character.system_prompt.startswith('{{original}}') and 'Never break character' in character.system_prompt
    assert character.creator == 'someone' and character.tags == ['Poe']
    assert character.creator_notes.endswith('Imported from Poe: https://poe.com/Tavern-Keeper')

    # A private prompt: the character is built from the public description and greeting.
    responses['https://poe.com/Secret'] = poe_page(
        {
            'handle': 'Secret',
            'displayName': 'Lady Vex',
            'description': 'A scheming duchess who trades in secrets.',
            'introduction': '*She lowers her fan.* You are late.',
            'isPromptPublic': False,
            'promptPlaintext': '',
        }
    )
    data, filename = await download_card('poe', 'Secret')
    character = parse_card_bytes(data, filename).character
    assert character.name == 'Lady Vex'
    assert character.description == 'A scheming duchess who trades in secrets.'
    assert character.first_mes == '*She lowers her fan.* You are late.'
    assert character.system_prompt == ''
    assert 'keeps its prompt private on Poe' in character.creator_notes
    assert character.extensions['poe']['prompt_public'] is False

    # Nothing public at all, or no such bot: clear errors. Poe cannot be searched.
    responses['https://poe.com/Empty'] = poe_page({'handle': 'Empty', 'isPromptPublic': False})
    with pytest.raises(SourceError, match='nothing to import'):
        await download_card('poe', 'Empty')
    responses['https://poe.com/Nobody'] = poe_page(None)
    with pytest.raises(SourceError, match='no bot named'):
        await download_card('poe', 'Nobody')
    with pytest.raises(SourceError, match='No Poe bot has that name'):
        await search_cards('poe', 'Nobody')

    # A picture somewhere else is not fetched; the card comes back as JSON.
    responses['https://poe.com/Elsewhere'] = poe_page(
        {
            'handle': 'Elsewhere',
            'isPromptPublic': True,
            'promptPlaintext': 'You are X.',
            'picture': {'url': 'https://evil.example/x.png'},
        }
    )
    calls.clear()
    data, filename = await download_card('poe', 'Elsewhere')
    assert filename == 'card.json' and calls == ['https://poe.com/Elsewhere']


async def test_downloads_are_cached_briefly(fake_get, monkeypatch):
    calls, responses = fake_get
    responses['https://cards.character-tavern.com/a/b.png'] = TINY_PNG
    assert await download_card('tavern', 'a/b') == await download_card('tavern', 'a/b')
    assert len(calls) == 1  # preview then import: one download

    monkeypatch.setattr(sources, 'CACHE_TTL', 0)
    await download_card('tavern', 'a/b')
    assert len(calls) == 2

    monkeypatch.setattr(sources, 'CACHE_TTL', 600)
    monkeypatch.setattr(sources, 'CACHE_MAX_ITEMS', 2)
    for name in ('c', 'd', 'e'):
        responses[f'https://cards.character-tavern.com/a/{name}.png'] = TINY_PNG
        await download_card('tavern', f'a/{name}')
    assert list(sources._download_cache) == [('tavern', 'a/d'), ('tavern', 'a/e')]


async def test_poe_search_looks_up_pasted_bots(fake_get):
    calls, responses = fake_get
    responses['https://poe.com/-RPG-'] = poe_page(
        {'handle': '-RPG-', 'displayName': 'RPG', 'isPromptPublic': True, 'promptPlaintext': 'Run a game.'}
    )
    responses['https://poe.com/Keeper'] = poe_page(
        {
            'handle': 'Keeper',
            'displayName': 'Tavern Keeper',
            'description': 'A grumpy innkeeper.',
            'isPromptPublic': False,
            'creator': {'handle': 'someone'},
            'picture': {'url': 'https://qph.cf2.poecdn.net/k.png'},
        }
    )
    page = await search_cards('poe', 'https://poe.com/-RPG-, @Keeper\npoe.com/chat/123 Missing -RPG-')
    assert sorted(call.split('poe.com/')[1] for call in calls) == ['-RPG-', 'Keeper', 'Missing']
    assert [(item.id, item.name, item.tags) for item in page.items] == [
        ('-RPG-', 'RPG', ['public prompt']),
        ('Keeper', 'Tavern Keeper', ['private prompt']),
    ]
    assert page.items[1].image_url == 'https://qph.cf2.poecdn.net/k.png' and page.has_more is False
    # The canonical handle imports too.
    data, filename = await download_card('poe', '-RPG-')
    assert parse_card_bytes(data, filename).character.name == 'RPG'
    # Nothing typed yet: no lookups, no error.
    calls.clear()
    assert (await search_cards('poe', '')).items == [] and calls == []


async def test_tag_filters(fake_get):
    calls, responses = fake_get
    # RisuRealm: required tags go into its search syntax; exclusions are applied to the results.
    cards = [
        {'name': 'A', 'id': 'a988c75b-070d-4519-8052-1d64286c8160', 'tags': ['Fantasy', 'knight'], 'type': 'normal'},
        {'name': 'B', 'id': 'b988c75b-070d-4519-8052-1d64286c8160', 'tags': ['fantasy', 'female'], 'type': 'normal'},
        {'name': 'C', 'id': 'c988c75b-070d-4519-8052-1d64286c8160', 'tags': ['sci-fi'], 'type': 'normal'},
    ]
    responses['https://realm.risuai.net/__data.json'] = sveltekit({'cards': cards, 'page': 1})
    page = await search_cards('risu', 'hero', 1, tags=['fantasy', ' high  fantasy,'], exclude_tags=['Female'])
    assert 'q=hero+tag%3Afantasy+tag%3Ahigh-fantasy&' in calls[-1]
    assert [item.name for item in page.items] == []  # nothing has both fantasy and "high fantasy"
    page = await search_cards('risu', '', 1, tags=['FANTASY'], exclude_tags=['female'])
    assert [item.name for item in page.items] == ['A']

    # Character Tavern filters by tag itself (its results carry no tags).
    responses['https://character-tavern.com/search/cards/__data.json'] = sveltekit(
        {
            'searchResults': {
                'hits': [{'id': 'CT_1', 'name': 'R', 'path': 'a/r', 'contentWarnings': []}],
                'totalPages': 1,
            }
        }
    )
    page = await search_cards('tavern', '', 1, tags=['fantasy', 'male'], exclude_tags=['female'])
    assert 'tags=fantasy%2Cmale&exclude_tags=female' in calls[-1]
    assert [item.name for item in page.items] == ['R']

    # Chub gets the tag parameters, and results are checked against their topics too.
    responses['https://gateway.chub.ai/search'] = json.dumps(
        {'data': {'nodes': [{'fullPath': 'a/x', 'name': 'X', 'topics': ['Fantasy']}, {'fullPath': 'a/y', 'name': 'Y'}]}}
    ).encode()
    page = await search_cards('chub', '', 1, tags=['fantasy'], exclude_tags=['NSFL'])
    assert 'tags=fantasy&exclude_tags=NSFL' in calls[-1]
    assert [item.name for item in page.items] == ['X']


PYG_ID = '72f651d3-68eb-484f-8ef4-24d592eb6964'


async def test_pygmalion_search_and_download(fake_get):
    calls, responses = fake_get
    assert parse_card_link(f'https://pygmalion.chat/character/{PYG_ID}') == ('pygmalion', PYG_ID)
    api = 'https://server.pygmalion.chat/galatea.v1.PublicCharacterService'
    responses[f'{api}/CharacterSearch'] = json.dumps(
        {
            'totalItems': '30',
            'characters': [
                {
                    'id': PYG_ID,
                    'displayName': 'Amber',
                    'description': 'A cheerful baker.',
                    'avatarUrl': 'https://assets.pygmalion.chat/abc',
                    'tags': ['Female', 'Fantasy'],
                    'owner': {'displayName': 'Maker'},
                    'downloads': '12',
                    'personalityTokenCount': '900',
                },
                {'id': 'not-a-uuid', 'displayName': 'Bad'},
            ],
        }
    ).encode()
    page = await search_cards('pygmalion', 'baker', 1, 'newest', False, ['fantasy'], ['Anime'])
    assert [item.name for item in page.items] == ['Amber'] and page.has_more is True
    assert page.items[0].tokens == 900 and page.items[0].creator == 'Maker'
    message = json.loads(parse_qs(urlparse(calls[-1]).query)['message'][0])
    assert message['orderBy'] == 'approved_at' and message['page'] == 0
    assert message['tagsNamesInclude'] == ['fantasy'] and message['tagsNamesExclude'] == ['Anime']

    responses[f'{api}/Character'] = json.dumps(
        {
            'character': {
                'id': PYG_ID,
                'displayName': 'Amber',
                'description': 'A cheerful baker.',
                'avatarUrl': 'https://assets.pygmalion.chat/abc',
                'tags': ['Female'],
                'personality': {
                    'persona': '{{char}} bakes bread.',
                    'greeting': 'Fresh loaves!',
                    'alternateGreetings': ['Back again?'],
                    'mesExample': '<START>\n{{char}}: Try this.',
                    'characterNotes': 'Stay cheerful.',
                },
            }
        }
    ).encode()
    responses['https://assets.pygmalion.chat/abc'] = TINY_PNG
    data, filename = await download_card('pygmalion', PYG_ID)
    character = parse_card_bytes(data, filename).character
    assert filename == 'card.png' and character.name == 'Amber'
    assert character.description == '{{char}} bakes bread.' and character.first_mes == 'Fresh loaves!'
    assert character.alternate_greetings == ['Back again?'] and character.creator_notes == 'A cheerful baker.'
    assert character.extensions['depth_prompt']['prompt'] == 'Stay cheerful.'


BACKYARD_ID = 'cmkqhbn9imrhhkb1w2yn3a1ol'


async def test_backyard_search_and_download(fake_get):
    calls, responses = fake_get
    assert parse_card_link(f'https://backyard.ai/hub/character/{BACKYARD_ID}') == ('backyard', BACKYARD_ID)
    char = f'{{_cfg&:{BACKYARD_ID}:cfg&_}}'
    responses['https://backyard.ai/api/trpc/hub.browse.getHubGroupConfigsBySearch'] = json.dumps(
        {
            'result': {
                'data': {
                    'json': {
                        'hubGroupConfigs': [
                            {
                                'id': 'cgroupgroupgroupgroupgrou',
                                'name': 'Bloonarius',
                                'tagline': 'Powerful Ruler',
                                'Tags': [{'name': 'fantasy'}],
                                'Author': {'username': 'maker'},
                                'CharacterConfigs': [
                                    {
                                        'id': BACKYARD_ID,
                                        'Images': [{'imageUrl': 'https://media.backyard.ai/public/x.webp/public'}],
                                    }
                                ],
                            }
                        ],
                        'nextCursor': 50,
                    }
                }
            }
        }
    ).encode()
    page = await search_cards('backyard', 'bloon', 2, 'popular', False)
    assert page.items[0].id == BACKYARD_ID and page.items[0].page_url.endswith(BACKYARD_ID) and page.has_more
    sent = json.loads(parse_qs(urlparse(calls[-1]).query)['input'][0])['json']
    assert sent == {'query': 'bloon', 'cursor': 50, 'sortBy': {'type': 'Relevance', 'direction': 'desc'}}

    responses['https://backyard.ai/api/trpc/hub.browse.getHubCharacterConfigById'] = json.dumps(
        {
            'result': {
                'data': {
                    'json': {
                        'id': BACKYARD_ID,
                        'name': 'Bloonarius',
                        'persona': f'{char} is an airship. {{user}} is small.',
                        'Tags': [{'name': 'fantasy'}],
                        'Author': {'username': 'maker'},
                        'LorebookItems': [{'key': 'Bloon, blimp', 'value': f'{char} is a Bloon.'}],
                        'Images': [{'imageUrl': 'https://media.backyard.ai/public/x.webp/public', 'order': 0}],
                        'standaloneGroupConfig': {
                            'PrimaryChat': {
                                'context': 'A deep cavern.',
                                'modelInstructions': {'type': 'custom', 'customText': 'Be dramatic.'},
                                'HubGreetingMessages': [
                                    {'text': 'Second', 'position': 'b'},
                                    {'text': f'{char} looms.', 'position': 'a'},
                                ],
                                'HubExampleMessages': [
                                    {'hubCharacterConfigId': 'global-user-persona', 'text': 'Hi', 'position': 'a'},
                                    {'hubCharacterConfigId': BACKYARD_ID, 'text': 'Hello, {user}.', 'position': 'b'},
                                ],
                            }
                        },
                    }
                }
            }
        }
    ).encode()
    responses['https://media.backyard.ai/public/x.webp/public'] = TINY_PNG
    data, filename = await download_card('backyard', BACKYARD_ID)
    character = parse_card_bytes(data, filename).character
    assert character.description == '{{char}} is an airship. {{user}} is small.'
    assert character.first_mes == '{{char}} looms.' and character.alternate_greetings == ['Second']
    assert character.scenario == 'A deep cavern.'
    assert character.mes_example == '<START>\n{{user}}: Hi\n{{char}}: Hello, {{user}}.'
    assert character.system_prompt == '{{original}}\n\nBe dramatic.'
    assert character.character_book['entries'][0]['keys'] == ['Bloon', 'blimp']

    responses['https://backyard.ai/api/trpc/hub.browse.getHubCharacterConfigById'] = json.dumps(
        {'result': {'data': {'json': {'notFound': True}}}}
    ).encode()
    with pytest.raises(SourceError, match='no public character'):
        await download_card('backyard', 'cmkqhbn9imrhhkb1w2yn3a1oz')


WYVERN_ID = '_d7e8wpEPXgmWEwLpjbEBY'


async def test_wyvern_search_and_download(fake_get):
    calls, responses = fake_get
    assert parse_card_link(f'https://app.wyvern.chat/characters/{WYVERN_ID}') == ('wyvern', WYVERN_ID)
    assert parse_card_link(f'https://app.wyvern.chat/profiles/maker/characters/{WYVERN_ID}') == ('wyvern', WYVERN_ID)
    character = {
        'id': WYVERN_ID,
        'name': 'He Yanzhi',
        'chat_name': 'Yanzhi',
        'description': 'A ghost cultivator.',
        'first_mes': 'The lanterns flicker.',
        'alternate_greetings': ['Again?'],
        'pre_history_instructions': 'Stay in the ghost city.',
        'character_note': 'Speak softly.',
        'tags': ['Male', 'Fantasy'],
        'rating': 'none',
        'avatar': 'https://imagedelivery.net/abc/def/public',
        'creator': {'displayName': 'Maker'},
        'lorebooks': [{'entries': [], 'lexicon': [{'keys': ['Wuyou'], 'content': 'The Ghost King.', 'entry_id': 'x'}]}],
    }
    responses['https://api.wyvern.chat/exploreSearch/characters'] = json.dumps(
        {'results': [character, {**character, 'id': 'bad', 'name': 'Bad'}], 'hasMore': False}
    ).encode()
    page = await search_cards('wyvern', '', 1, 'trending', False, ['Fantasy'], ['Male'])
    assert page.items == []  # every result had the excluded tag
    query = parse_qs(urlparse(calls[-1]).query)
    assert query['sort'] == ['popular'] and query['tags'] == ['Fantasy'] and query['rating'] == ['none']
    page = await search_cards('wyvern', 'ghost', 1, 'popular', False)
    assert [item.name for item in page.items] == ['Yanzhi']
    assert parse_qs(urlparse(calls[-1]).query)['sort'] == ['relevance']

    responses[f'https://api.wyvern.chat/characters/{WYVERN_ID}'] = json.dumps(character).encode()
    responses['https://imagedelivery.net/abc/def/public'] = TINY_PNG
    data, filename = await download_card('wyvern', WYVERN_ID)
    imported = parse_card_bytes(data, filename).character
    assert imported.name == 'He Yanzhi' and imported.nickname == 'Yanzhi'
    assert imported.system_prompt == 'Stay in the ghost city.'
    assert imported.extensions['depth_prompt']['prompt'] == 'Speak softly.'
    assert imported.character_book['entries'][0]['keys'] == ['Wuyou']


JANNY_ID = '1b2c3d4e-0000-4000-8000-1234567890ab'


def test_janny_links_and_search_config():
    assert parse_card_link(f'https://jannyai.com/characters/{JANNY_ID}_character-sera') == ('janny', JANNY_ID)
    assert parse_card_link('https://jannyai.com/characters/search') is None
    page = '<script type="module" src="/_astro/page.abc.js"></script><link href="/x.css"><script src="https://other.example/a.js">'
    assert sources.janny_script_urls(page) == ['https://jannyai.com/_astro/page.abc.js', 'https://other.example/a.js']
    filler = 'x' * 1000
    bundle = (
        f'const unrelated={{apiKey:"pk_live_notthesearchkeyatall_123456"}};{filler}'
        'new MeiliSearch({host:"https://search.jannyai.com",apiKey:"0123456789abcdef0123456789abcdef"});'
        'client.index("janny-characters").search(q)'
    )
    key, index = sources.find_janny_search_config(['no search host here apiKey:"aaaaaaaaaaaaaaaaaaaaaaaaaaaa"', bundle])
    assert (key, index) == ('0123456789abcdef0123456789abcdef', 'janny-characters')
    assert sources.find_janny_search_config(['nothing']) == (None, None)


def test_janny_hits_are_read_leniently():
    item = sources._janny_summary(
        {
            'id': JANNY_ID.upper(),
            'name': 'Sera the Elf',
            'description': '<p>A <b>forest</b> guardian.</p>',
            'avatar': 'abc.webp',
            'tags': [{'name': 'Fantasy'}, 12, 'Female'],
            'isNsfw': True,
            'totalToken': 1200,
        }
    )
    assert item.id == JANNY_ID and item.tagline == 'A forest guardian.'
    assert item.image_url == 'https://image.jannyai.com/bot-avatars/abc.webp'
    assert item.tags == ['Fantasy', 'Female'] and item.nsfw and item.tokens == 1200
    assert item.page_url == f'https://jannyai.com/characters/{JANNY_ID}_character-sera-the-elf'
    assert sources._janny_summary({'id': 'nope'}) is None


async def test_janny_search_with_configured_key_and_import_from_hits(fake_get, monkeypatch):
    from open_webui.models.config import Config

    calls, responses = fake_get
    monkeypatch.setattr(sources, '_janny_config', {})
    monkeypatch.setattr(sources, '_janny_hits', OrderedDict())
    await Config.upsert(
        {'characters.sources.janny.search_key': 'browser-key', 'characters.sources.janny.search_index': ''}
    )
    meili = []
    hit = {
        'id': JANNY_ID,
        'name': 'Sera',
        'description': 'A forest guardian.',
        'personality': '{{char}} guards the forest.',
        'scenario': 'A glade.',
        'firstMessage': 'Welcome, traveler.',
        'exampleDialogs': '{{user}}: hi',
        'avatar': 'sera.webp',
        'tags': [{'name': 'Fantasy'}],
        'isNsfw': False,
    }

    async def fake_meili(path, key, payload=None):
        meili.append((key, payload))
        index = payload['queries'][0]['indexUid']
        if index != 'characters':  # the first guesses do not exist
            return 404, {'message': 'index not found'}
        return 200, {'results': [{'hits': [hit, {**hit, 'id': 'bad'}], 'estimatedTotalHits': 1}]}

    monkeypatch.setattr(sources, '_janny_meili', fake_meili)
    try:
        page = await search_cards('janny', 'sera', 1, 'popular', False, ['fantasy'])
        assert [item.name for item in page.items] == ['Sera']
        assert all(key == 'browser-key' for key, _ in meili)
        assert not any('jannyai.com/characters/search' in url for url in calls)  # the protected page is skipped
        assert sources._janny_config['index'] == 'characters'

        # The download endpoint is blocked: the card comes from the search result instead.
        async def blocked(card_id):
            raise SourceError('JannyAI’s bot protection blocked this server.')

        monkeypatch.setattr(sources, '_janny_download_card', blocked)
        responses['https://image.jannyai.com/bot-avatars/sera.webp'] = TINY_PNG
        data, filename = await download_card('janny', JANNY_ID)
        character = parse_card_bytes(data, filename).character
        assert character.name == 'Sera' and character.description == '{{char}} guards the forest.'
        assert character.first_mes == 'Welcome, traveler.' and character.scenario == 'A glade.'
        assert character.creator_notes == 'A forest guardian.' and character.tags == ['Fantasy']

        # Never searched: the download's own error stands.
        with pytest.raises(SourceError, match='bot protection'):
            await download_card('janny', '00000000-0000-4000-8000-000000000000')

        # A refused key says so.
        sources._janny_config.clear()

        async def refused(path, key, payload=None):
            return 403, {'message': 'invalid key'}

        monkeypatch.setattr(sources, '_janny_meili', refused)
        with pytest.raises(SourceError, match='refused the search key'):
            await search_cards('janny', 'sera')
    finally:
        await Config.upsert({'characters.sources.janny.search_key': ''})


async def test_poe_search_through_web_search(fake_get):
    calls, responses = fake_get
    responses['https://poe.com/ElfQueen'] = poe_page(
        {'handle': 'ElfQueen', 'displayName': 'Elf Queen', 'description': 'Rules the forest.', 'isPromptPublic': False}
    )
    responses['https://poe.com/Ranger'] = poe_page(
        {'handle': 'Ranger', 'displayName': 'Ranger', 'isPromptPublic': True, 'promptPlaintext': 'You are a ranger.'}
    )
    queries = []

    async def web_search(query):
        queries.append(query)
        return [
            'https://poe.com/ElfQueen',
            'https://poe.com/explore',  # not a bot
            'https://example.com/elf',  # another site
            'https://www.poe.com/Ranger?ref=x',
            'https://poe.com/ElfQueen',  # duplicate
        ]

    page = await search_cards('poe', 'forest elf roleplay', web_search=web_search)
    assert queries == ['site:poe.com forest elf roleplay']
    assert [item.name for item in page.items] == ['Elf Queen', 'Ranger']

    # Links skip the web search; one word is tried as a bot name and searched.
    queries.clear()
    page = await search_cards('poe', 'https://poe.com/Ranger', web_search=web_search)
    assert queries == [] and [item.id for item in page.items] == ['Ranger']
    page = await search_cards('poe', 'Ranger', web_search=web_search)
    assert queries == ['site:poe.com Ranger'] and [item.id for item in page.items] == ['Ranger', 'ElfQueen']

    # Web search failing or off: names still work, and the error says how to search.
    async def broken(query):
        raise RuntimeError('engine down')

    assert [item.id for item in (await search_cards('poe', 'Ranger', web_search=broken)).items] == ['Ranger']
    with pytest.raises(SourceError, match='turn on Web Search'):
        await search_cards('poe', 'forest elves')

    async def nothing(query):
        return []

    with pytest.raises(SourceError, match='No Poe bots found'):
        await search_cards('poe', 'forest elves', web_search=nothing)
