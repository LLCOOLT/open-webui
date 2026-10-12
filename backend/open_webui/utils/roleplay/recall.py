"""Character memory across chats: what a character remembers about a user.

Separate from the story memory (``memory.py``), which summarizes one chat for itself. Recall keeps a
short list of facts a character would still know about a user the next time they meet, in any chat:
what the user shared about themselves, what happened between them, how the character feels about
them. It is stored per user and character (``models.character_memories``) and rendered into the
character's system block, so one person's facts never reach another user of the same character.

Updates are asked of the task model: given the current entries and what happened in the chat (the
story memory plus the messages since the last update), it returns the complete updated list.
"""

from __future__ import annotations

import re

from open_webui.models.character_memories import MAX_ENTRIES, normalize_entries
from open_webui.models.config import Config
from open_webui.utils.roleplay.assist import transcript
from open_webui.utils.roleplay.story import parse_json_object

# An automatic update runs once this many new messages are in the chat since the last one.
AUTO_INTERVAL = 10
SOURCE_CHARS = 12_000

DEFAULT_RECALL_PROMPT = """### Task:
You keep {{char}}'s long-term memory of {{user}}: what {{char}} would still know about {{user}} the
next time they meet, in a new conversation. Update that memory from what happened in this chat.

### Keep:
- Facts {{user}} shared about themselves: name, home, work, family, likes and dislikes, fears, past.
- What happened between {{char}} and {{user}} that would matter later: promises, gifts, fights,
  shared moments, nicknames, running jokes.
- How {{char}} feels about {{user}} now, and how that changed.

### Leave out:
- Facts about the world or other characters; lorebooks keep those.
- Scene details that stop mattering once the scene ends.

### Reply with JSON only:
{"entries": ["..."]}
- One short sentence per entry, in the third person, naming {{user}} and {{char}}.
- Return the complete updated list: every entry of the Current Memory that still holds, rewritten
  where it changed, plus the new ones. At most {{MAX}} entries; drop the least important first.
- Keep the Current Memory as it is when nothing new is worth remembering.

### Current Memory:
{{MEMORY}}

### What happened in this chat:
{{SOURCE}}"""


async def recall_available() -> bool:
    """Recall is on for this server (characters on, recall on)."""
    return bool(await Config.get('characters.enable')) and bool(await Config.get('characters.recall.enable'))


def recall_enabled_for_chat(chat_meta: dict | None) -> bool:
    """Per-chat switch; on unless the user turned it off for this chat."""
    return (chat_meta or {}).get('recall_enabled') is not False


async def recall_template() -> str:
    return (await Config.get('characters.recall.prompt') or '').strip() or DEFAULT_RECALL_PROMPT


def recall_source(summary: str | None, messages: list[dict], char: str, user: str) -> str:
    """What the update is drawn from: the story memory, then the messages since the last update."""
    parts = []
    if summary and summary.strip():
        parts.append('Story memory:\n' + summary.strip()[:SOURCE_CHARS])
    lines = transcript(messages, char, user, max_chars=SOURCE_CHARS)
    if lines:
        parts.append('Recent messages:\n' + lines)
    return '\n\n'.join(parts)


def build_recall_prompt(template: str, entries: list[dict], source: str, char: str, user: str) -> str:
    memory = '\n'.join(f'- {entry["content"]}' for entry in entries) or '(nothing yet)'
    prompt = template.replace('{{MEMORY}}', memory).replace('{{SOURCE}}', source).replace('{{MAX}}', str(MAX_ENTRIES))
    return prompt.replace('{{char}}', char or 'the character').replace('{{user}}', user or 'the user')


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def parse_recall_reply(reply: str, existing: list[dict], chat_id: str | None, now: int | None = None) -> list[dict]:
    """The updated entries from the model reply, or ``existing`` unchanged when it gives none.

    Entries that match an existing one keep its id and times; others are new, from ``chat_id``.
    Raises ``ValueError`` when the reply is not the expected JSON.
    """
    value = parse_json_object(reply).get('entries')
    if not isinstance(value, list):
        raise ValueError('The model did not return a list of entries.')
    by_content = {_norm(entry['content']): entry for entry in existing}
    merged: list = []
    for item in value:
        if isinstance(item, dict):
            item = item.get('content')
        if not isinstance(item, str) or not item.strip():
            continue
        known = by_content.get(_norm(item))
        merged.append(known if known is not None else item)
    updated = normalize_entries(merged, chat_id=chat_id, now=now)
    # An empty answer never wipes the memory; the user clears it on purpose from the chat controls.
    return updated if updated else existing


def recall_block(entries: list[dict], char: str, user: str) -> str:
    """The system block part that carries the memory, empty without entries."""
    if not entries:
        return ''
    lines = '\n'.join(f'- {entry["content"]}' for entry in entries)
    return f'What {char or "the character"} remembers about {user or "the user"} from earlier conversations:\n{lines}'


def messages_since(ids: list[str], checkpoint_id: str | None) -> list[str]:
    """Branch message ids after the last recall checkpoint; all of them when it is not on the branch."""
    if checkpoint_id and checkpoint_id in ids:
        return ids[ids.index(checkpoint_id) + 1 :]
    return list(ids)
