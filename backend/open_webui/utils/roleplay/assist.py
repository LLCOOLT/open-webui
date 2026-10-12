"""Roleplay helpers that ask a model for something other than the next reply.

- Impersonate: write ``{{user}}``'s next message in the user's own voice.
- Scene image: turn the current scene into a prompt for the image generator.
- Expression sprites: prompts for drawing a character with a given expression.
- Lorebook suggestions: lasting facts from the story memory, as lorebook entries to approve.
"""

from __future__ import annotations

import re

from open_webui.utils.roleplay.character import CharacterMeta
from open_webui.utils.roleplay.cleanup import cut_user_lines
from open_webui.utils.roleplay.prompt import MacroContext, expand_macros
from open_webui.utils.roleplay.story import parse_json_object

TRANSCRIPT_CHARS = 12_000
SCENE_TRANSCRIPT_CHARS = 5_000
FIELD_CHARS = 2_000
MAX_SUGGESTIONS = 8

THINK_RE = re.compile(r'<think(?:ing)?>.*?</think(?:ing)?>', re.DOTALL | re.IGNORECASE)
DETAILS_RE = re.compile(r'<details\b[^>]*>.*?</details>', re.DOTALL | re.IGNORECASE)


def message_text(message: dict) -> str:
    """The visible text of a chat message (string or multimodal content), without reasoning blocks."""
    content = message.get('content') if isinstance(message, dict) else ''
    if isinstance(content, list):
        content = '\n'.join(
            part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text'
        )
    if not isinstance(content, str):
        return ''
    return DETAILS_RE.sub('', THINK_RE.sub('', content)).strip()


def transcript(messages: list[dict], char: str, user: str, max_chars: int = TRANSCRIPT_CHARS) -> str:
    """The most recent messages as ``Name: text`` lines, newest kept when it is too long."""
    lines: list[str] = []
    total = 0
    for message in reversed(messages or []):
        role = message.get('role') if isinstance(message, dict) else None
        if role not in ('user', 'assistant'):
            continue
        text = message_text(message)
        if not text:
            continue
        line = f'{user if role == "user" else char}: {text}'
        if total + len(line) > max_chars:
            if not lines:
                lines.append(f'{user if role == "user" else char}: …{text[-max_chars:]}')
            break
        lines.append(line)
        total += len(line) + 2
    return '\n\n'.join(reversed(lines))


def _field(character: CharacterMeta, ctx: MacroContext, name: str) -> str:
    value = getattr(character, name, '') or ''
    return expand_macros(value, ctx).strip()[:FIELD_CHARS]


def character_brief(character: CharacterMeta, ctx: MacroContext) -> str:
    parts = [
        _field(character, ctx, 'description'),
        _field(character, ctx, 'personality'),
    ]
    return '\n'.join(part for part in parts if part) or '(no description)'


####################
# Impersonate
####################

IMPERSONATE_PROMPT = """You are writing the next message for {{user}} in a roleplay between {{user}} and {{char}}.

<{{char}}>
{{CHAR_DESCRIPTION}}
</{{char}}>

<{{user}}>
{{PERSONA}}
</{{user}}>
{{SCENARIO}}
<chat>
{{TRANSCRIPT}}
</chat>

Write {{user}}'s next message, continuing the scene from the last message.
- Write only as {{user}}: their words, actions and thoughts. Never write what {{char}} says or does.
- Match how {{user}} writes in the chat: the same person (first, second or third), tense, length,
  formatting (for example *actions* and "speech") and tone.
- Keep it to one message, with no name prefix, no notes and no explanation.{{DRAFT}}"""


def impersonate_prompt(character: CharacterMeta, ctx: MacroContext, messages: list[dict], draft: str = '') -> str:
    scenario = _field(character, ctx, 'scenario')
    draft = draft.strip()
    replacements = {
        '{{CHAR_DESCRIPTION}}': character_brief(character, ctx),
        '{{PERSONA}}': ctx.persona.strip()[:FIELD_CHARS] or f'{ctx.user}, the user.',
        '{{SCENARIO}}': f'\n<scenario>\n{scenario}\n</scenario>\n' if scenario else '',
        '{{TRANSCRIPT}}': transcript(messages, ctx.char, ctx.user) or '(The chat has not started yet.)',
        '{{DRAFT}}': (
            "\n- Build the message from this idea of {{user}}'s, keeping its intent:"
            f'\n<idea>\n{draft[:FIELD_CHARS]}\n</idea>'
            if draft
            else ''
        ),
    }
    prompt = IMPERSONATE_PROMPT
    for key, value in replacements.items():
        prompt = prompt.replace(key, value)
    return prompt.replace('{{char}}', ctx.char or 'the character').replace('{{user}}', ctx.user or 'the user')


