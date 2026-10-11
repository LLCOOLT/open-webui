import base64
import io
import json
import zipfile

import pytest

from open_webui.utils.roleplay.cards import (
    V2_EXPORT_WARNING,
    CardParseError,
    CardTooLargeError,
    character_to_v3,
    normalize_card,
    parse_card_bytes,
    read_png_chunks,
    write_card_png,
)
from open_webui.utils.roleplay.lorebook import normalize_lorebook, parse_decorators


def b64(value: dict) -> str:
    return base64.b64encode(json.dumps(value).encode('utf-8')).decode('ascii')


V1_CARD = {
    'name': 'Seraphina',
    'description': 'A guardian of the forest.',
    'personality': 'kind, protective',
    'scenario': '{{user}} wakes up in her glade.',
    'first_mes': 'You are awake at last.',
    'mes_example': '<START>\n{{user}}: hi\n{{char}}: Hello.',
    'talkativeness': '0.5',
}

BOOK = {
    'name': 'Forest lore',
    'scan_depth': 3,
    'token_budget': 500,
    'recursive_scanning': True,
    'extensions': {},
    'entries': [
        {
            'keys': ['glade'],
            'content': '@@depth 2\nThe glade is warded.',
            'extensions': {},
            'enabled': True,
            'insertion_order': 5,
            'use_regex': True,
        }
    ],
}

V2_CARD = {
    'spec': 'chara_card_v2',
    'spec_version': '2.0',
    'data': {
        **{key: value for key, value in V1_CARD.items() if key != 'talkativeness'},
        'creator_notes': 'Be nice to her.',
        'system_prompt': '{{original}} Stay in character.',
        'post_history_instructions': 'Write one paragraph.',
        'alternate_greetings': ['Hello again.', 'Oh, you.'],
        'character_book': BOOK,
        'tags': ['fantasy'],
        'creator': 'someone',
        'character_version': '1.2',
        'extensions': {'depth_prompt': {'prompt': 'Remember the forest.', 'depth': 4, 'role': 'system'}},
        'x_unknown': 'kept',
    },
}

V3_CARD = {
    'spec': 'chara_card_v3',
    'spec_version': '3.0',
    'data': {
        **V2_CARD['data'],
        'nickname': 'Sera',
        'group_only_greetings': ['Hello, everyone.'],
        'creator_notes_multilingual': {'fr': 'Soyez gentil.'},
        'source': ['https://example.com/sera'],
        'creation_date': 1700000000,
        'modification_date': 1700000500,
        'assets': [
            {'type': 'icon', 'uri': 'ccdefault:', 'name': 'main', 'ext': 'png'},
            {'type': 'background', 'uri': '__asset:bg/forest.png', 'name': 'forest', 'ext': 'png'},
            {'type': 'icon', 'uri': 'https://example.com/remote.png', 'name': 'remote', 'ext': 'png'},
        ],
    },
}

CAI_CARD = {
    'character': {
        'name': ' Detective ',
        'title': 'Hard-boiled investigator',
        'description': 'Solves crimes.',
        'greeting': 'What do you want?',
        'definition': '{{char}}: Speak.',
    }
}

TEXTGEN_CARD = {
    'char_name': 'Bot',
    'char_persona': 'A helpful robot.',
    'world_scenario': 'A lab.',
    'char_greeting': 'Beep.',
    'example_dialogue': 'You: hi\nBot: beep',
}


def as_json_bytes(card: dict) -> bytes:
    return json.dumps(card).encode('utf-8')


####################
# JSON formats
####################


def test_v1_json():
    parsed = parse_card_bytes(as_json_bytes(V1_CARD), 'sera.json')
    character = parsed.character
    assert parsed.source_format == 'json'
    assert parsed.avatar is None
    assert character.spec_version == '1.0'
    assert character.name == 'Seraphina'
    assert character.personality == 'kind, protective'
    assert character.mes_example.startswith('<START>')
    assert character.raw['talkativeness'] == '0.5'


