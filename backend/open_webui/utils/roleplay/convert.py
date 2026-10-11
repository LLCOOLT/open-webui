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
}
LABEL_TO_FIELD = {label: key for key, labels in LABELS.items() for label in labels}

WRAPPERS = (('**', '**'), ('__', '__'), ('[', ']'), ('<', '>'))
CLOSING_TAG_RE = re.compile(r'^\s*</\s*[A-Za-z][\w -]*>\s*$')
TRAILING_CLOSING_TAG_RE = re.compile(r'\s*</\s*[A-Za-z][\w -]*>\s*$')
YOU_ARE_RE = re.compile(
    r"^\s*(?i:you are|you're|you will play|you play|you will be)\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2})"
)
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
    rest = rest.strip().lstrip(':').strip().strip('*_').strip()
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
    match = YOU_ARE_RE.match(preamble or sections.get('description', ''))
    if match:
        return match.group(1).strip()
    return fallback


def _with_macros(text: str, name: str) -> str:
    text = re.sub(r'\{\{\s*USER_NAME\s*\}\}', '{{user}}', text)
    if name:
        text = re.sub(rf'(?<![\w{{]){re.escape(name)}(?![\w}}])', '{{char}}', text)
    return text


def _example_dialogue(text: str, name: str) -> str:
    speaker_char = rf'(?:{CHAR_SPEAKERS}|{re.escape(name)})' if name else CHAR_SPEAKERS
    lines = []
    for line in text.split('\n'):
        line = re.sub(rf'^\s*\**{USER_SPEAKERS}\**\s*:\s*', '{{user}}: ', line, flags=re.IGNORECASE)
        line = re.sub(rf'^\s*\**{speaker_char}\**\s*:\s*', '{{char}}: ', line, flags=re.IGNORECASE)
        lines.append(line)
    text = '\n'.join(lines).strip()
    return text if not text or text.upper().startswith('<START>') else f'<START>\n{text}'


def parse_system_prompt(system: str, model_name: str = '') -> Conversion:
    """Split a system prompt into character fields. Every line of the input ends up in some field."""
    preamble, sections = _sections(system or '')
    name = _find_name(preamble, sections, model_name)

    character: dict = {key: '' for key in TEXT_FIELDS}
    character.update({key: value for key, value in sections.items() if key != 'name'})
    if preamble:
        character['description'] = '\n\n'.join(part for part in (preamble, character['description']) if part)
    if character['first_mes']:
        character['first_mes'] = character['first_mes'].strip().strip('"“”')
    if character['mes_example']:
        character['mes_example'] = _example_dialogue(character['mes_example'], name)

    for key in TEXT_FIELDS:
        if key not in ('name', 'creator_notes'):
            character[key] = _with_macros(character[key], name)
    character['name'] = name

    detected = sorted(sections)
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


def character_from_ai_reply(reply: str, model_name: str = '') -> Conversion:
    """Parse the model's JSON reply. Raises ``ValueError`` when it is not usable."""
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
    if character['system_prompt']:
        character['prompt_overrides'] = {'system_prompt': 'supplement', 'post_history': 'replace'}
    detected = [key for key in TEXT_FIELDS if key != 'name' and character[key]]
    return Conversion(character=character, detected=detected)
