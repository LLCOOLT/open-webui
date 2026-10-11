import pytest

from open_webui.utils.roleplay.convert import character_from_ai_reply, parse_system_prompt

LABELLED = """You are Seraphina, the guardian of an enchanted forest.

## Personality
Kind, protective and wise. Seraphina rarely raises her voice.

**Scenario:** {{USER_NAME}} wakes up in Seraphina's glade after collapsing.

First message: "You are safe now. Rest."

[Example dialogue]
User: Who are you?
Seraphina: I am Seraphina, keeper of this forest.

Rules:
- Always describe actions between asterisks.
- Never speak for the user.
"""


def test_labelled_prompt():
    result = parse_system_prompt(LABELLED, 'forest-model')
    character = result.character
    assert character['name'] == 'Seraphina'
    assert character['description'] == 'You are {{char}}, the guardian of an enchanted forest.'
    assert character['personality'] == 'Kind, protective and wise. {{char}} rarely raises her voice.'
    assert character['scenario'] == "{{user}} wakes up in {{char}}'s glade after collapsing."
    assert character['first_mes'] == 'You are safe now. Rest.'
    assert (
        character['mes_example'] == '<START>\n{{user}}: Who are you?\n{{char}}: I am {{char}}, keeper of this forest.'
    )
    assert character['system_prompt'].startswith('- Always describe actions')
    assert character['prompt_overrides']['system_prompt'] == 'supplement'
    assert result.detected == ['first_mes', 'mes_example', 'personality', 'scenario', 'system_prompt']


def test_every_line_is_kept():
    result = parse_system_prompt(LABELLED)
    kept = ' '.join(value for value in result.character.values() if isinstance(value, str))
    for word in ('guardian', 'protective', 'collapsing', 'safe', 'keeper', 'asterisks', 'user.'):
        assert word in kept


@pytest.mark.parametrize(
    ('line', 'field'),
    [
        ('# Scenario', 'scenario'),
        ('Scenario', 'scenario'),
        ('**Background**', 'description'),
        ('<personality>', 'personality'),
        ('Greeting:', 'first_mes'),
        ('POST-HISTORY INSTRUCTIONS:', 'post_history_instructions'),
    ],
)
def test_header_forms(line, field):
    result = parse_system_prompt(f'{line}\nSome text.\n')
    assert result.character[field] == 'Some text.'


def test_xml_sections_and_name_label():
    prompt = '<name>Kai</name>\n<personality>\nCalm.\n</personality>\nTrailing note about Kai.'
    character = parse_system_prompt(prompt).character
    assert character['personality'] == 'Calm.'
    # The closing tag ends the section; text after it is unlabelled description.
    assert character['description'].endswith('Trailing note about {{char}}.')


def test_ordinary_colons_are_not_headers():
    character = parse_system_prompt('Remember this: be brief.\nNote that: x').character
    assert character['description'] == 'Remember this: be brief.\nNote that: x'


def test_prose_prompt_becomes_description_with_model_name_fallback():
    character = parse_system_prompt('A helpful pirate who loves puns.', 'Captain').character
    assert character['name'] == 'Captain'
    assert character['description'] == 'A helpful pirate who loves puns.'
    assert 'prompt_overrides' not in character


def test_ai_reply_parsing():
    reply = (
        'Sure!\n```json\n{"name": "Kai", "description": "{{char}} is calm.", '
        '"mes_example": "User: hi\\nKai: hello", "system_prompt": "Be brief.", "extra": 1}\n```'
    )
    result = character_from_ai_reply(reply, 'fallback')
    assert result.character['name'] == 'Kai'
    assert result.character['mes_example'] == '<START>\n{{user}}: hi\n{{char}}: hello'
    assert result.character['prompt_overrides']['system_prompt'] == 'supplement'
    assert 'extra' not in result.character
    assert result.detected == ['description', 'mes_example', 'system_prompt']

    with pytest.raises(ValueError):
        character_from_ai_reply('no json here')
    with pytest.raises(ValueError):
        character_from_ai_reply('{not json}')
