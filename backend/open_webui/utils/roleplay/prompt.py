"""Prompt assembly for character models.

Pure functions only (messages in, messages out) so the behavior card authors rely on can be
tested without FastAPI. The final message list sent to the provider is::

    [system]            the character block (see ``system_prompt_parts``)
    [examples ...]      example dialogue, when ``example_mode == 'messages'``
    [history ...]       depth prompts merged into the user message ``depth`` messages from the
                        end, post-history instructions appended to the last user message
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field, replace

from open_webui.utils.roleplay.character import CharacterMeta, DepthPrompt
from open_webui.utils.roleplay.lorebook import Activation

SIMPLE_MACRO_RE = re.compile(r'\{\{\s*(char|user|persona|original)\s*\}\}', re.IGNORECASE)
ORIGINAL_MACRO_RE = re.compile(r'\{\{\s*original\s*\}\}', re.IGNORECASE)
# Function macros never contain braces, so nested simple macros are expanded first.
FUNCTION_MACRO_RE = re.compile(r'\{\{([^{}]*)\}\}')
EXAMPLE_SEPARATOR_RE = re.compile(r'<START>', re.IGNORECASE)
EXAMPLE_LINE_RE = re.compile(r'^\s*\{\{(user|char)\}\}\s*:\s?(.*)$', re.IGNORECASE)
UNESCAPED_COMMA_RE = re.compile(r'(?<!\\),')
MAX_ROLL = 1_000_000_000


@dataclass
class MacroContext:
    char: str
    user: str
    persona: str = ''
    original: str = ''
    chat_id: str = ''
    # ``None`` leaves random, pick and roll macros in place (used for the stored static prompt).
    rng: random.Random | None = field(default_factory=random.Random)


def build_macro_context(
    character: CharacterMeta,
    *,
    model_name: str,
    user_name: str,
    original: str = '',
    persona: str = '',
    chat_id: str = '',
) -> MacroContext:
    """Context for one request. ``original`` may itself use macros (the default prompt does)."""
    ctx = MacroContext(char=character.display_name(model_name), user=user_name, persona=persona, chat_id=chat_id)
    return replace(ctx, original=expand_macros(original, ctx).strip())


####################
# Macros
####################


def _split_options(argument: str) -> list[str]:
    if argument.startswith(':'):
        argument = argument[1:]
        if '::' in argument:
            return [option.strip() for option in argument.split('::')]
    return [option.replace('\\,', ',').strip() for option in UNESCAPED_COMMA_RE.split(argument)]


def _stable_index(seed: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode('utf-8')).digest()[:8], 'big') % size


def _roll(argument: str, rng: random.Random) -> str | None:
    sides = argument.strip().lower().removeprefix('d')
    if not sides.isdigit() or not 0 < int(sides) <= MAX_ROLL:
        return None
    return str(rng.randint(1, int(sides)))


def _function_macro(name: str, argument: str, ctx: MacroContext, occurrence: int) -> str | None:
    """Value for ``{{name:argument}}``, or ``None`` to leave the macro untouched."""
    if name in ('hidden_key', 'comment'):
        return ''
    if name == 'reverse':
        return argument[::-1]
    if name == 'random' and ctx.rng is not None:
        return ctx.rng.choice(_split_options(argument))
    if name == 'pick' and ctx.rng is not None:
        options = _split_options(argument)
        return options[_stable_index(f'{ctx.chat_id}:{argument}:{occurrence}', len(options))]
    if name == 'roll' and ctx.rng is not None:
        return _roll(argument, ctx.rng)
    return None


def expand_macros(text: str, ctx: MacroContext, allow_original: bool = False) -> str:
    """Expand card macros case-insensitively. Unknown macros such as ``{{USER_NAME}}`` are kept.

    ``{{original}}`` is only meaningful in the system prompt and post-history instructions; anywhere
    else it expands to nothing.
    """
    if not text:
        return ''

    simple = {
        'char': ctx.char,
        'user': ctx.user,
        'persona': ctx.persona,
        'original': ctx.original if allow_original else '',
    }
    text = SIMPLE_MACRO_RE.sub(lambda match: simple[match.group(1).lower()], text)

    occurrences: dict[str, int] = {}

    def expand(match: re.Match) -> str:
        body = match.group(1)
        if body.strip().startswith('//'):
            return ''
        name, separator, argument = body.partition(':')
        if not separator:
            return match.group(0)
        occurrence = occurrences.get(body, 0)
        occurrences[body] = occurrence + 1
        value = _function_macro(name.strip().lower(), argument, ctx, occurrence)
        return match.group(0) if value is None else value

    return FUNCTION_MACRO_RE.sub(expand, text)


####################
# System block
####################


def _system_prompt_part(character: CharacterMeta) -> str:
    override = character.system_prompt.strip()
    if not override:
        return '{{original}}'
    if character.prompt_overrides.system_prompt == 'supplement' and not ORIGINAL_MACRO_RE.search(override):
        return '{{original}}\n\n' + override
    return override


def split_examples(mes_example: str) -> list[str]:
    """Split example dialogue into ``<START>`` blocks."""
    return [block.strip() for block in EXAMPLE_SEPARATOR_RE.split(mes_example or '') if block.strip()]


def examples_as_text(character: CharacterMeta) -> str:
    return '\n\n'.join(f'### New example chat\n{block}' for block in split_examples(character.mes_example))


def system_prompt_parts(
    character: CharacterMeta,
    lore_before: list[str] | None = None,
    lore_after: list[str] | None = None,
    persona: str = '',
    recall: str = '',
) -> list[str]:
    """The unexpanded parts of the system block, in order. Only the first may use ``{{original}}``.

    ``recall`` is what the character remembers about this user from earlier chats; it follows the
    persona, which it is about.
    """
    return [
        _system_prompt_part(character),
        *(lore_before or []),
        character.description,
        "{{char}}'s personality: " + character.personality if character.personality.strip() else '',
        'Scenario: ' + character.scenario if character.scenario.strip() else '',
        *(lore_after or []),
        persona,
        recall,
        examples_as_text(character) if character.example_mode == 'system' else '',
    ]


def render_system_prompt(
    character: CharacterMeta,
    ctx: MacroContext,
    lore_before: list[str] | None = None,
    lore_after: list[str] | None = None,
    recall: str = '',
) -> str:
    parts = system_prompt_parts(character, lore_before, lore_after, ctx.persona, recall)
    rendered = [expand_macros(part, ctx, allow_original=index == 0).strip() for index, part in enumerate(parts)]
    return '\n\n'.join(part for part in rendered if part)


def compile_static_system_prompt(character: CharacterMeta, model_name: str = '') -> str:
    """The prompt stored in ``params.system`` for code paths that do not know about characters.

    ``{{user}}`` maps to the existing ``{{USER_NAME}}`` variable and ``{{original}}`` is empty;
    random, pick and roll macros stay as written.
    """
    ctx = MacroContext(char=character.display_name(model_name), user='{{USER_NAME}}', rng=None)
    return render_system_prompt(character, ctx)


####################
# Messages
####################


def example_messages(character: CharacterMeta, ctx: MacroContext) -> list[dict]:
    """Example dialogue as alternating messages. Lines without a speaker continue the previous one."""
    messages: list[dict] = []
    for block in split_examples(character.mes_example):
        current: dict | None = None
        for line in block.split('\n'):
            match = EXAMPLE_LINE_RE.match(line)
            if match:
                current = {'role': 'user' if match.group(1).lower() == 'user' else 'assistant', 'content': match[2]}
                messages.append(current)
            elif current is not None:
                current['content'] += '\n' + line
    for message in messages:
        message['content'] = expand_macros(message['content'].strip(), ctx)
    return messages


def post_history_text(character: CharacterMeta, ctx: MacroContext, original: str = '') -> str:
    override = character.post_history_instructions.strip()
    if not override:
        text = '{{original}}'
    elif character.prompt_overrides.post_history == 'supplement' and not ORIGINAL_MACRO_RE.search(override):
        text = '{{original}}\n\n' + override
    else:
        text = override
    return expand_macros(text, replace(ctx, original=original), allow_original=True).strip()


def _history_start(messages: list[dict]) -> int:
    return 1 if messages and messages[0].get('role') == 'system' else 0


def _with_text(message: dict, text: str, prepend: bool) -> dict:
    """A copy of ``message`` with ``text`` added before or after its text content."""
    content = message.get('content')
    if isinstance(content, list):
        parts = [dict(part) for part in content]
        for part in parts:
            if part.get('type') == 'text':
                part['text'] = f'{text}\n\n{part["text"]}' if prepend else f'{part["text"]}\n\n{text}'
                break
        else:
            parts.insert(0 if prepend else len(parts), {'type': 'text', 'text': text})
        return {**message, 'content': parts}
    content = content if isinstance(content, str) else ''
    if not content:
        return {**message, 'content': text}
    return {**message, 'content': f'{text}\n\n{content}' if prepend else f'{content}\n\n{text}'}


def _user_positions(history: list[dict]) -> list[int]:
    return [index for index, message in enumerate(history) if message.get('role') == 'user']


def inject_into_history(
    messages: list[dict],
    depth_prompts: list[DepthPrompt],
    post_history: str = '',
    start: int | None = None,
) -> list[dict]:
    """Place depth prompts and the post-history block into ``messages[start:]``.

    The chat pipeline folds every system message into the first one before the request leaves
    (``normalize_messages_for_model``), so injected text cannot travel as mid-conversation system
    messages. Instead:

    - a system or user depth prompt is prepended to the first user message at or after the point
      ``depth`` messages from the end (the last user message when none follows);
    - an assistant depth prompt is inserted there as its own message;
    - post-history instructions are appended to the last user message.

    Nothing is placed before ``start``. Messages are copied, not modified.
    """
    start = _history_start(messages) if start is None else start
    head, history = messages[:start], list(messages[start:])
    users = _user_positions(history)

    inserted: dict[int, list[dict]] = {}
    for prompt in depth_prompts:
        if not prompt.text:
            continue
        position = max(0, len(history) - prompt.depth)
        if prompt.role == 'assistant' or not users:
            role = 'assistant' if prompt.role == 'assistant' else 'user'
            inserted.setdefault(position, []).append({'role': role, 'content': prompt.text})
            continue
        following = [index for index in users if index >= position]
        if following:
            history[following[0]] = _with_text(history[following[0]], prompt.text, prepend=True)
        else:
            history[users[-1]] = _with_text(history[users[-1]], prompt.text, prepend=False)

    if post_history and users:
        history[users[-1]] = _with_text(history[users[-1]], post_history, prepend=False)

    result = list(head)
    for position in range(len(history) + 1):
        result.extend(inserted.get(position, []))
        if position < len(history):
            result.append(history[position])
    return result


def apply_character_system_prompt(
    messages: list[dict],
    character: CharacterMeta,
    ctx: MacroContext,
    lore: Activation | None = None,
    recall: str = '',
) -> list[dict]:
    """Replace the leading system message (already captured as ``ctx.original``) with the character block."""
    rest = messages[_history_start(messages) :]
    system = render_system_prompt(
        character, ctx, lore.before_char if lore else None, lore.after_char if lore else None, recall
    )
    return [{'role': 'system', 'content': system}, *rest] if system else rest


def render_ooc_system_prompt(character: CharacterMeta, ctx: MacroContext, ooc_prompt: str) -> str:
    """The system block for an out-of-character message: the OOC instruction, then the character
    block as reference so questions about the story can still be answered."""
    instruction = expand_macros(ooc_prompt, ctx).strip()
    reference = render_system_prompt(character, ctx)
    if not reference:
        return instruction
    return f'{instruction}\n\n---\nRoleplay context, for reference only:\n\n{reference}'.strip()


def lore_depth_prompts(lore: Activation | None) -> list[DepthPrompt]:
    """Activated ``@@depth`` entries as depth prompts."""
    return [DepthPrompt(text=entry.text, depth=entry.depth, role=entry.role) for entry in (lore.depth if lore else [])]


def apply_world_info(
    messages: list[dict],
    lore: Activation | None,
    ctx: MacroContext,
    extra_depth_prompts: list[DepthPrompt] | None = None,
) -> list[dict]:
    """Lorebooks and the author's note for a model that is not a character.

    Activated entries are appended to the system message (before-description entries first) and
    depth entries are placed in the history like a character's.
    """
    texts = [*(lore.before_char if lore else []), *(lore.after_char if lore else [])]
    world = '\n\n'.join(text for text in (expand_macros(text, ctx).strip() for text in texts) if text)
    if world:
        start = _history_start(messages)
        if start:
            system = messages[0].get('content')
            system = system if isinstance(system, str) else ''
            messages = [{**messages[0], 'content': f'{system}\n\n{world}' if system else world}, *messages[1:]]
        else:
            messages = [{'role': 'system', 'content': world}, *messages]
    depth_prompts = [
        DepthPrompt(text=expand_macros(prompt.text, ctx).strip(), depth=prompt.depth, role=prompt.role)
        for prompt in [*lore_depth_prompts(lore), *(extra_depth_prompts or [])]
    ]
    return inject_into_history(messages, depth_prompts)


def apply_character_history(
    messages: list[dict],
    character: CharacterMeta,
    ctx: MacroContext,
    extra_depth_prompts: list[DepthPrompt] | None = None,
) -> list[dict]:
    """Add example messages, depth prompts (character note, author's note) and post-history instructions."""
    start = _history_start(messages)
    examples = example_messages(character, ctx) if character.example_mode == 'messages' else []
    messages = [*messages[:start], *examples, *messages[start:]]

    depth_prompts = [
        DepthPrompt(text=expand_macros(prompt.text, ctx).strip(), depth=prompt.depth, role=prompt.role)
        for prompt in [character.depth_prompt, *(extra_depth_prompts or [])]
    ]
    return inject_into_history(messages, depth_prompts, post_history_text(character, ctx), start + len(examples))


def greetings(character: CharacterMeta, ctx: MacroContext) -> list[str]:
    """``[first_mes, *alternate_greetings]`` expanded, without empty entries."""
    texts = [character.first_mes, *character.alternate_greetings]
    return [expanded for text in texts if (expanded := expand_macros(text, ctx).strip())]
