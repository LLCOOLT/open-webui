"""Turn an existing system prompt into character card fields.

``parse_system_prompt`` is deterministic: it recognizes labelled sections (``Personality:``,
``## Scenario``, ``**First message:**``, ``[Example dialogue]``, ``<scenario>`` ...), the
character's name, and example dialogue, and rewrites the name as ``{{char}}``. Unlabelled text
becomes the description, so nothing is dropped. Prose prompts with no labels are better served by
the model-assisted path (``AI_CONVERSION_PROMPT`` + ``character_from_ai_reply``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

TEXT_FIELDS = (
    'name',
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'system_prompt',
    'post_history_instructions',
    'creator_notes',
)

# Section labels people use, normalized to lower case without punctuation, mapped to card fields.
LABELS = {
    'name': ('name', 'character name', 'char name', 'character'),
    'description': (
        'description',
        'appearance',
        'background',
        'backstory',
        'bio',
        'biography',
        'persona',
        'profile',
        'about',
        'identity',
        'character description',
        'who you are',
    ),
    'personality': ('personality', 'traits', 'personality traits', 'temperament', 'demeanor', 'demeanour'),
    'scenario': ('scenario', 'setting', 'situation', 'world', 'context', 'premise'),
    'first_mes': ('first message', 'first mes', 'greeting', 'opening message', 'opening', 'intro', 'first_mes'),
    'mes_example': (
        'example dialogue',
        'example dialogues',
        'example messages',
        'examples',
        'sample dialogue',
        'dialogue examples',
        'example conversation',
        'mes example',
        'mes_example',
    ),
    'system_prompt': (
        'instructions',
        'rules',
        'guidelines',
        'behavior',
        'behaviour',
        'writing style',
        'response style',
        'formatting',
        'system prompt',
    ),
    'post_history_instructions': ('post history instructions', 'post-history instructions', 'reminder', 'reminders'),
    'creator_notes': ('notes', 'creator notes', 'author notes', "author's notes"),
    'alternate_greetings': (
        'alternate greetings',
        'alternative greetings',
        'alternate greeting',
        'other greetings',
        'extra greetings',
        'more greetings',
        'alternate first messages',
        'alternate openings',
    ),
    'tags': ('tags', 'keywords', 'categories'),
}
LIST_LABEL_FIELDS = ('alternate_greetings', 'tags')
LIST_ITEM_RE = re.compile(r'^\s*(?:[-*•]|\d+[.)]|#\d+:?|greeting\s*\d+\s*:)\s*', re.IGNORECASE)
LABEL_TO_FIELD = {label: key for key, labels in LABELS.items() for label in labels}

WRAPPERS = (('**', '**'), ('__', '__'), ('[', ']'), ('<', '>'))
CLOSING_TAG_RE = re.compile(r'^\s*</\s*[A-Za-z][\w -]*>\s*$')
TRAILING_CLOSING_TAG_RE = re.compile(r'\s*</\s*[A-Za-z][\w -]*>\s*$')
YOU_ARE_RE = re.compile(
    r"^\s*(?i:you are|you're|you will play|you play|you will be)\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2})"
)
_PROPER_NAME = r"([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2})"
# "a pirate named Max", "Your name is Max", "I am Max", "Max is a ..." at the start: the name when
# the prompt does not open with "You are <Name>".
NAMED_RES = (
    re.compile(rf'\b(?i:named|called)\s+{_PROPER_NAME}'),
    re.compile(rf'\b(?i:your name is|you go by|my name is)\s+{_PROPER_NAME}'),
    re.compile(rf"^\s*(?i:i am|i'm)\s+{_PROPER_NAME}"),
)
NOT_NAMES = {'I', 'You', 'The', 'A', 'An', 'This', 'Your', 'My', 'AI', 'Assistant'}
USER_SPEAKERS = r'(?:\{\{user\}\}|user|you|human|me)'
CHAR_SPEAKERS = r'(?:\{\{char\}\}|assistant|ai|bot|character|model)'


@dataclass
class Conversion:
    character: dict
    detected: list[str] = field(default_factory=list)


def _normalize_label(label: str) -> str:
    return re.sub(r'\s+', ' ', label.replace('_', ' ').replace('’', "'")).strip().lower()


def _split_label(text: str) -> tuple[str, str, bool]:
    """``(label, rest, wrapped)`` for a line with leading ``#`` marks already removed."""
    for opener, closer in WRAPPERS:
        if text.startswith(opener):
            close = text.find(closer, len(opener))
            if close < 0:
                return '', '', False
            label, rest = text[len(opener) : close], text[close + len(closer) :]
            return label, rest, True
    label, colon, rest = text.partition(':')
    return (label + colon) if colon else text, rest if colon else '', False