def test_v2_json():
    character = parse_card_bytes(as_json_bytes(V2_CARD)).character
    assert character.spec_version == '2.0'
    assert character.system_prompt == '{{original}} Stay in character.'
    assert character.alternate_greetings == ['Hello again.', 'Oh, you.']
    assert character.nickname is None
    assert character.group_only_greetings == []
    assert character.extensions['depth_prompt']['depth'] == 4
    assert character.raw['data']['x_unknown'] == 'kept'

    book = character.character_book
    assert book is not None
    assert book['name'] == 'Forest lore'
    assert book['scan_depth'] == 3
    assert book['recursive_scanning'] is True
    entry = book['entries'][0]
    assert entry['keys'] == ['glade']
    assert entry['use_regex'] is True
    assert entry['position'] == 'before_char'
    assert entry['priority'] == 10


def test_v3_json():
    character = parse_card_bytes(as_json_bytes(V3_CARD)).character
    assert character.spec_version == '3.0'
    assert character.nickname == 'Sera'
    assert character.group_only_greetings == ['Hello, everyone.']
    assert character.creator_notes_multilingual == {'fr': 'Soyez gentil.'}
    assert character.creation_date == 1700000000
    assert len(character.assets) == 3


def test_v2_without_spec_field_is_detected():
    card = {'data': V2_CARD['data']}
    assert normalize_card(card).spec_version == '2.0'


def test_character_ai_json():
    character = parse_card_bytes(as_json_bytes(CAI_CARD)).character
    assert character.spec_version == 'cai'
    assert character.name == 'Detective'
    assert character.description == 'Solves crimes.'
    assert character.creator_notes == 'Hard-boiled investigator'
    assert character.first_mes == 'What do you want?'
    assert character.mes_example == '{{char}}: Speak.'


def test_character_ai_history_json():
    character = normalize_card({'info': CAI_CARD})
    assert character.name == 'Detective'


def test_textgen_json():
    character = parse_card_bytes(as_json_bytes(TEXTGEN_CARD)).character
    assert character.spec_version == 'textgen'
    assert character.name == 'Bot'
    assert character.description == 'A helpful robot.'
    assert character.scenario == 'A lab.'
    assert character.first_mes == 'Beep.'


def test_utf8_bom_and_wrong_types_are_tolerated():
    card = {'spec': 'chara_card_v2', 'data': {'name': 'X', 'description': None, 'tags': ['a', 3], 'extensions': []}}
    character = parse_card_bytes(b'\xef\xbb\xbf' + as_json_bytes(card)).character
    assert character.description == ''
    assert character.tags == ['a']
    assert character.extensions == {}


@pytest.mark.parametrize(
    'data',
    [b'not json', b'[1, 2]', as_json_bytes({'foo': 'bar'}), as_json_bytes({'spec': 'chara_card_v2', 'data': {}})],
)
def test_invalid_json_cards_are_rejected(data):
    with pytest.raises(CardParseError):
        parse_card_bytes(data)


def test_raw_is_dropped_when_too_large():
    card = {**V1_CARD, 'padding': 'x' * (1024 * 1024 + 1)}
    character = normalize_card(card)
    assert character.raw == {}
    assert character.raw_truncated is True
    assert character.name == 'Seraphina'


####################
# PNG
####################


def test_v2_png(png_factory):
    png = png_factory(text_chunks={'chara': b64(V2_CARD)})
    parsed = parse_card_bytes(png, 'sera.png')
    assert parsed.source_format == 'png'
    assert parsed.character.spec_version == '2.0'
    assert parsed.avatar_content_type == 'image/png'
    # The stored avatar keeps the image but drops the embedded card.
    chunk_types = [chunk_type for chunk_type, _ in read_png_chunks(parsed.avatar)]
    assert b'tEXt' not in chunk_types
    assert b'IDAT' in chunk_types


