import base64
import json
import random

import pytest

from open_webui.utils.roleplay.cards import parse_card_bytes
from open_webui.utils.roleplay.character import CharacterMeta, DepthPrompt, character_summary
from open_webui.utils.roleplay.prompt import (
    MacroContext,
    apply_character_history,
    apply_character_system_prompt,
    build_macro_context,
    compile_static_system_prompt,
    expand_macros,
    greetings,
    inject_into_history,
    render_system_prompt,
)


def ctx(**overrides) -> MacroContext:
    values = {'char': 'Sera', 'user': 'Alex', 'original': 'BASE PROMPT', 'chat_id': 'chat-1', 'rng': random.Random(7)}
    return MacroContext(**{**values, **overrides})


def character(**fields) -> CharacterMeta:
    return CharacterMeta(**{'name': 'Seraphina', **fields})


####################
# Macros
####################


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('{{char}} meets {{user}}', 'Sera meets Alex'),
        ('{{CHAR}} and {{ User }}', 'Sera and Alex'),
        ('a{{// hidden note}}b', 'ab'),
        ('a{{hidden_key:secret}}b', 'ab'),
        ('a{{comment: note}}b', 'ab'),
        ('{{reverse:abc}}', 'cba'),
        ('{{persona}}', 'Tall.'),
        ('{{USER_NAME}} {{chat.variables.mood}}', '{{USER_NAME}} {{chat.variables.mood}}'),
        ('{{unknown:value}}', '{{unknown:value}}'),
    ],
)
def test_expand_macros(text, expected):
    assert expand_macros(text, ctx(persona='Tall.')) == expected


def test_original_only_where_allowed():
    assert expand_macros('{{original}} x', ctx(), allow_original=True) == 'BASE PROMPT x'
    assert expand_macros('{{original}} x', ctx()) == ' x'


def test_random_choices_and_escaped_commas():
    seen = {expand_macros('{{random:a,b\\,c}}', ctx(rng=random.Random(seed))) for seed in range(50)}
    assert seen == {'a', 'b,c'}
    seen = {expand_macros('{{random::x::y}}', ctx(rng=random.Random(seed))) for seed in range(50)}
    assert seen == {'x', 'y'}


def test_random_can_use_nested_simple_macros():
    assert expand_macros('{{random:{{char}}}}', ctx()) == 'Sera'


def test_pick_is_stable_per_chat():
    text = '{{pick::red,green,blue}} {{pick::red,green,blue}}'
    first = expand_macros(text, ctx(rng=random.Random(1)))
    assert expand_macros(text, ctx(rng=random.Random(2))) == first
    values = {expand_macros('{{pick::red,green,blue}}', ctx(chat_id=f'chat-{index}')) for index in range(30)}
    assert values == {'red', 'green', 'blue'}


def test_roll():
    rolls = {int(expand_macros('{{roll:d6}}', ctx(rng=random.Random(seed)))) for seed in range(100)}
    assert rolls == {1, 2, 3, 4, 5, 6}
    assert expand_macros('{{roll:3}}', ctx()) in {'1', '2', '3'}
    assert expand_macros('{{roll:zero}}', ctx()) == '{{roll:zero}}'
    assert expand_macros('{{roll:0}}', ctx()) == '{{roll:0}}'


def test_static_context_leaves_dynamic_macros():
    assert expand_macros('{{random:a,b}} {{roll:6}} {{char}}', ctx(rng=None)) == '{{random:a,b}} {{roll:6}} Sera'


def test_build_macro_context_expands_original_and_prefers_nickname():
    context = build_macro_context(
        character(nickname='Sera'), model_name='m', user_name='Alex', original='Play {{char}} for {{user}}.'
    )
    assert context.char == 'Sera'
    assert context.original == 'Play Sera for Alex.'
    assert build_macro_context(character(name=''), model_name='Model', user_name='A').char == 'Model'


####################
# System block
####################

FULL = {
    'description': '{{char}} guards the forest.',
    'personality': 'kind',
    'scenario': '{{user}} wakes in the glade.',
    'mes_example': '<START>\n{{user}}: hi\n{{char}}: Hello.\n<START>\n{{char}}: Again.',
}