def _header(line: str) -> tuple[str, str, bool] | None:
    """``(field, inline text, closed)`` when ``line`` labels a section, e.g. ``## Scenario``.

    ``closed`` is true for one-line XML sections such as ``<name>Kai</name>``.
    """
    text = line.strip()
    hashes = text.startswith('#')
    text = text.lstrip('#').strip()
    if not text:
        return None
    label, rest, wrapped = _split_label(text)
    # "[Personality: kind]" and "**Scenario:**" carry the colon inside the wrapper.
    label, colon, inner = label.partition(':')
    rest = f'{inner} {rest}' if inner.strip() else rest
    field_name = LABEL_TO_FIELD.get(_normalize_label(label))
    if field_name is None or len(label) > 40:
        return None
    bare = not rest.strip() and not colon
    if not (hashes or wrapped or colon or bare):
        return None
    # Only the remains of the label's own markup ("Scenario:** text"); a leading *action* stays.
    rest = re.sub(r'^[*_]+(?=\s|$)', '', rest.strip().lstrip(':').strip()).strip()
    closed = bool(TRAILING_CLOSING_TAG_RE.search(rest))
    return field_name, TRAILING_CLOSING_TAG_RE.sub('', rest), closed


def _sections(text: str) -> tuple[str, dict[str, list[str]]]:
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] = preamble
    for line in text.replace('\r\n', '\n').split('\n'):
        if CLOSING_TAG_RE.match(line):
            current = preamble
            continue
        header = _header(line)
        if header:
            field_name, rest, closed = header
            section = sections.setdefault(field_name, [])
            if section:
                section.append('')
            if rest:
                section.append(rest)
            current = preamble if closed else section
            continue
        current.append(line)
    return '\n'.join(preamble).strip(), {key: '\n'.join(lines).strip() for key, lines in sections.items()}


def _find_name(preamble: str, sections: dict[str, str], fallback: str) -> str:
    if sections.get('name'):
        return sections['name'].split('\n')[0].strip().strip('*"\'').strip()
    text = preamble or sections.get('description', '')
    match = YOU_ARE_RE.match(text)
    if match and match.group(1).split()[0] not in NOT_NAMES:
        return match.group(1).strip()
    for pattern in NAMED_RES:
        match = pattern.search(text)
        if match and match.group(1).split()[0] not in NOT_NAMES:
            return match.group(1).strip()
    return fallback


def _with_macros(text: str, name: str) -> str:
    text = re.sub(r'\{\{\s*USER_NAME\s*\}\}', '{{user}}', text)
    if name:
        text = re.sub(rf'(?<![\w{{]){re.escape(name)}(?![\w}}])', '{{char}}', text)
    return text


def _unquote(text: str) -> str:
    """Drop quotes wrapping the whole text; quotes inside it (after an *action*) stay."""
    text = text.strip()
    if len(text) > 1 and text[0] in '"“' and text[-1] in '"”' and not re.search(r'["“”]', text[1:-1]):
        return text[1:-1].strip()
    return text


