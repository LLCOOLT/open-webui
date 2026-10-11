"""Draft roleplay characters and a world lorebook from a story.

The story is split into chunks; the model extracts characters (appearance, background, relationships,
goals, voice), world details and events from each chunk as JSON; the results are merged by name (and
alias) without the model; characters with notes from several chunks get one consolidation pass.
A detailed profile pass then rereads the passages where a main character appears and writes a full
sheet with example dialogue in their voice. Nothing here saves anything: the result is a draft the
admin reviews before ``/characters/story/create``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

Complete = Callable[[str], Awaitable[str]]

CHUNK_CHARS = 12_000
MAX_STORY_CHARS = 480_000
CONCURRENCY = 4
MAX_QUOTES = 6
MAX_CONSOLIDATED_CHARACTERS = 8
MAX_FIELD_CHARS = 6_000
MAX_PROFILES = 4
PROFILE_EXCERPT_CHARS = 24_000
WORLD_KINDS = ('place', 'faction', 'object', 'concept', 'lore')

CHUNK_PROMPT = """You are reading part {{PART}} of {{TOTAL}} of a story{{TITLE}}, to adapt it for roleplay.

Reply with only a JSON object of this shape:
{"characters": [{"name": "", "aliases": [], "appearance": "", "background": "", "personality": "",
   "speech": "", "relationships": [{"name": "", "relation": ""}], "goals": "", "quotes": []}],
 "world": [{"name": "", "kind": "place|faction|object|concept|lore", "aliases": [], "content": ""}],
 "events": [""]}

Rules:
- Only include what this part states or clearly shows. Do not guess or add outside knowledge.
- characters: people (or beings) who act or speak in this part. Write in the third person, using names.
  appearance: looks, clothing, age, distinctive features. background: history, role, occupation,
  abilities and what has happened to them. personality: temperament, values, fears, habits and how
  they treat others. speech: how they talk (tone, vocabulary, verbal tics). relationships: how they
  relate to the other characters in this part. goals: what they want or are trying to do.
  quotes: up to 4 short lines they actually say here, copied exactly.
- Be specific and concrete; prefer details the story shows over general adjectives.
- world: named places, groups, objects and ideas that matter to the story, each with what this part
  reveals about it.
- events: 1 to 8 one-sentence summaries of what happens in this part, in order.
- Keep each text field under 120 words; leave a field empty when this part says nothing about it.

<story_part>
{{TEXT}}
</story_part>"""

CONSOLIDATE_PROMPT = """These are notes about {{NAME}} collected from different parts of a story, in story order.
Merge them into one character sheet. Keep every distinct fact, drop repetition, do not add anything.
Where later notes show the character changing, describe who they are by the end and note the change.

Reply with only a JSON object:
{"appearance": "", "background": "", "personality": "", "speech": "", "relationships": "", "goals": ""}
(each field under 150 words; relationships as one line per person: "Name: relation")

<notes>
{{NOTES}}
</notes>"""

PROFILE_PROMPT = """Write a detailed roleplay character sheet for {{NAME}}{{ALIASES}}, from the story{{TITLE}}.
Use the notes and the passages below, which are every scene {{NAME}} appears in (or a sample of them).
Stay faithful to the story: everything must come from the passages or notes; do not invent backstory.

Reply with only a JSON object:
{"appearance": "", "background": "", "personality": "", "speech": "", "mannerisms": "",
 "relationships": [{"name": "", "relation": ""}], "goals": "", "example_dialogue": [{"user": "", "char": ""}]}

- appearance, background: concrete details, up to 150 words each.
- personality: temperament, values, fears, contradictions, how they treat friends and strangers; up to 200 words.
- speech: tone, vocabulary, sentence length, verbal tics, what they never say; up to 100 words.
- mannerisms: habitual gestures and behaviours shown in the story; up to 80 words.
- relationships: everyone important to them, each with how they feel about that person.
- goals: what drives them and what they fear losing.
- example_dialogue: 3 or 4 short exchanges where {{user}} says something and {{NAME}} answers in
  their own voice, with actions between asterisks. Reuse lines from the story where they fit.

