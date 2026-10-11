"""Character expressions: per-emotion images, and picking one for a reply.

A character keeps ``{name: file_id}`` in ``meta.character.expressions``. Names are free-form, as in
the cards that ship them (``joy``, ``smirk``, ``crying``…), so classification chooses among the
character's own names rather than a fixed list.
"""

from __future__ import annotations

import re

MAX_EXPRESSIONS = 64
MAX_NAME_LENGTH = 40
CLASSIFY_TEXT_CHARS = 2_000

CLASSIFY_PROMPT = """Which expression best shows how {{char}} looks and feels in this roleplay message?

Choose exactly one of: {{NAMES}}

Reply with only the expression name, nothing else.

<message>
{{TEXT}}
</message>"""


def clean_expression_name(name: str) -> str:
    """Lowercase, single-spaced, letters/digits/space/_/- only (any script), at most 40 characters."""
    name = re.sub(r'[^\w\s-]', '', (name or '').strip().lower())
    return re.sub(r'\s+', ' ', name).strip()[:MAX_NAME_LENGTH].strip()


def expression_name_from_filename(filename: str) -> str:
    """``joy.png`` and ``sprites/Joy_1.webp`` give ``joy`` and ``joy_1``."""
    stem = (filename or '').replace('\\', '/').rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return clean_expression_name(stem)


def classify_prompt(char: str, names: list[str], text: str) -> str:
    text = text.strip()
    if len(text) > CLASSIFY_TEXT_CHARS:
        text = '…' + text[-CLASSIFY_TEXT_CHARS:]  # the end of a reply is where the character is now
    return (
        CLASSIFY_PROMPT.replace('{{char}}', char or 'the character')
        .replace('{{NAMES}}', ', '.join(names))
        .replace('{{TEXT}}', text)
    )


def pick_expression(reply: str, names: list[str]) -> str | None:
    """The expression a model reply names: an exact answer, else the longest name it mentions."""
    reply = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', reply or '', flags=re.DOTALL | re.IGNORECASE)
    answer = clean_expression_name(reply.strip().splitlines()[0] if reply.strip() else '')
    if answer in names:
        return answer
    lowered = reply.lower()
    for name in sorted(names, key=len, reverse=True):
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', lowered):
            return name
    return None