def test_system_block_order():
    rendered = render_system_prompt(character(**FULL), ctx(), lore_before=['LORE A'], lore_after=['LORE B'])
    assert rendered == (
        'BASE PROMPT\n\n'
        'LORE A\n\n'
        'Sera guards the forest.\n\n'
        "Sera's personality: kind\n\n"
        'Scenario: Alex wakes in the glade.\n\n'
        'LORE B\n\n'
        '### New example chat\nAlex: hi\nSera: Hello.\n\n'
        '### New example chat\nSera: Again.'
    )


def test_empty_parts_are_skipped():
    assert render_system_prompt(character(description='Just this.'), ctx(original='')) == 'Just this.'


@pytest.mark.parametrize(
    ('system_prompt', 'mode', 'expected'),
    [
        ('', 'replace', 'BASE PROMPT'),
        ('   ', 'replace', 'BASE PROMPT'),
        ('Card prompt.', 'replace', 'Card prompt.'),
        ('Card prompt.', 'supplement', 'BASE PROMPT\n\nCard prompt.'),
        ('Before. {{original}} After.', 'replace', 'Before. BASE PROMPT After.'),
        ('Before. {{original}}', 'supplement', 'Before. BASE PROMPT'),
    ],
)
def test_system_prompt_override(system_prompt, mode, expected):
    meta = character(system_prompt=system_prompt, prompt_overrides={'system_prompt': mode})
    assert render_system_prompt(meta, ctx()) == expected


def test_original_is_not_expanded_outside_the_system_prompt():
    assert render_system_prompt(character(description='x {{original}}'), ctx(original='')) == 'x'


def test_examples_in_messages_mode_are_not_in_the_system_block():
    meta = character(**FULL, example_mode='messages')
    assert '### New example chat' not in render_system_prompt(meta, ctx())


def test_static_prompt_for_other_code_paths():
    meta = character(**FULL, system_prompt='{{original}}Be {{char}}. {{random:a,b}}')
    static = compile_static_system_prompt(meta, 'Model')
    assert static.startswith('Be Seraphina. {{random:a,b}}')
    assert 'Scenario: {{USER_NAME}} wakes in the glade.' in static


####################
# Messages
####################


def test_apply_system_prompt_replaces_the_user_system_message():
    messages = [{'role': 'system', 'content': 'BASE PROMPT'}, {'role': 'user', 'content': 'hi'}]
    result = apply_character_system_prompt(messages, character(description='D.'), ctx())
    assert result == [{'role': 'system', 'content': 'BASE PROMPT\n\nD.'}, {'role': 'user', 'content': 'hi'}]

    result = apply_character_system_prompt([{'role': 'user', 'content': 'hi'}], character(), ctx(original=''))
    assert result == [{'role': 'user', 'content': 'hi'}]


def history(count: int) -> list[dict]:
    return [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': f'm{index}'} for index in range(count)]


def contents(messages: list[dict]) -> list[str]:
    return [message['content'] for message in messages]


def test_depth_prompts_merge_into_user_messages():
    messages = [{'role': 'system', 'content': 'S'}, *history(5)]  # m0 u, m1 a, m2 u, m3 a, m4 u
    result = inject_into_history(
        messages,
        [
            DepthPrompt(text='D0', depth=0),
            DepthPrompt(text='D2', depth=2, role='user'),
            DepthPrompt(text='D3', depth=3),
            DepthPrompt(text='D99', depth=99),
            DepthPrompt(text='DA', depth=1, role='assistant'),
            DepthPrompt(text='', depth=1),
        ],
    )
    assert contents(result) == ['S', 'D99\n\nm0', 'm1', 'D3\n\nm2', 'm3', 'DA', 'D2\n\nm4\n\nD0']
    assert [message['role'] for message in result] == [
        'system',
        'user',
        'assistant',
        'user',
        'assistant',
        'assistant',
        'user',
    ]
    # The caller's messages are not modified.
    assert contents(messages) == ['S', 'm0', 'm1', 'm2', 'm3', 'm4']


def test_depth_injection_never_precedes_start():
    messages = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'EX'}, *history(1)]
    result = inject_into_history(messages, [DepthPrompt(text='D', depth=10)], start=2)
    assert contents(result) == ['S', 'EX', 'D\n\nm0']