<notes>
{{NOTES}}
</notes>

<passages>
{{EXCERPTS}}
</passages>"""

GREETING_PROMPT = """Write the opening message for a roleplay chat in which {{NAME}} talks to {{user}}.

Character: {{DESCRIPTION}}
Personality: {{PERSONALITY}}
Scenario: {{SCENARIO}}

Write only {{NAME}}'s message: 2 to 5 sentences, in character, actions between asterisks, ending
in a way that invites {{user}} to respond. Never speak or act for {{user}}."""


####################
# Chunking and parsing
####################


def split_story(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Paragraph-aligned chunks of about ``chunk_chars``; overlong paragraphs are cut on sentences."""
    paragraphs = [part.strip() for part in re.split(r'\n\s*\n', text.replace('\r\n', '\n')) if part.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > chunk_chars:
            cut = paragraph.rfind('. ', 0, chunk_chars)
            cut = cut + 1 if cut > chunk_chars // 2 else chunk_chars
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)

    chunks: list[str] = []
    current = ''
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > chunk_chars:
            chunks.append(current)
            current = piece
        else:
            current = f'{current}\n\n{piece}' if current else piece
    if current:
        chunks.append(current)
    return chunks


def parse_json_object(reply: str) -> dict:
    """The first ``{...}`` in a model reply. Raises ``ValueError`` when there is none."""
    reply = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', reply, flags=re.DOTALL | re.IGNORECASE)
    start, end = reply.find('{'), reply.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('The model did not return JSON.')
    try:
        value = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError('The model did not return valid JSON.') from error
    if not isinstance(value, dict):
        raise ValueError('The model did not return a JSON object.')
    return value


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ''


def _texts(value) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _relationships(value) -> list[tuple[str, str]]:
    """``[{"name", "relation"}]``, ``{"Name": "relation"}`` or ``"Name: relation"`` lines."""
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        value = [{'name': name, 'relation': relation} for name, relation in value.items()]
    if isinstance(value, str):
        value = [
            dict(zip(('name', 'relation'), line.lstrip('-* ').split(':', 1)))
            for line in value.splitlines()
            if ':' in line
        ]
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and _text(item.get('name')) and _text(item.get('relation')):
            pairs.append((_text(item['name']), _text(item['relation'])))
    return pairs


def _key(name: str) -> str:
    return re.sub(r'\s+', ' ', name).strip().casefold()


####################
# Merging
####################


@dataclass
class _Character:
    name: str
    aliases: set[str] = field(default_factory=set)
    appearance: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)  # background (or a model's free-form description)
    personalities: list[str] = field(default_factory=list)
    speech: list[str] = field(default_factory=list)
    relationships: dict[str, list[str]] = field(default_factory=dict)
    goals: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    chunks: set[int] = field(default_factory=set)

    def names(self) -> set[str]:
        return {_key(self.name), *(_key(alias) for alias in self.aliases)}

    def note_count(self) -> int:
        return (
            len(self.appearance)
            + len(self.descriptions)
            + len(self.personalities)
            + len(self.goals)
            + sum(len(items) for items in self.relationships.values())
        )

    def notes_text(self) -> str:
        lines = [*(f'- Appearance: {text}' for text in self.appearance)]
        lines += [f'- Background: {text}' for text in self.descriptions]
        lines += [f'- Personality: {text}' for text in self.personalities]
        lines += [f'- Speech: {text}' for text in self.speech]
        lines += [f'- Relationship with {name}: {text}' for name, items in self.relationships.items() for text in items]
        lines += [f'- Goals: {text}' for text in self.goals]
        lines += [f'- Says: "{quote}"' for quote in self.quotes]
        return '\n'.join(lines)