def test_png_prefers_ccv3_over_chara(png_factory):
    png = png_factory(text_chunks={'chara': b64(V2_CARD), 'ccv3': b64(V3_CARD)})
    assert parse_card_bytes(png).character.spec_version == '3.0'

    # Order of chunks does not matter.
    png = png_factory(text_chunks={'ccv3': b64(V3_CARD), 'chara': b64(V2_CARD)})
    assert parse_card_bytes(png).character.nickname == 'Sera'


def test_v3_png_resolves_embedded_background(png_factory):
    background = png_factory(width=2, height=2)
    png = png_factory(
        text_chunks={
            'ccv3': b64(V3_CARD),
            'chara-ext-asset_:bg/forest.png': base64.b64encode(background).decode('ascii'),
        }
    )
    parsed = parse_card_bytes(png)
    assert parsed.background == background
    assert parsed.background_content_type == 'image/png'
    assert parsed.avatar_content_type == 'image/png'


def test_png_with_corrupt_crc_is_rejected(png_factory):
    png = bytearray(png_factory(text_chunks={'chara': b64(V2_CARD)}))
    png[-20] ^= 0xFF  # inside the tEXt chunk payload
    with pytest.raises(CardParseError, match='CRC'):
        parse_card_bytes(bytes(png))


def test_png_without_card_is_rejected(png_factory):
    with pytest.raises(CardParseError, match='does not contain'):
        parse_card_bytes(png_factory())


def test_truncated_png_is_rejected(png_factory):
    png = png_factory(text_chunks={'chara': b64(V2_CARD)})
    with pytest.raises(CardParseError):
        parse_card_bytes(png[:-30])


####################
# CHARX
####################