def test_depth_prompt_without_user_messages_becomes_a_user_message():
    messages = [{'role': 'system', 'content': 'S'}, {'role': 'assistant', 'content': 'Hi'}]
    result = inject_into_history(messages, [DepthPrompt(text='D', depth=0)], post_history='PH')
    assert result == [*messages, {'role': 'user', 'content': 'D'}]


def test_post_history_appends_to_the_last_user_message():
    messages = [{'role': 'system', 'content': 'S'}, *history(3)]
    result = inject_into_history(messages, [DepthPrompt(text='D0', depth=0)], post_history='PH')
    assert contents(result) == ['S', 'm0', 'm1', 'm2\n\nD0\n\nPH']

    # Continuing an assistant message: it still goes to the last user message.
    messages = [{'role': 'system', 'content': 'S'}, *history(4)]
    assert contents(inject_into_history(messages, [], post_history='PH')) == ['S', 'm0', 'm1', 'm2\n\nPH', 'm3']


def test_injection_into_multipart_user_content():
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,x'}}
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'look'}, image]}]
    result = inject_into_history(messages, [DepthPrompt(text='D', depth=1)], post_history='PH')
    assert result[0]['content'] == [{'type': 'text', 'text': 'D\n\nlook\n\nPH'}, image]
    assert messages[0]['content'][0]['text'] == 'look'


@pytest.mark.parametrize(
    ('instructions', 'mode', 'expected'),
    [('', 'replace', None), ('Stay brief, {{user}}.', 'replace', 'Stay brief, Alex.')],
)
def test_post_history_instructions(instructions, mode, expected):
    meta = character(post_history_instructions=instructions, prompt_overrides={'post_history': mode})
    result = apply_character_history([{'role': 'system', 'content': 'S'}, *history(1)], meta, ctx())
    if expected is None:
        assert contents(result) == ['S', 'm0']
    else:
        assert result[-1] == {'role': 'user', 'content': f'm0\n\n{expected}'}


def test_example_messages_mode():
    meta = character(
        mes_example='<START>\n{{user}}: hi\nthere\n{{char}}: Hello, {{user}}.\n<START>\n{{char}}: Again.',
        example_mode='messages',
    )
    result = apply_character_history([{'role': 'system', 'content': 'S'}, *history(1)], meta, ctx())
    assert result == [
        {'role': 'system', 'content': 'S'},
        {'role': 'user', 'content': 'hi\nthere'},
        {'role': 'assistant', 'content': 'Hello, Alex.'},
        {'role': 'assistant', 'content': 'Again.'},
        {'role': 'user', 'content': 'm0'},
    ]


def test_character_depth_prompt_and_extra_prompts():
    meta = character(depth_prompt={'text': 'Remember {{char}}.', 'depth': 1, 'role': 'system'})
    result = apply_character_history(
        [{'role': 'system', 'content': 'S'}, *history(3)],
        meta,
        ctx(),
        extra_depth_prompts=[DepthPrompt(text="Author's note", depth=0)],
    )
    assert contents(result) == ['S', 'm0', 'm1', "Remember Sera.\n\nm2\n\nAuthor's note"]


def test_greetings():
    meta = character(first_mes='Hi {{user}}.', alternate_greetings=['', 'Hey {{user}}.'])
    assert greetings(meta, ctx()) == ['Hi Alex.', 'Hey Alex.']


####################
# Character metadata
####################