@dataclass
class _WorldItem:
    name: str
    kind: str
    aliases: set[str] = field(default_factory=set)
    contents: list[str] = field(default_factory=list)


def _add_unique(target: list[str], value: str) -> None:
    if value and _key(value) not in {_key(existing) for existing in target}:
        target.append(value)


def _find(items: list, names: set[str]):
    return next((item for item in items if item.names() & names), None) if names else None


@dataclass
class StoryNotes:
    characters: list[_Character] = field(default_factory=list)
    world: list[_WorldItem] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def add_chunk(self, index: int, data: dict) -> None:
        for raw in data.get('characters') or []:
            if isinstance(raw, dict) and _text(raw.get('name')):
                self._add_character(index, raw)
        for raw in data.get('world') or []:
            if isinstance(raw, dict) and _text(raw.get('name')):
                self._add_world_item(raw)
        for event in _texts(data.get('events')):
            _add_unique(self.events, event)

    def _add_character(self, index: int, raw: dict) -> None:
        name, aliases = _text(raw['name']), set(_texts(raw.get('aliases')))
        character = _find(self.characters, {_key(name), *(_key(alias) for alias in aliases)})
        if character is None:
            character = _Character(name=name)
            self.characters.append(character)
        elif _key(name) != _key(character.name):
            aliases.add(name)
        character.aliases |= {alias for alias in aliases if _key(alias) != _key(character.name)}
        character.chunks.add(index)
        _add_unique(character.appearance, _text(raw.get('appearance')))
        _add_unique(character.descriptions, _text(raw.get('description')))
        _add_unique(character.descriptions, _text(raw.get('background')))
        _add_unique(character.personalities, _text(raw.get('personality')))
        _add_unique(character.speech, _text(raw.get('speech')))
        _add_unique(character.goals, _text(raw.get('goals')))
        for name, relation in _relationships(raw.get('relationships')):
            if _key(name) not in character.names():
                _add_unique(character.relationships.setdefault(name, []), relation)
        for quote in _texts(raw.get('quotes')):
            if len(character.quotes) < MAX_QUOTES:
                _add_unique(character.quotes, quote.strip('"“”'))

    def _add_world_item(self, raw: dict) -> None:
        name, aliases = _text(raw['name']), set(_texts(raw.get('aliases')))
        names = {_key(name), *(_key(alias) for alias in aliases)}
        item = next((item for item in self.world if {_key(item.name), *(_key(a) for a in item.aliases)} & names), None)
        if item is None:
            kind = _text(raw.get('kind')).lower()
            item = _WorldItem(name=name, kind=kind if kind in WORLD_KINDS else 'lore')
            self.world.append(item)
        item.aliases |= {alias for alias in aliases if _key(alias) != _key(item.name)}
        _add_unique(item.contents, _text(raw.get('content')))


####################
# Drafts
####################


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rsplit(' ', 1)[0] + '…'


def _with_char(text: str, character: _Character) -> str:
    for name in sorted({character.name, *character.aliases}, key=len, reverse=True):
        text = re.sub(rf'(?<![\w{{]){re.escape(name)}(?![\w}}])', '{{char}}', text)
    return text


def _sheet(sections: list[tuple[str, str]]) -> str:
    """``Label: text`` paragraphs, skipping empty sections."""
    return '\n\n'.join(f'{label}: {text}' if label else text for label, text in sections if text)


def _relationship_lines(relationships: dict[str, list[str]]) -> str:
    return '\n'.join(f'- {name}: {" ".join(items)}' for name, items in relationships.items() if items)


def _description(character: _Character) -> str:
    relationships = _relationship_lines(character.relationships)
    return _sheet(
        [
            ('Appearance', ' '.join(character.appearance)),
            ('', '\n\n'.join(character.descriptions))
            if not character.appearance
            else ('Background', ' '.join(character.descriptions)),
            ('Relationships', f'\n{relationships}' if relationships else ''),
            ('Goals', ' '.join(character.goals)),
        ]
    )


