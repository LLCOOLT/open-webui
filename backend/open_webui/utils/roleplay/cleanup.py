"""Reply cleanup for character chats (SillyTavern's "regex scripts" and output trimming).

Applied once, when a reply finishes, to the text the model wrote, so what is shown, saved and sent
back on later turns is the cleaned version:

- ``stop_at_user``: cut the reply where the model starts writing ``{{user}}``'s lines
  (``Robin: ...``), and drop a leading ``{{char}}:`` name prefix. The same name is also sent as a
  stop sequence, so the model usually stops there by itself.
- ``rules``: find-and-replace rules, plain text or regular expressions, applied in order.
  ``{{char}}`` and ``{{user}}`` work in both fields; regex replacements refer to groups as ``$1``
  or ``$<name>``, as in SillyTavern.
- ``trim_incomplete``: drop a final sentence the model did not finish (e.g. cut off by max tokens).
"""

from __future__ import annotations

import logging
import re

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

MAX_RULES = 32
RULE_TIMEOUT_SECONDS = 0.25
MAX_STOP_SEQUENCES = 4  # OpenAI's limit

# What a finished sentence ends with: punctuation, or a closing quote, action (*...*) or bracket.
SENTENCE_STOPS = '.!?…。！？~♪'
SENTENCE_CLOSERS = '"”*_)]」』》`'


class CleanupRule(BaseModel):
    model_config = ConfigDict(extra='ignore')

    find: str = Field(default='', max_length=1000)
    replace: str = Field(default='', max_length=2000)
    regex: bool = False
    enabled: bool = True


class ReplyCleanup(BaseModel):
    model_config = ConfigDict(extra='ignore')

    stop_at_user: bool = True
    trim_incomplete: bool = False
    rules: list[CleanupRule] = Field(default_factory=list)

    @field_validator('rules', mode='before')
    @classmethod
    def limit_rules(cls, value):
        return value[:MAX_RULES] if isinstance(value, list) else []

    def active(self) -> bool:
        return self.stop_at_user or self.trim_incomplete or any(rule.enabled and rule.find for rule in self.rules)


def _name_pattern(name: str) -> str:
    return re.escape(name.strip())


def _speaker_line(names: list[str]) -> re.Pattern | None:
    """A line opening with ``Name:``, allowing markdown around the name (``**Robin:**``)."""
    names = [_name_pattern(name) for name in names if name and name.strip()]
    if not names:
        return None
    return re.compile(rf'^[ \t>*_"]*(?:{"|".join(names)})[ \t*_]*:', re.MULTILINE | re.IGNORECASE)


def cut_user_lines(text: str, char: str, user: str) -> str:
    """Remove a leading ``{{char}}:`` and everything from the first line spoken as ``{{user}}``."""
    char_line = _speaker_line([char])
    if char_line is not None:
        match = char_line.match(text.lstrip())
        if match:
            text = text.lstrip()[match.end() :].lstrip(' \t')
    user_line = _speaker_line([user, '{{user}}'])
    if user_line is not None:
        for match in user_line.finditer(text):
            if text[: match.start()].strip():
                return text[: match.start()].rstrip()
    return text


def _ends_sentence(text: str, index: int) -> bool:
    char = text[index]
    if char in SENTENCE_STOPS:
        return True
    # Quotes and markdown close a sentence only when closing (right after text), not when opening.
    return char in SENTENCE_CLOSERS and index > 0 and not text[index - 1].isspace()


def trim_incomplete_sentence(text: str) -> str:
    """Cut an unfinished final sentence. Text without any sentence end is left as is."""
    stripped = text.rstrip()
    if not stripped or _ends_sentence(stripped, len(stripped) - 1):
        return stripped
    for index in range(len(stripped) - 2, 0, -1):
        if _ends_sentence(stripped, index):
            return stripped[: index + 1].rstrip()
    return stripped


def _expand(text: str, char: str, user: str, escape: bool = False) -> str:
    names = {'char': char, 'user': user}

    def value(match: re.Match) -> str:
        name = names[match.group(1).lower()]
        return regex.escape(name) if escape else name

    return re.sub(r'\{\{(char|user)\}\}', value, text, flags=re.IGNORECASE)


def _parse_regex(find: str) -> tuple[str, int]:
    """``/pattern/flags`` (SillyTavern's form) or a bare pattern."""
    match = re.fullmatch(r'/(.*)/([a-z]*)', find, re.DOTALL)
    if not match:
        return find, 0
    flags = 0
    for letter in match.group(2):
        flags |= {'i': regex.IGNORECASE, 'm': regex.MULTILINE, 's': regex.DOTALL}.get(letter, 0)
    return match.group(1), flags


def apply_rule(text: str, rule: CleanupRule, char: str, user: str) -> str:
    if not rule.enabled or not rule.find:
        return text
    replacement = _expand(rule.replace, char, user)
    if not rule.regex:
        return text.replace(_expand(rule.find, char, user), replacement)
    pattern, flags = _parse_regex(rule.find)
    # JavaScript-style $1 / $<name> groups, as in SillyTavern scripts.
    replacement = re.sub(r'\$(\d+)', r'\\g<\1>', replacement.replace('\\', '\\\\'))
    replacement = re.sub(r'\$<(\w+)>', r'\\g<\1>', replacement)
    try:
        compiled = regex.compile(_expand(pattern, char, user, escape=True), flags)
        return compiled.sub(replacement, text, timeout=RULE_TIMEOUT_SECONDS)
    except (regex.error, TimeoutError, IndexError) as error:
        log.info('Skipping reply cleanup rule %r: %s', rule.find[:80], error)
        return text


def clean_reply(text: str, settings: ReplyCleanup, char: str, user: str) -> str:
    if not text:
        return text
    if settings.stop_at_user:
        text = cut_user_lines(text, char, user)
    for rule in settings.rules:
        text = apply_rule(text, rule, char, user)
    if settings.trim_incomplete:
        text = trim_incomplete_sentence(text)
    return text


def clean_output(output: list, settings: ReplyCleanup, char: str, user: str) -> list:
    """Clean the text of a structured reply: rules apply to every text part; cutting at ``{{user}}``
    lines and trimming apply to the end of the reply, its last text part."""
    parts = [
        part
        for item in output or []
        if isinstance(item, dict) and item.get('type') == 'message' and isinstance(item.get('content'), list)
        for part in item['content']
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    ]
    if not parts:
        return output
    rules_only = ReplyCleanup(stop_at_user=False, trim_incomplete=False, rules=settings.rules)
    for part in parts[:-1]:
        part['text'] = clean_reply(part['text'], rules_only, char, user)
    parts[-1]['text'] = clean_reply(parts[-1]['text'], settings, char, user)
    return output


def add_stop_sequences(form_data: dict, model: dict, user: str) -> None:
    """Stop when the model starts a ``{{user}}:`` line, keeping the stop sequences already set."""
    if not user.strip():
        return
    stop = form_data.get('stop')
    if stop is None:
        stop = (((model or {}).get('info') or {}).get('params') or {}).get('stop')
        # The provider routers decode escapes in the model's stop strings; they are copied here, so do the same.
        stop = [bytes(s, 'utf-8').decode('unicode_escape') for s in stop if isinstance(s, str)] if stop else []
    if isinstance(stop, str):
        stop = [stop]
    if not isinstance(stop, list):
        return
    sequence = f'\n{user.strip()}:'
    if sequence not in stop and len(stop) < MAX_STOP_SEQUENCES:
        form_data['stop'] = [*stop, sequence]