def test_from_card_lifts_sillytavern_depth_prompt_and_round_trips():
    card = {
        'spec': 'chara_card_v2',
        'data': {
            'name': 'Sera',
            'extensions': {'depth_prompt': {'prompt': 'Note.', 'depth': 2, 'role': 'user'}, 'other': 1},
        },
    }
    parsed = parse_card_bytes(json.dumps(card).encode())
    meta = CharacterMeta.from_card(parsed.character)
    assert meta.depth_prompt == DepthPrompt(text='Note.', depth=2, role='user')

    meta.depth_prompt.text = 'Edited.'
    exported = meta.to_card()
    assert exported.extensions == {'depth_prompt': {'prompt': 'Edited.', 'depth': 2, 'role': 'user'}, 'other': 1}


def test_character_meta_is_lenient():
    meta = CharacterMeta(prompt_overrides=None, depth_prompt=None, unknown='kept')
    assert meta.name == ''
    assert meta.prompt_overrides.system_prompt == 'replace'
    assert meta.model_dump()['unknown'] == 'kept'


def test_character_summary_hides_prompt_text():
    summary = character_summary(
        {'name': 'S', 'description': 'secret', 'system_prompt': 'secret', 'first_mes': 'Hi', 'creator_notes': 'n'}
    )
    assert summary == {'name': 'S', 'creator_notes': 'n', 'greeting_count': 1, 'expressions': []}


####################
# End to end: imported V2 PNG to provider message list
####################


def test_imported_v2_png_message_list_snapshot(png_factory):
    card = {
        'spec': 'chara_card_v2',
        'spec_version': '2.0',
        'data': {
            'name': 'Seraphina',
            'description': '{{char}} is a forest guardian.',
            'personality': 'gentle',
            'scenario': '{{user}} is lost in the woods.',
            'first_mes': 'You are safe now, {{user}}.',
            'mes_example': '<START>\n{{user}}: Who are you?\n{{char}}: Seraphina.',
            'system_prompt': '{{original}}\nNever break character.',
            'post_history_instructions': 'Reply in under 100 words.',
            'alternate_greetings': [],
            'tags': [],
            'creator': '',
            'character_version': '',
            'creator_notes': '',
            'extensions': {'depth_prompt': {'prompt': '{{char}} senses danger.', 'depth': 1, 'role': 'system'}},
        },
    }
    png = png_factory(text_chunks={'chara': base64.b64encode(json.dumps(card).encode()).decode()})
    meta = CharacterMeta.from_card(parse_card_bytes(png).character)

    context = build_macro_context(
        meta,
        model_name='seraphina-1234',
        user_name='Alex',
        original="Write {{char}}'s next reply.",
        chat_id='c1',
    )
    messages = [
        {'role': 'system', 'content': 'user settings prompt (replaced)'},
        {'role': 'assistant', 'content': 'You are safe now, Alex.'},
        {'role': 'user', 'content': 'Where am I?'},
        {'role': 'assistant', 'content': 'In my glade.'},
        {'role': 'user', 'content': 'Thank you.'},
    ]
    messages = apply_character_system_prompt(messages, meta, context)
    messages = apply_character_history(messages, meta, context)

    assert messages == [
        {
            'role': 'system',
            'content': (
                "Write Seraphina's next reply.\nNever break character.\n\n"
                'Seraphina is a forest guardian.\n\n'
                "Seraphina's personality: gentle\n\n"
                'Scenario: Alex is lost in the woods.\n\n'
                '### New example chat\nAlex: Who are you?\nSeraphina: Seraphina.'
            ),
        },
        {'role': 'assistant', 'content': 'You are safe now, Alex.'},
        {'role': 'user', 'content': 'Where am I?'},
        {'role': 'assistant', 'content': 'In my glade.'},
        {'role': 'user', 'content': 'Seraphina senses danger.\n\nThank you.\n\nReply in under 100 words.'},
    ]


@pytest.mark.parametrize(
    ('text', 'ooc'),
    [
        ('(OOC: can we skip ahead?)', True),
        ('((brb))', True),
        ('ooc: pause', True),
        ('[OOC] quick question', True),
        ('(She smiles.)', False),
        ('The book was OOC: out of copies', False),
    ],
)
def test_ooc_prefixes(text, ooc):
    from open_webui.utils.roleplay.filter import is_ooc_text

    assert is_ooc_text(text) is ooc