def character_draft(character: _Character, title: str = '', min_chunks_for_selection: int = 2) -> dict:
    personality = '\n'.join(character.personalities)
    if character.speech:
        personality = '\n'.join(part for part in (personality, 'Speech: ' + ' '.join(character.speech)) if part)
    examples = '\n'.join(f'{{{{char}}}}: {quote}' for quote in character.quotes)
    return {
        'name': character.name,
        'aliases': sorted(character.aliases),
        'description': _clip(_with_char(_description(character), character)),
        'personality': _clip(_with_char(personality, character)),
        'mes_example': f'<START>\n{examples}' if examples else '',
        'first_mes': '',
        'creator_notes': f'Adapted from {title}.' if title else '',
        'mentions': len(character.chunks),
        'selected': len(character.chunks) >= min_chunks_for_selection,
        'detailed': False,
    }


def lorebook_draft(notes: StoryNotes, title: str = '') -> dict:
    entries = []
    for index, item in enumerate(notes.world):
        entries.append(
            {
                'name': item.name,
                'comment': item.kind,
                'keys': [item.name, *sorted(item.aliases)],
                'content': _clip(f'{item.name}: ' + ' '.join(item.contents)),
                'insertion_order': 100 + index,
                'enabled': True,
            }
        )
    for index, character in enumerate(notes.characters):
        # Other characters come up in chat too; a short entry keeps the world consistent.
        summary = ' '.join([*character.appearance[:1], *character.descriptions[:1]])
        if summary:
            entries.append(
                {
                    'name': character.name,
                    'comment': 'character',
                    'keys': [character.name, *sorted(character.aliases)],
                    'content': _clip(f'{character.name}: {summary}', 800),
                    'insertion_order': 500 + index,
                    'priority': 5,
                    'enabled': True,
                }
            )
    return {'name': f'{title} world' if title else 'Story world', 'entries': entries}


@dataclass
class StoryDraft:
    characters: list[dict]
    lorebook: dict
    events: list[str]
    chunks: int
    failed_chunks: int


async def _gather_limited(factories: list[Callable[[], Awaitable]], limit: int) -> list:
    semaphore = asyncio.Semaphore(limit)

    async def run(factory):
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(factory) for factory in factories), return_exceptions=True)


async def _consolidate(complete: Complete, character: _Character) -> None:
    prompt = CONSOLIDATE_PROMPT.replace('{{NAME}}', character.name).replace('{{NOTES}}', character.notes_text())
    merged = parse_json_object(await complete(prompt))
    fields = {key: _text(merged.get(key)) for key in ('appearance', 'background', 'personality', 'speech', 'goals')}
    # Older prompts (and some models) answer with a single free-form description.
    fields['background'] = fields['background'] or _text(merged.get('description'))
    if not any(fields.values()):
        return
    character.appearance = [fields['appearance']] if fields['appearance'] else []
    character.descriptions = [fields['background']] if fields['background'] else []
    if fields['personality']:
        character.personalities = [fields['personality']]
        character.speech = [fields['speech']] if fields['speech'] else []
    if fields['goals']:
        character.goals = [fields['goals']]
    relationships = _relationships(merged.get('relationships'))
    if relationships:
        character.relationships = {}
        for name, relation in relationships:
            character.relationships.setdefault(name, []).append(relation)