def clean_impersonation(reply: str, ctx: MacroContext) -> str:
    """The message text: no reasoning, no ``{{user}}:`` prefix, nothing written as ``{{char}}``."""
    text = THINK_RE.sub('', reply or '').strip()
    # Same cut as reply cleanup with the roles swapped: drop "User:" up front, stop at "Char:" lines.
    text = cut_user_lines(text, ctx.user, ctx.char)
    return text.strip()


####################
# Scene images and sprites
####################

SCENE_PROMPT = """Write a prompt for an image generator that illustrates the current moment of this roleplay.

<{{char}}>
{{CHAR_DESCRIPTION}}
</{{char}}>

<{{user}}>
{{PERSONA}}
</{{user}}>

<chat>
{{TRANSCRIPT}}
</chat>

Describe what a single picture of the latest moment would show: who is in it, their appearance
(hair, eyes, build, clothing) taken from the descriptions above, their poses and expressions, the
place, the lighting and the mood.
- Describe people by their looks, not by name; image generators do not know these characters.
- Leave out anyone who is not in the scene. If {{user}} has no described looks, keep them out of
  view or show them from behind.
- Write one paragraph of comma-separated visual details, under 80 words, then the art style.{{STYLE}}
- Reply with only the prompt."""

SPRITE_EDIT_PROMPT = (
    'The same character as in this image, with the same face, hair, outfit and art style, showing a clear '
    '{{EXPRESSION}} expression. Keep the framing and a plain background.'
)

SPRITE_PROMPT = """Write a prompt for an image generator: a portrait of {{char}}
showing a clear {{EXPRESSION}} expression.

<{{char}}>
{{CHAR_DESCRIPTION}}
</{{char}}>

Describe the character's looks (face, hair, eyes, build, clothing) from the description, the
{{EXPRESSION}} expression and body language, upper body, plain background. Do not use the name.
One paragraph of comma-separated visual details, under 70 words, then the art style.{{STYLE}}
Reply with only the prompt."""


def _style_line(style: str) -> str:
    style = style.strip()[:300]
    return f'\n- Art style: {style}.' if style else ''


def scene_prompt(character: CharacterMeta, ctx: MacroContext, messages: list[dict], style: str = '') -> str:
    prompt = (
        SCENE_PROMPT.replace('{{CHAR_DESCRIPTION}}', character_brief(character, ctx))
        .replace('{{PERSONA}}', ctx.persona.strip()[:FIELD_CHARS] or '(no description)')
        .replace('{{TRANSCRIPT}}', transcript(messages, ctx.char, ctx.user, SCENE_TRANSCRIPT_CHARS) or '(none)')
        .replace('{{STYLE}}', _style_line(style))
    )
    return prompt.replace('{{char}}', ctx.char or 'the character').replace('{{user}}', ctx.user or 'the user')


def sprite_prompt(character: CharacterMeta, ctx: MacroContext, expression: str, style: str = '') -> str:
    prompt = (
        SPRITE_PROMPT.replace('{{CHAR_DESCRIPTION}}', character_brief(character, ctx))
        .replace('{{EXPRESSION}}', expression)
        .replace('{{STYLE}}', f'\nArt style: {style.strip()[:300]}.' if style.strip() else '')
    )
    return prompt.replace('{{char}}', ctx.char or 'the character')


def sprite_edit_prompt(expression: str) -> str:
    return SPRITE_EDIT_PROMPT.replace('{{EXPRESSION}}', expression)


def clean_image_prompt(reply: str) -> str:
    text = THINK_RE.sub('', reply or '').strip()
    text = re.sub(r'^(?:image\s+)?prompt\s*:\s*', '', text, flags=re.IGNORECASE)
    return text.strip().strip('"').strip()[:2_000]


####################
# Lorebook suggestions
####################

LORE_PROMPT = """You maintain the lorebook (world info) for a roleplay between {{char}} and {{user}}.
A lorebook entry is inserted into the prompt when one of its keywords comes up, so the story stays
consistent about people, places, objects and facts.

<story_memory>
{{MEMORY}}
</story_memory>

<existing_entries>
{{EXISTING}}
</existing_entries>

Suggest new entries for lasting facts in the story memory that the existing entries do not cover:
named people met in the story (not {{char}} or {{user}} themselves), places, factions, important
objects, promises, secrets and rules of the world. Skip passing moments and anything already covered.

Reply with only a JSON object:
{"entries": [{"name": "", "keys": [""], "content": ""}]}
- name: what the entry is about. keys: 1 to 4 words or names that would come up when it matters.
- content: 1 to 3 sentences of facts, in the third person, using names.
- At most {{MAX}} entries; an empty list if there is nothing new."""


