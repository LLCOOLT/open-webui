"""Long-chat memory for character chats.

Built on context compaction: once a character chat passes the memory threshold, the older messages
are summarized into a story memory (events, current scene, relationships, facts, open threads) that
is stored as the chat's compaction checkpoint and sent in place of those messages. The roleplay
settings replace compaction's (generic, task-oriented) prompt and threshold for these chats only, so
memory works whether or not compaction is enabled for everything else.
"""

from __future__ import annotations

from open_webui.models.config import Config

MEMORY_LABEL = '[STORY MEMORY]'

DEFAULT_MEMORY_PROMPT = """### Task:
You keep the memory for a long roleplay between {{char}} and {{user}}. Older messages are about to be
removed from the chat; rewrite the story memory so it covers the Previous Memory plus those messages.

### Write these sections, in this order:
Story so far: the important events, in order, briefly.
Current situation: where and when the scene is, who is present and what is happening right now.
Relationships: how {{char}} and the other characters feel about {{user}} and each other now, and
how that changed.
Facts to keep: names, places, promises, secrets, injuries, possessions and anything else
established that must stay consistent.
Open threads: unresolved goals, mysteries and plans.

### Rules:
- USER lines are {{user}}; ASSISTANT lines are {{char}}. Use names, never "user" or "assistant".
- Keep every fact from the Previous Memory unless the story has since changed it.
- Be specific and factual. Do not invent anything. Describe events in the past tense.
- Stay under 600 words.

### Previous Memory:
{{PREVIOUS_SUMMARY}}

### Messages Being Removed From The Chat:
{{COMPACTED_MESSAGES}}

### Recent Messages (still in the chat, for context only):
{{RECENT_MESSAGES}}"""


def memory_enabled_for_chat(chat_meta: dict | None) -> bool:
    """Per-chat switch; on unless the user turned it off for this chat."""
    return (chat_meta or {}).get('memory_enabled') is not False


async def memory_prompt(char: str, user: str) -> str:
    template = (await Config.get('characters.memory.prompt') or '').strip() or DEFAULT_MEMORY_PROMPT
    # Names are filled in here; compaction fills the message and summary placeholders.
    return template.replace('{{char}}', char or 'the character').replace('{{user}}', user or 'the user')


async def memory_overrides(char: str, user: str, chat_meta: dict | None) -> dict | None:
    """Compaction settings for a character chat, or ``None`` when roleplay memory does not apply."""
    if not await Config.get('characters.memory.enable'):
        return None
    if not memory_enabled_for_chat(chat_meta):
        # The user turned memory off for this chat: no summarizing at all, not even generic compaction.
        return {'enable': False}
    try:
        threshold = max(1000, int(await Config.get('characters.memory.token_threshold') or 12000))
    except (TypeError, ValueError):
        threshold = 12000
    return {
        'enable': True,
        'token_threshold': threshold,
        'token_cap': threshold,
        'prompt_template': await memory_prompt(char, user),
        'label': MEMORY_LABEL,
    }


def branch_message_ids(messages_map: dict, current_id: str | None) -> list[str]:
    """Message ids from the root to ``current_id`` (map keys, which stored messages may not repeat)."""
    ids: list[str] = []
    seen: set[str] = set()
    while current_id and current_id in messages_map and current_id not in seen:
        seen.add(current_id)
        ids.append(current_id)
        current_id = (messages_map[current_id] or {}).get('parentId')
    ids.reverse()
    return ids


def latest_memory(messages_map: dict, current_id: str | None) -> dict:
    """The story memory in effect on the current branch: the latest compaction checkpoint."""
    ids = branch_message_ids(messages_map, current_id)
    for index in range(len(ids) - 1, -1, -1):
        message = messages_map[ids[index]] or {}
        value = message.get('contextSummary') or message.get('context_summary')
        if isinstance(value, str) and value.strip():
            return {
                'summary': value,
                'message_id': ids[index],
                'summarized_messages': index,
                'total_messages': len(ids),
            }
    return {'summary': None, 'message_id': None, 'summarized_messages': 0, 'total_messages': len(ids)}