async def _extract_notes(chunks: list[str], complete: Complete, title: str) -> tuple[StoryNotes, int]:
    title_part = f' titled "{title}"' if title else ''

    async def extract(index: int, chunk: str) -> dict:
        prompt = (
            CHUNK_PROMPT.replace('{{PART}}', str(index + 1))
            .replace('{{TOTAL}}', str(len(chunks)))
            .replace('{{TITLE}}', title_part)
            .replace('{{TEXT}}', chunk)
        )
        return parse_json_object(await complete(prompt))

    results = await _gather_limited(
        [lambda index=index, chunk=chunk: extract(index, chunk) for index, chunk in enumerate(chunks)], CONCURRENCY
    )
    notes = StoryNotes()
    failed = 0
    for index, result in enumerate(results):
        if isinstance(result, Exception):
            log.warning('Story chunk %d could not be analyzed: %s', index, result)
            failed += 1
        else:
            notes.add_chunk(index, result)
    return notes, failed


async def _consolidate_main_characters(notes: StoryNotes, complete: Complete) -> None:
    """One merge pass for the most present characters whose notes came from several chunks."""
    main = [character for character in notes.characters[:MAX_CONSOLIDATED_CHARACTERS] if character.note_count() > 2]
    results = await _gather_limited([lambda c=c: _consolidate(complete, c) for c in main], CONCURRENCY)
    for character, result in zip(main, results):
        if isinstance(result, Exception):
            log.info('Keeping unmerged notes for %s: %s', character.name, result)


####################
# Detailed profiles
####################


def _name_pattern(names: list[str]) -> re.Pattern | None:
    names = sorted({name.strip() for name in names if len(name.strip()) > 1}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(name) for name in names) + r')(?!\w)')