def _example_dialogue(text: str, name: str) -> str:
    # "Captain Mara Voss" speaks as "Mara" too.
    names = [re.escape(part) for part in dict.fromkeys([name, *name.split()]) if len(part) > 1] if name else []
    speaker_char = rf'(?:{CHAR_SPEAKERS}|{"|".join(names)})' if names else CHAR_SPEAKERS
    lines = []
    for line in text.split('\n'):
        line = re.sub(rf'^\s*\**{USER_SPEAKERS}\**\s*:\s*', '{{user}}: ', line, flags=re.IGNORECASE)
        line = re.sub(rf'^\s*\**{speaker_char}\**\s*:\s*', '{{char}}: ', line, flags=re.IGNORECASE)
        lines.append(line)
    text = '\n'.join(lines).strip()
    return text if not text or text.upper().startswith('<START>') else f'<START>\n{text}'


def _greeting_list(text: str) -> list[str]:
    """Alternate greetings written as a list ("- ...", "1. ...") or as paragraphs."""
    lines = text.split('\n')
    if any(LIST_ITEM_RE.match(line) for line in lines):
        items: list[str] = []
        for line in lines:
            if LIST_ITEM_RE.match(line):
                items.append(LIST_ITEM_RE.sub('', line, count=1))
            elif items:
                items[-1] += '\n' + line
            elif line.strip():
                items.append(line)
    else:
        items = re.split(r'\n\s*\n', text)
    return [_unquote(item) for item in items if item.strip()]


def _tag_list(text: str) -> list[str]:
    tags = [LIST_ITEM_RE.sub('', tag).strip().strip('#').strip() for tag in re.split(r'[,\n]', text)]
    return [tag for tag in dict.fromkeys(tags) if tag]


def parse_system_prompt(system: str, model_name: str = '') -> Conversion:
    """Split a system prompt into character fields. Every line of the input ends up in some field."""
    preamble, sections = _sections(system or '')
    name = _find_name(preamble, sections, model_name)
    # Lines under "Name:" after the name itself ("Age: 25", "Gender: ...") describe the character.
    name_rest = '\n'.join(sections.get('name', '').split('\n')[1:]).strip()
    lists = {key: sections.pop(key) for key in LIST_LABEL_FIELDS if key in sections}

    character: dict = {key: '' for key in TEXT_FIELDS}
    character.update({key: value for key, value in sections.items() if key != 'name'})
    character['description'] = '\n\n'.join(part for part in (preamble, name_rest, character['description']) if part)
    if character['first_mes']:
        character['first_mes'] = _unquote(character['first_mes'])
    if character['mes_example']:
        character['mes_example'] = _example_dialogue(character['mes_example'], name)

    for key in TEXT_FIELDS:
        if key not in ('name', 'creator_notes'):
            character[key] = _with_macros(character[key], name)
    character['name'] = name
    if 'alternate_greetings' in lists:
        character['alternate_greetings'] = [
            _with_macros(greeting, name) for greeting in _greeting_list(lists['alternate_greetings'])
        ]
    if 'tags' in lists:
        character['tags'] = _tag_list(lists['tags'])

    detected = sorted({*sections, *lists})
    if character['system_prompt']:
        # The author's rules sit alongside the default roleplay instruction rather than replacing it.
        character['prompt_overrides'] = {'system_prompt': 'supplement', 'post_history': 'replace'}
    return Conversion(character=character, detected=detected)


####################
# Model-assisted conversion
####################