def make_charx(card: dict, files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('card.json', json.dumps(card))
        for name, data in (files or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_minimal_charx(png_factory):
    icon = png_factory(width=3, height=3)
    card = {
        **V3_CARD,
        'data': {
            **V3_CARD['data'],
            'assets': [{'type': 'icon', 'uri': 'embeded://assets/icon/images/main.png', 'name': 'main', 'ext': 'png'}],
        },
    }
    parsed = parse_card_bytes(make_charx(card, {'assets/icon/images/main.png': icon}), 'sera.charx')
    assert parsed.source_format == 'charx'
    assert parsed.character.nickname == 'Sera'
    assert parsed.avatar == icon
    assert parsed.background is None


def test_png_with_zero_iend_crc(png_factory):
    # RisuAI writes IEND with a zero CRC; other chunks are still checked.
    png = png_factory(text_chunks={'chara': base64.b64encode(json.dumps(V2_CARD).encode()).decode()})
    assert png.endswith(b'IEND\xaeB`\x82')
    parsed = parse_card_bytes(png[:-4] + b'\x00\x00\x00\x00', 'risu.png')
    assert parsed.character.name == V2_CARD['data']['name']


def test_charx_appended_to_a_jpeg():
    # RisuRealm serves CHARX as a JPEG with the archive appended, so it still previews as an image.
    jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 64 + b'\xff\xd9'
    parsed = parse_card_bytes(jpeg + make_charx(V3_CARD), 'card.charx')
    assert parsed.source_format == 'charx'
    assert parsed.character.nickname == 'Sera'


def test_charx_without_card_json_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('other.json', '{}')
    with pytest.raises(CardParseError, match='card.json'):
        parse_card_bytes(buffer.getvalue())


def test_charx_uncompressed_size_is_limited():
    data = make_charx(V3_CARD, {'assets/big.bin': b'\x00' * 100_000})
    assert len(data) < 10_000
    with pytest.raises(CardTooLargeError):
        parse_card_bytes(data, max_bytes=50_000)


def test_remote_assets_are_not_fetched():
    card = {
        **V3_CARD,
        'data': {
            **V3_CARD['data'],
            'assets': [{'type': 'icon', 'uri': 'https://example.com/a.png', 'name': 'main', 'ext': 'png'}],
        },
    }
    parsed = parse_card_bytes(make_charx(card))
    assert parsed.avatar is None


####################
# Size limit
####################


def test_oversize_card_is_rejected():
    with pytest.raises(CardTooLargeError):
        parse_card_bytes(as_json_bytes(V1_CARD), 'sera.json', max_bytes=10)


####################
# Export and round trip
####################


def test_v3_export_keeps_passthrough_fields_and_edits():
    character = normalize_card(V3_CARD)
    character.description = 'Edited.'
    card = character_to_v3(character)
    assert card['spec'] == 'chara_card_v3'
    assert card['data']['description'] == 'Edited.'
    assert card['data']['x_unknown'] == 'kept'
    assert card['data']['assets'] == [{'type': 'icon', 'uri': 'ccdefault:', 'name': 'main', 'ext': 'png'}]


def test_png_export_writes_both_chunks(png_factory):
    character = normalize_card(V3_CARD)
    png = write_card_png(png_factory(text_chunks={'chara': 'stale', 'other': 'kept'}), character_to_v3(character))

    texts = {}
    for chunk_type, chunk_data in read_png_chunks(png):
        if chunk_type == b'tEXt':
            keyword, text = chunk_data.split(b'\x00', 1)
            texts[keyword.decode()] = text.decode()
    assert set(texts) == {'other', 'chara', 'ccv3'}

    v2 = json.loads(base64.b64decode(texts['chara']))
    assert v2['spec'] == 'chara_card_v2'
    assert 'nickname' not in v2['data']
    assert 'assets' not in v2['data']
    assert v2['data']['creator_notes'].endswith(V2_EXPORT_WARNING)
    v2_entry = v2['data']['character_book']['entries'][0]
    assert v2_entry['content'] == 'The glade is warded.'
    assert 'use_regex' not in v2_entry

    v3 = json.loads(base64.b64decode(texts['ccv3']))
    assert v3['data']['character_book']['entries'][0]['content'].startswith('@@depth 2')


@pytest.mark.parametrize('card', [V1_CARD, V2_CARD, V3_CARD, CAI_CARD])
def test_round_trip(png_factory, card):
    first = parse_card_bytes(png_factory(text_chunks={'chara': b64(card)})).character
    exported = write_card_png(png_factory(), character_to_v3(first))
    second = parse_card_bytes(exported).character

    exclude = {'raw', 'spec_version', 'assets'}
    assert second.model_dump(exclude=exclude) == first.model_dump(exclude=exclude)
    assert second.spec_version == '3.0'

    # A second export and import is stable, including the asset list.
    third = parse_card_bytes(write_card_png(png_factory(), character_to_v3(second))).character
    assert third.model_dump(exclude={'raw'}) == second.model_dump(exclude={'raw'})


####################
# Lorebook helpers
####################


def test_normalize_lorebook_v3_wrapper_and_world_info_dict():
    book = normalize_lorebook({'spec': 'lorebook_v3', 'data': {'name': 'W', 'entries': {'0': {'key': 'a, b'}}}})
    assert book['name'] == 'W'
    assert book['entries'][0]['keys'] == ['a', 'b']
    assert book['entries'][0]['id'] == 0
    assert normalize_lorebook('nope') is None


def test_parse_decorators():
    decorators, body = parse_decorators('@@depth 4\n@@unknown x\n@@@role user\n@@activate\nBody\n@@depth 1')
    assert decorators == [('depth', '4'), ('role', 'user'), ('activate', '')]
    assert body == 'Body\n@@depth 1'

    # A fallback is ignored when the preceding decorator was understood.
    decorators, body = parse_decorators('@@role system\n@@@role user\nText')
    assert decorators == [('role', 'system')]
    assert body == 'Text'

    assert parse_decorators('Plain text') == ([], 'Plain text')
    assert parse_decorators('@@depth 3') == ([('depth', '3')], '')