def character_excerpts(text: str, names: list[str], budget: int = PROFILE_EXCERPT_CHARS) -> str:
    """The paragraphs that mention any of ``names`` (plus the one after each, which often holds the
    reply), in story order; spread evenly across the story when they exceed ``budget``."""
    pattern = _name_pattern(names)
    if pattern is None:
        return ''
    paragraphs = [part.strip() for part in re.split(r'\n\s*\n', text.replace('\r\n', '\n')) if part.strip()]
    hits = [index for index, paragraph in enumerate(paragraphs) if pattern.search(paragraph)]
    picked = sorted({i for hit in hits for i in (hit, hit + 1) if i < len(paragraphs)})
    if not picked:
        return ''

    def size(indices: list[int]) -> int:
        return sum(len(paragraphs[i]) + 2 for i in indices)

    if size(picked) > budget:
        # Keep the mentions themselves, sampled evenly, so early and late scenes both appear.
        average = max(1, size(hits) // len(hits))
        count = max(1, min(len(hits), budget // average))
        step = len(hits) / count
        picked = [hits[int(i * step)] for i in range(count)]
        while len(picked) > 1 and size(picked) > budget:
            picked = picked[::2]

    parts, previous = [], None
    for index in picked:
        if previous is not None and index != previous + 1:
            parts.append('[…]')
        parts.append(_clip(paragraphs[index], budget))
        previous = index
    return '\n\n'.join(parts)


def _example_dialogue(value) -> str:
    exchanges = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        user, char = _text(item.get('user')), _text(item.get('char'))
        if char:
            lines = [f'{{{{user}}}}: {user}'] if user else []
            exchanges.append('<START>\n' + '\n'.join([*lines, f'{{{{char}}}}: {char}']))
    return '\n'.join(exchanges)


def profile_prompt(character: dict, story: str, title: str = '') -> str:
    name = _text(character.get('name')) or 'the character'
    aliases = [alias for alias in _texts(character.get('aliases')) if _key(alias) != _key(name)]
    notes = '\n\n'.join(
        part
        for part in (
            _text(character.get('description')),
            _text(character.get('personality')),
            _text(character.get('mes_example')),
        )
        if part
    ).replace('{{char}}', name)
    excerpts = character_excerpts(story, [name, *aliases])
    return (
        PROFILE_PROMPT.replace('{{NAME}}', name)
        .replace('{{ALIASES}}', f' (also called {", ".join(aliases)})' if aliases else '')
        .replace('{{TITLE}}', f' "{title}"' if title else '')
        .replace('{{NOTES}}', notes or 'None yet.')
        .replace('{{EXCERPTS}}', excerpts or 'None found; rely on the notes.')
    )


async def write_profile(character: dict, story: str, complete: Complete, title: str = '') -> dict:
    """A detailed sheet for one drafted character: ``description``, ``personality``, ``mes_example``.

    Fields the model leaves empty keep the draft's value. Raises ``ValueError`` on a non-JSON reply.
    """
    reply = parse_json_object(await complete(profile_prompt(character, story, title)))
    name = _text(character.get('name'))
    holder = _Character(name=name, aliases=set(_texts(character.get('aliases'))))
    for other, relation in _relationships(reply.get('relationships')):
        if _key(other) not in holder.names():
            holder.relationships.setdefault(other, []).append(relation)
    relationships = _relationship_lines(holder.relationships)
    description = _sheet(
        [
            ('Appearance', _text(reply.get('appearance'))),
            ('Background', _text(reply.get('background'))),
            ('Relationships', f'\n{relationships}' if relationships else ''),
            ('Goals', _text(reply.get('goals'))),
        ]
    )
    personality = _sheet(
        [
            ('', _text(reply.get('personality'))),
            ('Speech', _text(reply.get('speech'))),
            ('Mannerisms', _text(reply.get('mannerisms'))),
        ]
    )
    examples = _example_dialogue(reply.get('example_dialogue'))
    return {
        'description': _clip(_with_char(description, holder)) if description else character.get('description', ''),
        'personality': _clip(_with_char(personality, holder)) if personality else character.get('personality', ''),
        'mes_example': _clip(_with_char(examples, holder)) if examples else character.get('mes_example', ''),
    }


async def _write_profiles(characters: list[dict], story: str, complete: Complete, title: str) -> None:
    chosen = [character for character in characters if character['selected']][:MAX_PROFILES]
    results = await _gather_limited([lambda c=c: write_profile(c, story, complete, title) for c in chosen], CONCURRENCY)
    for character, result in zip(chosen, results):
        if isinstance(result, Exception):
            log.info('Keeping the short draft for %s: %s', character['name'], result)
        else:
            character.update(result, detailed=True)


async def analyze_story(text: str, complete: Complete, title: str = '', detailed: bool = True) -> StoryDraft:
    """Extract over every chunk, merge, consolidate the main characters, and draft fields; with
    ``detailed``, also write full profiles for the preselected (main) characters."""
    text = text.strip()
    if not text:
        raise ValueError('The story is empty.')
    if len(text) > MAX_STORY_CHARS:
        raise ValueError(f'The story is too long ({len(text):,} characters; the limit is {MAX_STORY_CHARS:,}).')

    chunks = split_story(text)
    notes, failed = await _extract_notes(chunks, complete, title)
    if failed == len(chunks):
        raise ValueError('The model could not analyze the story.')

    notes.characters.sort(key=lambda character: -len(character.chunks))
    await _consolidate_main_characters(notes, complete)

    min_chunks = 2 if len(chunks) > 1 else 1
    characters = [character_draft(character, title, min_chunks) for character in notes.characters]
    if characters and not any(character['selected'] for character in characters):
        characters[0]['selected'] = True
    if detailed:
        await _write_profiles(characters, text, complete, title)
    return StoryDraft(
        characters=characters,
        lorebook=lorebook_draft(notes, title),
        events=notes.events,
        chunks=len(chunks),
        failed_chunks=failed,
    )


def greeting_prompt(character: dict, scenario: str) -> str:
    name = _text(character.get('name')) or 'the character'
    return (
        GREETING_PROMPT.replace('{{NAME}}', name)
        .replace('{{DESCRIPTION}}', _text(character.get('description')).replace('{{char}}', name))
        .replace('{{PERSONALITY}}', _text(character.get('personality')).replace('{{char}}', name))
        .replace('{{SCENARIO}}', scenario.replace('{{char}}', name) or 'None given.')
    )
