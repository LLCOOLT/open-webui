"""Lorebooks (character book / world info): normalization, decorators and activation.

Entries follow the Character Card V3 superset of the V2 schema. ``activate`` selects the entries
that apply to a conversation using V2 semantics plus the V3 decorators listed in
``KNOWN_DECORATORS``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import regex

log = logging.getLogger(__name__)

DEFAULT_SCAN_DEPTH = 4
DEFAULT_INSERTION_ORDER = 100
DEFAULT_PRIORITY = 10
ENTRY_POSITIONS = ('before_char', 'after_char')

# Decorators this implementation understands. Anything else is ignored (see ``parse_decorators``).
KNOWN_DECORATORS = {
    'depth',
    'role',
    'position',
    'scan_depth',
    'activate',
    'dont_activate',
    'additional_keys',
    'exclude_keys',
}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(',') if part.strip()]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item != '']


def _int(value: Any, default: int | None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_entry(entry: Any, index: int) -> dict | None:
    """Normalize one V1/V2/V3 lorebook entry. Returns ``None`` for unusable input."""
    if not isinstance(entry, dict):
        return None

    position = entry.get('position')
    return {
        'id': _int(entry.get('id'), index),
        'name': _str(entry.get('name')),
        'comment': _str(entry.get('comment')),
        'enabled': entry.get('enabled') is not False,
        'keys': _str_list(entry.get('keys', entry.get('key'))),
        'secondary_keys': _str_list(entry.get('secondary_keys', entry.get('keysecondary'))),
        'selective': bool(entry.get('selective', False)),
        'use_regex': bool(entry.get('use_regex', False)),
        'case_sensitive': bool(entry.get('case_sensitive', False)),
        'constant': bool(entry.get('constant', False)),
        'insertion_order': _int(entry.get('insertion_order'), DEFAULT_INSERTION_ORDER),
        'priority': _int(entry.get('priority'), DEFAULT_PRIORITY),
        'position': position if position in ENTRY_POSITIONS else 'before_char',
        'content': _str(entry.get('content')),
        'extensions': _dict(entry.get('extensions')),
    }


def normalize_lorebook(book: Any) -> dict | None:
    """Normalize a ``character_book`` or ``lorebook_v3`` payload into the stored shape.

    Accepts either the bare book object or a ``{"spec": "lorebook_v3", "data": {...}}`` wrapper.
    SillyTavern world-info exports keep entries in a dict keyed by uid; those are accepted too.
    """
    if not isinstance(book, dict):
        return None
    if book.get('spec') == 'lorebook_v3' and isinstance(book.get('data'), dict):
        book = book['data']

    raw_entries = book.get('entries')
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    if not isinstance(raw_entries, list):
        raw_entries = []

    entries = [
        normalized
        for index, entry in enumerate(raw_entries)
        if (normalized := normalize_entry(entry, index)) is not None
    ]

    return {
        'name': _str(book.get('name')),
        'description': _str(book.get('description')),
        'scan_depth': _int(book.get('scan_depth'), None),
        'token_budget': _int(book.get('token_budget'), None),
        'recursive_scanning': bool(book.get('recursive_scanning', False)),
        'extensions': _dict(book.get('extensions')),
        'entries': entries,
    }


def _parse_decorator_line(line: str) -> tuple[str, str] | None:
    """Parse ``@@name value`` into ``(name, value)``; ``None`` if the line is not a decorator."""
    if not line.startswith('@@') or line.startswith('@@@'):
        return None
    name, _, value = line[2:].strip().partition(' ')
    if not name:
        return None
    return name, value.strip()


def parse_decorators(content: str) -> tuple[list[tuple[str, str]], str]:
    """Split leading decorator lines off an entry's content.

    Returns ``(decorators, body)``. A ``@@@name value`` line is a fallback for the decorator
    line before it: it is used only when the preceding decorator is not understood. Unknown
    decorators are dropped and logged at debug level.
    """
    lines = content.split('\n')
    decorators: list[tuple[str, str]] = []
    previous_known = True
    index = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('@@@'):
            fallback = _parse_decorator_line(stripped[1:])
            if fallback and not previous_known and fallback[0] in KNOWN_DECORATORS:
                decorators.append(fallback)
                previous_known = True
            continue
        parsed = _parse_decorator_line(stripped)
        if parsed is None:
            break
        previous_known = parsed[0] in KNOWN_DECORATORS
        if previous_known:
            decorators.append(parsed)
        else:
            log.debug('Ignoring unsupported lorebook decorator @@%s', parsed[0])
    else:
        index = len(lines)

    return decorators, '\n'.join(lines[index:])


def strip_decorators(content: str) -> str:
    """Return entry content without its leading decorator lines (for V2 exports)."""
    return parse_decorators(content)[1]


####################
# Activation
####################

# User-authored patterns run against chat text on every request; bound their cost.
REGEX_TIMEOUT_SECONDS = 0.05
MAX_REGEX_LENGTH = 500
ENTRY_ROLES = ('system', 'user', 'assistant')


def estimate_tokens(text: str) -> int:
    """Rough token count, the same 4-characters-per-token estimate context compaction uses."""
    return max(1, len(text) // 4) if text else 0


@dataclass
class DepthEntry:
    text: str
    depth: int
    role: str = 'system'


@dataclass
class Activation:
    """Activated entry contents, grouped by where they go, each list in insertion order."""

    before_char: list[str] = field(default_factory=list)
    after_char: list[str] = field(default_factory=list)
    depth: list[DepthEntry] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.before_char or self.after_char or self.depth)


@dataclass
class _Candidate:
    entry: dict
    body: str
    decorators: dict[str, str]
    order: int


def _decorator_map(decorators: list[tuple[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in decorators:
        values.setdefault(name, value)
    return values


def _split_keys(value: str) -> list[str]:
    return [key.strip() for key in value.split(',') if key.strip()]


def _key_matches(key: str, text: str, use_regex: bool, case_sensitive: bool) -> bool:
    if not key:
        return False
    if use_regex:
        if len(key) > MAX_REGEX_LENGTH:
            return False
        try:
            flags = 0 if case_sensitive else regex.IGNORECASE
            return regex.search(key, text, flags=flags, timeout=REGEX_TIMEOUT_SECONDS) is not None
        except (regex.error, TimeoutError):
            log.debug('Skipping lorebook key %r (invalid or too slow)', key)
            return False
    if case_sensitive:
        return key in text
    return key.casefold() in text.casefold()


def _any_match(keys: list[str], text: str, entry: dict) -> bool:
    return any(_key_matches(key, text, entry['use_regex'], entry['case_sensitive']) for key in keys)


def _entry_matches(candidate: _Candidate, text: str) -> bool:
    entry, decorators = candidate.entry, candidate.decorators
    if 'dont_activate' in decorators:
        return False
    if entry['constant'] or 'activate' in decorators:
        return True
    keys = entry['keys'] + _split_keys(decorators.get('additional_keys', ''))
    if not _any_match(keys, text, entry):
        return False
    if _any_match(_split_keys(decorators.get('exclude_keys', '')), text, entry):
        return False
    if entry['selective'] and entry['secondary_keys']:
        return _any_match(entry['secondary_keys'], text, entry)
    return True


def _message_text(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, list):
        return '\n'.join(part.get('text', '') for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ''


def _scan_text(messages: list[dict], depth: int) -> str:
    chat = [message for message in messages if message.get('role') in ('user', 'assistant')]
    return '\n'.join(_message_text(message) for message in chat[-depth:]) if depth > 0 else ''


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _candidates(book: dict) -> list[_Candidate]:
    candidates = []
    for order, raw_entry in enumerate(book.get('entries', [])):
        entry = normalize_entry(raw_entry, order)
        if entry is None or not entry['enabled']:
            continue
        decorators, body = parse_decorators(entry.get('content', ''))
        if body.strip():
            candidates.append(_Candidate(entry, body.strip(), _decorator_map(decorators), order))
    return candidates


def _activate_book(book: dict, messages: list[dict]) -> list[_Candidate]:
    candidates = _candidates(book)
    book_depth = book.get('scan_depth') or DEFAULT_SCAN_DEPTH
    texts: dict[int, str] = {}

    def text_for(candidate: _Candidate) -> str:
        depth = _int(candidate.decorators.get('scan_depth', book_depth), book_depth)
        if depth not in texts:
            texts[depth] = _scan_text(messages, depth)
        return texts[depth]

    active = [candidate for candidate in candidates if _entry_matches(candidate, text_for(candidate))]

    if book.get('recursive_scanning') and active:
        # One more pass over the content of what activated, as the V2 spec allows.
        recursive_text = '\n'.join(candidate.body for candidate in active)
        active += [
            candidate
            for candidate in candidates
            if candidate not in active and _entry_matches(candidate, recursive_text)
        ]

    return _apply_budget(active, book.get('token_budget'))


def _apply_budget(active: list[_Candidate], budget: int | None) -> list[_Candidate]:
    """Keep the highest-priority entries that fit in ``budget`` tokens."""
    ranked = sorted(active, key=lambda c: (-c.entry['priority'], c.entry['insertion_order'], c.order))
    if not budget or budget <= 0:
        return ranked
    kept, used = [], 0
    for candidate in ranked:
        cost = estimate_tokens(candidate.body)
        if used + cost > budget:
            break
        kept.append(candidate)
        used += cost
    return kept


def _place(candidate: _Candidate, activation: Activation) -> None:
    decorators = candidate.decorators
    if 'depth' in decorators:
        role = decorators.get('role', 'system')
        activation.depth.append(
            DepthEntry(
                text=candidate.body,
                depth=max(0, _int(decorators['depth'], DEFAULT_SCAN_DEPTH)),
                role=role if role in ENTRY_ROLES else 'system',
            )
        )
        return
    position = decorators.get('position', candidate.entry['position'])
    if position == 'after_char':
        activation.after_char.append(candidate.body)
    else:
        activation.before_char.append(candidate.body)


def activate(lorebooks: list[dict], messages: list[dict]) -> Activation:
    """Select and place the entries of ``lorebooks`` that apply to ``messages``.

    Each book applies its own scan depth, recursion and token budget. Activated entries are placed
    in ``insertion_order``: entries with ``@@depth`` go into the history, the rest before or after
    the character description.
    """
    chosen = [candidate for book in lorebooks for candidate in _activate_book(book, messages)]
    chosen.sort(key=lambda c: (c.entry['insertion_order'], c.order))
    activation = Activation()
    for candidate in chosen:
        _place(candidate, activation)
    return activation