def lore_prompt(memory: str, existing: list[dict], ctx: MacroContext) -> str:
    lines = []
    for entry in existing[:200]:
        keys = ', '.join(entry.get('keys') or [])
        label = entry.get('name') or entry.get('comment') or keys
        lines.append(f'- {label} [{keys}]: {(entry.get("content") or "")[:120]}')
    prompt = (
        LORE_PROMPT.replace('{{MEMORY}}', memory.strip()[:20_000])
        .replace('{{EXISTING}}', '\n'.join(lines) or '(none)')
        .replace('{{MAX}}', str(MAX_SUGGESTIONS))
    )
    return prompt.replace('{{char}}', ctx.char or 'the character').replace('{{user}}', ctx.user or 'the user')


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def parse_lore_suggestions(reply: str, existing: list[dict], ctx: MacroContext) -> list[dict]:
    """Entries from the model reply, dropping ones whose keys or name an existing entry already uses."""
    try:
        entries = parse_json_object(reply).get('entries')
    except ValueError:
        return []
    taken = {
        _norm(value)
        for entry in existing
        for value in [*(entry.get('keys') or []), entry.get('name') or '', entry.get('comment') or '']
        if isinstance(value, str) and value.strip()
    }
    taken.discard('')
    excluded = {_norm(ctx.char), _norm(ctx.user)}
    suggestions: list[dict] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('name') or '').strip()[:120]
        content = str(entry.get('content') or '').strip()[:1_500]
        raw_keys = entry.get('keys')
        keys = [str(key).strip()[:60] for key in raw_keys if str(key).strip()] if isinstance(raw_keys, list) else []
        keys = list(dict.fromkeys(keys))[:6] or ([name] if name else [])
        if not content or not keys:
            continue
        normalized = {_norm(key) for key in keys} | {_norm(name)}
        if normalized & taken or {_norm(name)} & excluded:
            continue
        taken |= normalized
        suggestions.append({'name': name or keys[0], 'keys': keys, 'content': content})
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return suggestions


####################
# Opening from memory
####################

OPENING_PROMPT = """Write {{char}}'s first message for a new conversation with {{user}}.

<{{char}}>
{{CHAR_DESCRIPTION}}
</{{char}}>

<{{user}}>
{{PERSONA}}
</{{user}}>
{{SCENARIO}}
<memory>
{{MEMORY}}
</memory>
{{EXAMPLES}}
- {{memory_rule}}
- Write only as {{char}}: their words, actions and thoughts. Never write what {{user}} says or does.
- Match {{char}}'s voice and the formatting of the example openings (for example *actions* and "speech").
- One message, about as long as the example openings, with no name prefix and no notes."""

MEMORY_RULE = (
    '{{char}} remembers {{user}}: pick up naturally from what {{char}} remembers, as when two people '
    'meet again (a callback to something shared, how things stand between them now). Do not list '
    'the memories or retell them.'
)
NO_MEMORY_RULE = '{{char}} and {{user}} have not talked before; open a fresh scene that invites {{user}} in.'


def opening_prompt(character: CharacterMeta, ctx: MacroContext, memory: list[dict], examples: list[str]) -> str:
    scenario = _field(character, ctx, 'scenario')
    lines = '\n'.join(f'- {entry["content"]}' for entry in memory if entry.get('content'))
    shown = [text[:1_500] for text in examples[:3]]
    replacements = {
        '{{CHAR_DESCRIPTION}}': character_brief(character, ctx),
        '{{PERSONA}}': ctx.persona.strip()[:FIELD_CHARS] or f'{ctx.user}, the user.',
        '{{SCENARIO}}': f'\n<scenario>\n{scenario}\n</scenario>\n' if scenario else '',
        '{{MEMORY}}': lines or '(nothing: this is their first conversation)',
        '{{EXAMPLES}}': (
            '\n<example_openings>\n' + '\n\n---\n\n'.join(shown) + '\n</example_openings>\n' if shown else ''
        ),
        '{{memory_rule}}': MEMORY_RULE if lines else NO_MEMORY_RULE,
    }
    prompt = OPENING_PROMPT
    for key, value in replacements.items():
        prompt = prompt.replace(key, value)
    return prompt.replace('{{char}}', ctx.char or 'the character').replace('{{user}}', ctx.user or 'the user')


def clean_opening(reply: str, ctx: MacroContext) -> str:
    """The message text: no reasoning, no ``{{char}}:`` prefix, nothing written as ``{{user}}``."""
    return cut_user_lines(THINK_RE.sub('', reply or '').strip(), ctx.char, ctx.user).strip()