AI_CONVERSION_PROMPT = """Convert the system prompt below into a roleplay character card.

Reply with only a JSON object with exactly these keys, each a string ("" when the prompt has
nothing for it): name, description, personality, scenario, first_mes, mes_example, system_prompt,
post_history_instructions, creator_notes

Rules:
- Keep the author's wording. Do not invent details that are not in the prompt.
- Inside the text fields write {{char}} for the character's name and {{user}} for the person they talk to.
- description: who the character is (appearance, background, relationships).
- personality: a short summary of temperament and traits.
- scenario: the situation or setting the conversation starts in.
- first_mes: an opening message only if the prompt contains one.
- mes_example: only if the prompt contains example exchanges, as blocks that start with <START>
  followed by lines "{{user}}: ..." and "{{char}}: ...".
- system_prompt: rules for the model itself (format, style, things to always or never do).

<system_prompt>
{{PROMPT}}
</system_prompt>"""


def ai_conversion_prompt(system: str) -> str:
    return AI_CONVERSION_PROMPT.replace('{{PROMPT}}', system)


_WORD_RE = re.compile(r"[\w'’]+")
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|\n+')


def _words(text: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(text) if len(word) > 2}


def uncovered_sentences(source: str, character: dict, name: str = '') -> list[str]:
    """Sentences of ``source`` that the converted fields do not carry, so nothing can go missing.

    A sentence counts as carried when most of its words appear somewhere in the fields; this
    tolerates the light rewording (``{{char}}`` for the name, "you" to "{{char}}") the conversion
    asks for, while catching sentences that were dropped.
    """
    carried = _words(' '.join(str(character.get(key) or '') for key in TEXT_FIELDS))
    carried |= {'char', 'user'} | (_words(name) if name else set())
    missing = []
    for sentence in _SENTENCE_RE.split(source or ''):
        sentence = sentence.strip()
        words = _words(sentence) - {'you', 'your', 'are', 'the', 'and'}
        if len(words) < 3:
            continue
        if len(words & carried) / len(words) < 0.6:
            missing.append(sentence)
    return missing


def character_from_ai_reply(reply: str, model_name: str = '', source: str = '') -> Conversion:
    """Parse the model's JSON reply. Raises ``ValueError`` when it is not usable.

    With ``source`` (the prompt that was converted), sentences the model left out are appended to
    the character system prompt rather than lost.
    """
    start, end = reply.find('{'), reply.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('The model did not return a character.')
    try:
        data = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError('The model did not return valid JSON.') from error
    if not isinstance(data, dict):
        raise ValueError('The model did not return a character.')

    character = {key: data[key].strip() if isinstance(data.get(key), str) else '' for key in TEXT_FIELDS}
    character['name'] = character['name'] or model_name
    if character['mes_example']:
        character['mes_example'] = _example_dialogue(character['mes_example'], character['name'])
    for key in TEXT_FIELDS:
        if key not in ('name', 'creator_notes'):
            character[key] = _with_macros(character[key], character['name'])
    missing = uncovered_sentences(source, character, character['name'])
    if missing:
        kept = _with_macros(' '.join(missing), character['name'])
        character['system_prompt'] = '\n\n'.join(part for part in (character['system_prompt'], kept) if part)
    if character['system_prompt']:
        character['prompt_overrides'] = {'system_prompt': 'supplement', 'post_history': 'replace'}
    detected = [key for key in TEXT_FIELDS if key != 'name' and character[key]]
    return Conversion(character=character, detected=detected)


####################
# Pasted card JSON
####################

CARD_LIST_FIELDS = ('alternate_greetings', 'tags')


def card_from_json_text(text: str) -> Conversion | None:
    """A character card pasted as JSON (any format card import reads, in a code fence or not)."""
    from open_webui.utils.roleplay.cards import CardParseError, normalize_card

    start, end = (text or '').find('{'), (text or '').rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        card = normalize_card(data)
    except (json.JSONDecodeError, CardParseError, ValueError):
        return None
    character = card.model_dump(include={*TEXT_FIELDS, *CARD_LIST_FIELDS, 'character_book', 'creator'})
    detected = [
        key for key in (*TEXT_FIELDS, *CARD_LIST_FIELDS, 'character_book') if key != 'name' and character.get(key)
    ]
    return Conversion(character=character, detected=detected)
