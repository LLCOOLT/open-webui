import json

import pytest

from open_webui.utils.roleplay.story import (
    MAX_STORY_CHARS,
    analyze_story,
    character_excerpts,
    greeting_prompt,
    parse_json_object,
    profile_prompt,
    split_story,
    write_profile,
)

pytestmark = pytest.mark.asyncio(loop_scope='session')


def test_split_story_respects_paragraphs_and_size():
    paragraphs = [f'Paragraph {index}. ' + 'word ' * 50 for index in range(40)]
    chunks = split_story('\n\n'.join(paragraphs), chunk_chars=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert ''.join(chunks).count('Paragraph') == 40
    assert chunks[0].startswith('Paragraph 0.')


def test_split_story_cuts_overlong_paragraphs_on_sentences():
    text = ' '.join(f'Sentence {index} is here.' for index in range(200))
    chunks = split_story(text, chunk_chars=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert all(chunk.endswith('.') for chunk in chunks[:-1])


def test_parse_json_object():
    assert parse_json_object('Here:\n```json\n{"a": 1}\n```') == {'a': 1}
    assert parse_json_object('<think>maybe {x}?</think>{"a": 2}') == {'a': 2}
    for bad in ('nothing', '{nope}', '[1, 2]'):
        with pytest.raises(ValueError):
            parse_json_object(bad)


CHUNK_REPLIES = [
    {
        'characters': [
            {
                'name': 'Elena',
                'aliases': ['Lena'],
                'description': 'Elena is a young cartographer from Port Vey.',
                'personality': 'curious, stubborn',
                'speech': 'quick and precise',
                'quotes': ['"Maps never lie, people do."'],
            },
            {'name': 'Captain Holt', 'description': 'Holt commands the Gull.', 'quotes': ['Hold fast.']},
        ],
        'world': [{'name': 'Port Vey', 'kind': 'place', 'content': 'A foggy harbor town.'}],
        'events': ['Elena boards the Gull.'],
    },
    {
        'characters': [
            {'name': 'Lena', 'description': 'Lena lost her father at sea.', 'quotes': ['I will find him.']},
            {'name': 'Marrow', 'description': 'A smuggler.'},
        ],
        'world': [
            {'name': 'Port Vey', 'content': 'Its lighthouse has been dark for years.'},
            {'name': 'The Gull', 'kind': 'object', 'aliases': ['Gull'], 'content': 'Holt’s old schooner.'},
        ],
        'events': ['A storm hits.', 'Elena boards the Gull.'],
    },
]


def scripted(chunk_replies, consolidated=None, profile=None):
    calls = []

    async def complete(prompt: str) -> str:
        calls.append(prompt)
        if prompt.startswith('Write a detailed'):
            return json.dumps(profile) if profile is not None else 'no profile'
        if prompt.startswith('These are notes about'):
            if consolidated is None:
                return 'not json'
            return json.dumps(consolidated)
        part = int(prompt.split('part ', 1)[1].split(' ', 1)[0])
        reply = chunk_replies[part - 1]
        if isinstance(reply, Exception):
            raise reply
        return 'Sure:\n' + json.dumps(reply)

    return complete, calls


STORY = 'First half of the story. ' * 400 + '\n\n' + 'Second half of the story. ' * 400  # two chunks


async def test_analyze_merges_characters_and_world():
    complete, calls = scripted(CHUNK_REPLIES)
    draft = await analyze_story(STORY, complete, 'The Dark Light')
    assert draft.chunks == 2
    assert draft.failed_chunks == 0

    elena = draft.characters[0]
    assert elena['name'] == 'Elena'
    assert elena['aliases'] == ['Lena']
    assert elena['mentions'] == 2
    assert elena['selected'] is True
    # Notes from both chunks, with her names replaced by {{char}}.
    assert elena['description'] == (
        '{{char}} is a young cartographer from Port Vey.\n\n{{char}} lost her father at sea.'
    )
    assert elena['personality'] == 'curious, stubborn\nSpeech: quick and precise'
    assert elena['mes_example'] == '<START>\n{{char}}: Maps never lie, people do.\n{{char}}: I will find him.'
    assert elena['creator_notes'] == 'Adapted from The Dark Light.'

    # Characters seen in only one part are drafted but not preselected.
    assert {c['name']: c['selected'] for c in draft.characters[1:]} == {'Captain Holt': False, 'Marrow': False}

    entries = {entry['name']: entry for entry in draft.lorebook['entries']}
    assert draft.lorebook['name'] == 'The Dark Light world'
    assert entries['Port Vey']['content'] == 'Port Vey: A foggy harbor town. Its lighthouse has been dark for years.'
    assert entries['The Gull']['keys'] == ['The Gull', 'Gull']
    assert entries['Elena']['comment'] == 'character'
    assert draft.events == ['Elena boards the Gull.', 'A storm hits.']
    # Elena has 3 notes across chunks, so she got a consolidation call (which failed, keeping notes).
    assert sum(call.startswith('These are notes about Elena') for call in calls) == 1


async def test_consolidation_replaces_notes():
    complete, _ = scripted(CHUNK_REPLIES, {'description': 'Elena, a cartographer.', 'personality': 'Curious.'})
    draft = await analyze_story(STORY, complete)
    assert draft.characters[0]['description'] == '{{char}}, a cartographer.'
    assert draft.characters[0]['personality'] == 'Curious.'


async def test_failed_chunks_are_reported_not_fatal():
    complete, _ = scripted([CHUNK_REPLIES[0], ValueError('timeout')])
    draft = await analyze_story(STORY, complete)
    assert draft.failed_chunks == 1
    assert draft.characters[0]['name'] == 'Elena'

    complete, _ = scripted([ValueError('down'), ValueError('down')])
    with pytest.raises(ValueError, match='could not analyze'):
        await analyze_story(STORY, complete)


async def test_limits():
    complete, _ = scripted(CHUNK_REPLIES)
    with pytest.raises(ValueError, match='empty'):
        await analyze_story('   ', complete)
    with pytest.raises(ValueError, match='too long'):
        await analyze_story('x' * (MAX_STORY_CHARS + 1), complete)


def test_greeting_prompt_names_the_character():
    prompt = greeting_prompt(
        {'name': 'Elena', 'description': '{{char}} maps coasts.', 'personality': 'curious'}, 'Aboard the Gull.'
    )
    assert 'Elena maps coasts.' in prompt
    assert 'Aboard the Gull.' in prompt
    assert '{{user}}' in prompt


RICH_REPLIES = [
    {
        'characters': [
            {
                'name': 'Mira',
                'appearance': 'Mira has a burn scar across her left hand.',
                'background': 'Mira was a palace cook before the fire.',
                'personality': 'wry, protective',
                'speech': 'dry one-liners',
                'relationships': [{'name': 'Tomas', 'relation': 'Mira raised Tomas after the fire.'}],
                'goals': 'Keep Tomas out of the war.',
                'quotes': ['Eat first, panic later.'],
            },
            {'name': 'Tomas', 'background': 'A stable boy.', 'relationships': {'Mira': 'adores her'}},
        ],
        'events': ['The fire.'],
    },
    {
        'characters': [
            {
                'name': 'Mira',
                'relationships': 'Captain Rell: owes him a debt\nMira: herself',
                'goals': 'Repay Rell.',
            }
        ],
    },
]


async def test_rich_fields_become_a_structured_sheet():
    complete, _ = scripted(RICH_REPLIES)
    draft = await analyze_story(STORY, complete, detailed=False)
    mira = draft.characters[0]
    assert mira['description'] == (
        'Appearance: {{char}} has a burn scar across her left hand.\n\n'
        'Background: {{char}} was a palace cook before the fire.\n\n'
        'Relationships: \n- Tomas: {{char}} raised Tomas after the fire.\n- Captain Rell: owes him a debt\n\n'
        'Goals: Keep Tomas out of the war. Repay Rell.'
    )
    assert mira['personality'] == 'wry, protective\nSpeech: dry one-liners'
    assert mira['detailed'] is False
    entries = {entry['name']: entry for entry in draft.lorebook['entries']}
    assert entries['Mira']['content'].startswith('Mira: Mira has a burn scar')


async def test_consolidation_keeps_structure():
    consolidated = {
        'appearance': 'Scarred hand.',
        'background': 'Former palace cook.',
        'personality': 'Wry.',
        'speech': 'Dry.',
        'relationships': 'Tomas: her ward',
        'goals': 'Protect Tomas.',
    }
    complete, calls = scripted(RICH_REPLIES, consolidated)
    draft = await analyze_story(STORY, complete, detailed=False)
    assert 'Relationship with Tomas' in next(call for call in calls if call.startswith('These are notes about Mira'))
    assert draft.characters[0]['description'] == (
        'Appearance: Scarred hand.\n\nBackground: Former palace cook.\n\n'
        'Relationships: \n- Tomas: her ward\n\nGoals: Protect Tomas.'
    )
    assert draft.characters[0]['personality'] == 'Wry.\nSpeech: Dry.'


PROFILE = {
    'appearance': 'Elena wears a salt-stained coat.',
    'background': 'Raised in Port Vey.',
    'personality': 'Curious and stubborn.',
    'speech': 'Clipped.',
    'mannerisms': 'Taps her compass.',
    'relationships': [{'name': 'Captain Holt', 'relation': 'distrusts him'}, {'name': 'Lena', 'relation': 'self'}],
    'goals': 'Find her father.',
    'example_dialogue': [
        {'user': 'Where are we?', 'char': '*checks the chart* Two days out of Port Vey.'},
        {'user': '', 'char': 'Maps never lie.'},
    ],
}


async def test_detailed_profiles_for_main_characters():
    complete, calls = scripted(CHUNK_REPLIES, profile=PROFILE)
    draft = await analyze_story(STORY, complete)
    elena = draft.characters[0]
    assert elena['detailed'] is True
    assert elena['description'] == (
        'Appearance: {{char}} wears a salt-stained coat.\n\nBackground: Raised in Port Vey.\n\n'
        'Relationships: \n- Captain Holt: distrusts him\n\nGoals: Find her father.'
    )
    assert elena['personality'] == 'Curious and stubborn.\n\nSpeech: Clipped.\n\nMannerisms: Taps her compass.'
    assert elena['mes_example'] == (
        '<START>\n{{user}}: Where are we?\n{{char}}: *checks the chart* Two days out of Port Vey.\n'
        '<START>\n{{char}}: Maps never lie.'
    )
    # Only selected characters get a profile call.
    assert [call.split(', from the story')[0] for call in calls if call.startswith('Write a detailed')] == [
        'Write a detailed roleplay character sheet for Elena (also called Lena)'
    ]


async def test_failed_profile_keeps_the_draft():
    complete, _ = scripted(CHUNK_REPLIES)
    draft = await analyze_story(STORY, complete)
    assert draft.characters[0]['detailed'] is False
    assert draft.characters[0]['description'].startswith('{{char}} is a young cartographer')


async def test_write_profile_for_one_character():
    complete, calls = scripted([], profile={'personality': 'Bold.'})
    story = 'Elena laughed.\n\nThe sea was calm.\n\nNobody spoke.'
    result = await write_profile(
        {'name': 'Elena', 'description': '{{char}} maps coasts.', 'mes_example': 'old'}, story, complete, 'Gull'
    )
    # Empty fields keep the draft's values.
    assert result == {'description': '{{char}} maps coasts.', 'personality': 'Bold.', 'mes_example': 'old'}
    assert 'Elena maps coasts.' in calls[0]
    assert '<passages>\nElena laughed.\n\nThe sea was calm.\n</passages>' in calls[0]


def test_character_excerpts_follow_mentions_within_budget():
    paragraphs = [f'Filler {index}.' for index in range(100)]
    for index in (5, 40, 90):
        paragraphs[index] = f'Lena speaks in scene {index}.'
    text = '\n\n'.join(paragraphs)
    excerpts = character_excerpts(text, ['Elena', 'Lena'])
    assert excerpts.split('\n\n') == [
        'Lena speaks in scene 5.',
        'Filler 6.',
        '[…]',
        'Lena speaks in scene 40.',
        'Filler 41.',
        '[…]',
        'Lena speaks in scene 90.',
        'Filler 91.',
    ]
    # Whole words only: "Elenas" and "Magdalena" are not mentions.
    assert character_excerpts('Magdalena sings.\n\nElenas run.', ['Elena', 'Lena']) == ''

    many = '\n\n'.join(f'Lena acts in scene {index}. ' + 'x' * 200 for index in range(400))
    sampled = character_excerpts(many, ['Lena'], budget=5_000)
    assert len(sampled) <= 6_000
    assert 'scene 0.' in sampled and 'scene 399.' not in sampled[:100]
    assert sampled.count('Lena acts') >= 10


def test_profile_prompt_includes_notes_and_passages():
    prompt = profile_prompt(
        {'name': 'Elena', 'aliases': ['Lena', 'Elena'], 'personality': '{{char}} is curious.'},
        'Lena smiled.',
        'The Gull',
    )
    assert prompt.startswith(
        'Write a detailed roleplay character sheet for Elena (also called Lena), from the story "The Gull"'
    )
    assert 'Elena is curious.' in prompt
    assert 'Lena smiled.' in prompt
