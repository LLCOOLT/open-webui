"""Search and download character cards from public card sites.

Each source maps a search to that site's own API (or the data its pages load) and a card id to a
download URL on a fixed host. Card ids are validated against a strict pattern before they reach a
URL, and redirects or image links are only followed to the site's own hosts, so nothing here fetches
an arbitrary URL. Downloads are capped at ``CHARACTER_CARD_MAX_BYTES`` and returned as card bytes for
``parse_card_bytes``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode, urlparse

import aiohttp
from open_webui.env import CHARACTER_CARD_MAX_BYTES
from open_webui.utils.roleplay.cards import PNG_SIGNATURE, write_png_text_chunks
from open_webui.utils.session_pool import get_session

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=45, sock_connect=15)
SEARCH_MAX_BYTES = 8 * 1024 * 1024
HEADERS = {'User-Agent': 'OpenWebUI', 'Accept': 'application/json, image/*, */*'}
PAGE_SIZE = 24
SORTS = ('popular', 'newest', 'trending')


class SourceError(ValueError):
    """The site could not be reached or returned something unusable."""


@dataclass
class CardSummary:
    source: str
    id: str
    name: str
    tagline: str = ''
    creator: str = ''
    tags: list[str] = field(default_factory=list)
    image_url: str = ''
    page_url: str = ''
    nsfw: bool = False
    has_lorebook: bool | None = None
    tokens: int | None = None
    downloads: str = ''


@dataclass
class TagFilter:
    include: list[str] = field(default_factory=list)  # every one required
    exclude: list[str] = field(default_factory=list)  # none allowed

    def matches(self, tags: list[str]) -> bool:
        have = {tag.casefold() for tag in tags}
        return all(tag.casefold() in have for tag in self.include) and not any(
            tag.casefold() in have for tag in self.exclude
        )


def clean_tags(tags) -> list[str]:
    """At most 10 distinct tags, without commas, at most 40 characters each."""
    cleaned: list[str] = []
    for tag in tags or []:
        tag = re.sub(r'\s+', ' ', str(tag).replace(',', ' ')).strip()[:40]
        if tag and tag.casefold() not in {existing.casefold() for existing in cleaned}:
            cleaned.append(tag)
    return cleaned[:10]


@dataclass
class SearchPage:
    items: list[CardSummary]
    page: int
    has_more: bool

    def as_dict(self) -> dict:
        return {'items': [asdict(item) for item in self.items], 'page': self.page, 'has_more': self.has_more}


####################
# HTTP
####################


def _allowed(url: str, hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and any(host == allowed or host.endswith('.' + allowed) for allowed in hosts)


async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """The body, stopping one byte past ``max_bytes`` so callers can tell it was cut off."""
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:
            break
    return b''.join(chunks)[: max_bytes + 1]


async def _get(url: str, hosts: tuple[str, ...], max_bytes: int, **kwargs) -> tuple[bytes, str]:
    """GET ``url`` (on ``hosts``), reading at most ``max_bytes``. Returns the body and content type."""
    if not _allowed(url, hosts):
        raise SourceError('Refusing to fetch a URL outside the card site.')
    session = await get_session()
    try:
        async with session.get(url, headers=HEADERS, timeout=TIMEOUT, max_redirects=3, **kwargs) as response:
            if not _allowed(str(response.url), hosts):
                raise SourceError('The card site redirected somewhere unexpected.')
            if response.status >= 400:
                detail = (await _read_capped(response, 2_000)).decode('utf-8', 'replace')
                if 'not available in your country' in detail:
                    raise SourceError('The site is not available from this server’s location.')
                if 'cloudflare' in detail.lower() and response.status in (403, 429, 503):
                    raise SourceError(
                        'The site’s bot protection blocked this server. This happens more on cloud hosts than '
                        'at home; try again later, or download the card from the site and use Import Character Cards.'
                    )
                summary = _error_detail(detail)
                raise SourceError(f'The site answered {response.status}' + (f': {summary}' if summary else '.'))
            body = await _read_capped(response, max_bytes)
            if len(body) > max_bytes:
                raise SourceError('The download is too large.')
            return body, response.headers.get('Content-Type', '')
    except aiohttp.ClientError as error:
        raise SourceError(f'Could not reach the site: {error}') from error
    except TimeoutError as error:
        raise SourceError('The site did not answer in time.') from error


def _error_detail(body: str) -> str:
    """A short, readable reason from an error body: FastAPI-style ``detail`` lists, plain messages."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        text = re.sub(r'<[^>]+>|\s+', ' ', body).strip()
        return text[:160] if text and '<' not in body[:20] else ''
    detail = data.get('detail', data.get('message', data.get('error'))) if isinstance(data, dict) else data
    if isinstance(detail, list):
        parts = []
        for item in detail[:3]:
            if isinstance(item, dict):
                location = '.'.join(str(part) for part in item.get('loc') or [] if part not in ('query', 'body'))
                parts.append(f'{location}: {item.get("msg", "")}'.strip(': '))
            else:
                parts.append(str(item))
        detail = '; '.join(parts)
    return str(detail)[:200] if detail else ''


async def _get_json(url: str, hosts: tuple[str, ...]):
    body, _ = await _get(url, hosts, SEARCH_MAX_BYTES)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise SourceError('The site returned something other than JSON.') from error


def unflatten_devalue(data: list):
    """Decode SvelteKit's ``devalue`` format (``__data.json`` nodes): values reference others by index."""
    cache: dict[int, object] = {}

    def hydrate(index):
        if not isinstance(index, int) or index < 0 or index >= len(data):
            return None
        if index in cache:
            return cache[index]
        value = data[index]
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                kind = value[0]
                return value[1] if kind == 'Date' else hydrate(value[1]) if kind == 'Promise' else None
            out: list = []
            cache[index] = out
            out.extend(hydrate(item) for item in value)
            return out
        if isinstance(value, dict):
            obj: dict = {}
            cache[index] = obj
            for key, item in value.items():
                obj[key] = hydrate(item)
            return obj
        return value

    return hydrate(0)


async def _sveltekit_data(url: str, hosts: tuple[str, ...], key: str) -> dict:
    """The first page data node holding ``key`` from a SvelteKit ``__data.json`` response."""
    body, _ = await _get(url, hosts, SEARCH_MAX_BYTES)
    try:
        # Streamed responses add one JSON line per deferred value; the page data is the first line.
        payload = json.loads(body.decode('utf-8').split('\n', 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceError('The site returned something other than page data.') from error
    for node in payload.get('nodes') or []:
        if isinstance(node, dict) and node.get('type') == 'data' and isinstance(node.get('data'), list):
            root = unflatten_devalue(node['data'])
            if isinstance(root, dict) and key in root:
                return root
    raise SourceError('The site changed its page format; search is unavailable for now.')


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ''


def _strings(value) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


####################
# Chub (chub.ai / characterhub.org)
####################

CHUB_HOSTS = ('chub.ai', 'charhub.io', 'characterhub.org')
CHUB_ID = re.compile(r'^[\w.~-]+/[\w.~-]+$')
CHUB_SORTS = {'popular': 'download_count', 'newest': 'created_at', 'trending': 'trending_downloads'}


def _chub_summary(node: dict) -> CardSummary | None:
    full_path = _text(node.get('fullPath'))
    if not CHUB_ID.match(full_path):
        return None
    topics = _strings(node.get('topics'))
    return CardSummary(
        source='chub',
        id=full_path,
        name=_text(node.get('name')) or full_path.split('/')[-1],
        tagline=_text(node.get('tagline')) or _text(node.get('description'))[:300],
        creator=full_path.split('/')[0],
        tags=topics[:40],
        image_url=f'https://avatars.charhub.io/avatars/{full_path}/avatar.webp',
        page_url=f'https://chub.ai/characters/{full_path}',
        nsfw='NSFW' in {topic.upper() for topic in topics} or bool(node.get('nsfw_image')),
        has_lorebook=bool(node.get('related_lorebooks')) or None,
        tokens=node.get('nTokens') if isinstance(node.get('nTokens'), int) else None,
        downloads=str(node.get('starCount') or ''),
    )


# Chub has moved its search between hosts and versions; the first one that answers is remembered.
_chub_search_variant: int | None = None


def _chub_tag_params(tags: TagFilter | None) -> dict:
    params = {}
    if tags and tags.include:
        params['tags'] = ','.join(tags.include)
    if tags and tags.exclude:
        params['exclude_tags'] = ','.join(tags.exclude)
    return params


def _chub_search_urls(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter | None = None) -> list[str]:
    common = {
        'first': PAGE_SIZE,
        'page': page,
        'sort': CHUB_SORTS.get(sort, 'download_count'),
        'asc': 'false',
        'nsfw': 'true' if nsfw else 'false',
        'nsfl': 'false',
        'include_forks': 'true',
        **({'search': query} if query else {}),
        **_chub_tag_params(tags),
    }
    minimal = {
        'first': PAGE_SIZE,
        'page': page,
        'nsfw': 'true' if nsfw else 'false',
        **({'search': query} if query else {}),
        **_chub_tag_params(tags),
    }
    # chub.ai's own site calls the gateway; api.chub.ai/search answers the same. (The older
    # /api/characters/search that SillyTavern's extension used now reads "search" as a project id.)
    return [
        f'https://{host}/search?{urlencode({**params, "namespace": "characters"})}'
        for host in ('gateway.chub.ai', 'api.chub.ai')
        for params in (common, minimal)
    ]


async def _chub_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    global _chub_search_variant
    urls = _chub_search_urls(query, page, sort, nsfw, tags)
    order = list(range(len(urls)))
    if _chub_search_variant is not None:
        order.remove(_chub_search_variant)
        order.insert(0, _chub_search_variant)
    errors: list[str] = []
    data = None
    for index in order:
        try:
            data = await _get_json(urls[index], CHUB_HOSTS)
        except SourceError as error:
            log.info('Chub search %s failed: %s', urls[index].split('?')[0], error)
            if 'location' in str(error) or 'bot protection' in str(error):
                raise
            errors.append(str(error))
            continue
        _chub_search_variant = index
        break
    if data is None:
        raise SourceError('Chub search failed: ' + ' | '.join(dict.fromkeys(errors)))
    nodes = (data.get('nodes') or (data.get('data') or {}).get('nodes') or []) if isinstance(data, dict) else []
    items = [item for item in (_chub_summary(node) for node in nodes if isinstance(node, dict)) if item]
    if not nsfw:
        items = [item for item in items if not item.nsfw]
    return SearchPage(items=items, page=page, has_more=len(nodes) >= PAGE_SIZE)


def _to_png(image: bytes) -> bytes | None:
    if image.startswith(PNG_SIGNATURE):
        return image
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as picture:
            out = io.BytesIO()
            picture.convert('RGBA').save(out, format='PNG')
            return out.getvalue()
    except Exception as error:  # Pillow raises several types for unreadable images
        log.info('Could not convert the Chub avatar to PNG: %s', error)
        return None


async def _chub_download(card_id: str) -> tuple[bytes, str]:
    """Rebuild a V2 card from the character's current definition, as SillyTavern does, on its avatar."""
    data = await _get_json(f'https://api.chub.ai/api/characters/{card_id}?full=true', CHUB_HOSTS)
    node = (data or {}).get('node') if isinstance(data, dict) else None
    definition = (node or {}).get('definition')
    if not isinstance(definition, dict):
        raise SourceError('Chub returned no character definition.')
    card = {
        'spec': 'chara_card_v2',
        'spec_version': '2.0',
        'data': {
            'name': _text(definition.get('name')) or _text(node.get('name')),
            'description': definition.get('personality') or '',
            'personality': definition.get('tavern_personality') or '',
            'scenario': definition.get('scenario') or '',
            'first_mes': definition.get('first_message') or '',
            'mes_example': definition.get('example_dialogs') or '',
            'creator_notes': definition.get('description') or '',
            'system_prompt': definition.get('system_prompt') or '',
            'post_history_instructions': definition.get('post_history_instructions') or '',
            'alternate_greetings': _strings(definition.get('alternate_greetings')),
            'tags': _strings(node.get('topics')),
            'creator': card_id.split('/')[0],
            'character_version': '',
            'character_book': definition.get('embedded_lorebook') or None,
            'extensions': definition.get('extensions') if isinstance(definition.get('extensions'), dict) else {},
        },
    }
    if not card['data']['character_book']:
        del card['data']['character_book']
    card_json = json.dumps(card, ensure_ascii=False)
    image_url = _text(node.get('max_res_url')) or f'https://avatars.charhub.io/avatars/{card_id}/chara_card_v2.png'
    try:
        image, _ = await _get(image_url, CHUB_HOSTS, CHARACTER_CARD_MAX_BYTES)
        png = _to_png(image)
    except SourceError as error:
        log.info('Importing %s without its avatar: %s', card_id, error)
        png = None
    if png is None:
        return card_json.encode('utf-8'), 'card.json'
    encoded = base64.b64encode(card_json.encode('utf-8')).decode('ascii')
    return write_png_text_chunks(png, {'chara': encoded}), 'card.png'


####################
# RisuRealm (realm.risuai.net)
####################

RISU_HOSTS = ('realm.risuai.net', 'sv.risuai.xyz')
RISU_ID = re.compile(r'^[0-9a-f-]{20,80}$', re.IGNORECASE)
RISU_SORTS = {'popular': 'downloads', 'newest': '', 'trending': 'trending'}


def _risu_summary(card: dict) -> CardSummary | None:
    card_id = _text(card.get('id'))
    if not RISU_ID.match(card_id) or (card.get('type') not in (None, 'normal', 'character')):
        return None
    tags = _strings(card.get('tags'))
    image = _text(card.get('img'))
    return CardSummary(
        source='risu',
        id=card_id,
        name=_text(card.get('name')) or card_id,
        tagline=_text(card.get('desc'))[:400],
        creator=_text(card.get('authorname')),
        tags=[tag for tag in tags if tag != 'nsfw'][:40],
        image_url=f'https://sv.risuai.xyz/resource/{image}' if re.fullmatch(r'[\w.-]+', image) else '',
        page_url=f'https://realm.risuai.net/character/{card_id}',
        nsfw='nsfw' in tags,
        has_lorebook=bool(card.get('haslore')),
        downloads=_text(card.get('download')),
    )


async def _risu_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    # RisuRealm's search syntax: "tag:x" requires a tag (exclusions are filtered afterwards).
    tag_terms = ['tag:' + re.sub(r'\s+', '-', tag) for tag in tags.include]
    query = ' '.join([query, *tag_terms]).strip()
    params = {'q': query, 'page': page, 'sort': RISU_SORTS.get(sort, 'downloads'), 'nsfw': 'true' if nsfw else ''}
    params = {key: value for key, value in params.items() if value not in ('', None)}
    root = await _sveltekit_data(f'https://realm.risuai.net/__data.json?{urlencode(params)}', RISU_HOSTS, 'cards')
    cards = [card for card in root.get('cards') or [] if isinstance(card, dict)]
    items = [item for item in (_risu_summary(card) for card in cards) if item]
    if not nsfw:
        items = [item for item in items if not item.nsfw]
    return SearchPage(items=items, page=page, has_more=len(cards) >= 20)


async def _risu_download(card_id: str) -> tuple[bytes, str]:
    # Creators choose which formats they allow; try the smallest complete one first.
    last_error: SourceError | None = None
    for fmt, filename in (('png-v3', 'card.png'), ('json-v3', 'card.json'), ('charx-v3', 'card.charx')):
        try:
            body, _ = await _get(
                f'https://realm.risuai.net/api/v1/download/{fmt}/{card_id}?non_commercial=true',
                RISU_HOSTS,
                CHARACTER_CARD_MAX_BYTES,
            )
            return body, filename
        except SourceError as error:
            last_error = error
    if last_error is not None and 'too large' in str(last_error):
        limit = CHARACTER_CARD_MAX_BYTES // (1024 * 1024)
        raise SourceError(
            f'RisuRealm only offers this card as a download over the {limit} MB import limit '
            '(CHARACTER_CARD_MAX_BYTES).'
        )
    raise SourceError(f'RisuRealm would not provide this card ({last_error}).')


####################
# Character Tavern (character-tavern.com)
####################

TAVERN_HOSTS = ('character-tavern.com',)
TAVERN_ID = re.compile(r'^[\w.-]+/[\w.-]+$')
TAVERN_SORTS = {'popular': '', 'newest': 'newest', 'trending': ''}


def _tavern_summary(hit: dict) -> CardSummary | None:
    path = _text(hit.get('path'))
    if not TAVERN_ID.match(path):
        return None
    warnings = _strings(hit.get('contentWarnings'))
    return CardSummary(
        source='tavern',
        id=path,
        name=_text(hit.get('name')) or path.split('/')[-1],
        tagline=_text(hit.get('tagline'))[:400],
        creator=_text(hit.get('author')) or path.split('/')[0],
        tags=warnings[:12],
        image_url=f'https://cards.character-tavern.com/cdn-cgi/image/format=auto,width=256/{path}.png',
        page_url=f'https://character-tavern.com/character/{path}',
        nsfw=bool(warnings),
        tokens=hit.get('permanentTokens') if isinstance(hit.get('permanentTokens'), int) else None,
    )


async def _tavern_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    params = {
        'query': query,
        'page': page if page > 1 else '',
        'sort': TAVERN_SORTS.get(sort, ''),
        'tags': ','.join(tags.include),
        'exclude_tags': ','.join(tags.exclude),
    }
    params = {key: value for key, value in params.items() if value not in ('', None)}
    root = await _sveltekit_data(
        f'https://character-tavern.com/search/cards/__data.json?{urlencode(params)}', TAVERN_HOSTS, 'searchResults'
    )
    results = root.get('searchResults') or {}
    hits = [hit for hit in results.get('hits') or [] if isinstance(hit, dict)]
    items = [item for item in (_tavern_summary(hit) for hit in hits) if item]
    if not nsfw:
        items = [item for item in items if not item.nsfw]
    total_pages = results.get('totalPages') if isinstance(results.get('totalPages'), int) else 0
    return SearchPage(items=items, page=page, has_more=page < total_pages)


async def _tavern_download(card_id: str) -> tuple[bytes, str]:
    body, _ = await _get(
        f'https://cards.character-tavern.com/{card_id}.png?action=download', TAVERN_HOSTS, CHARACTER_CARD_MAX_BYTES
    )
    return body, 'card.png'


####################
# Poe (poe.com) bots
####################

# Poe has no character cards and no public search: a bot is a name, description, greeting and,
# when its creator made it public, a prompt. Import works from a bot's link; the prompt goes
# through the same converter as "convert a system prompt".
POE_HOSTS = ('poe.com', 'poecdn.net')
POE_ID = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$')  # Poe handles may start with '-' ('-RPG-')
POE_RESERVED = {
    'api', 'bots', 'chat', 'chats', 'create_bot', 'edit_bot', 'explore', 'login', 'profile', 'search', 'settings',
    'subscribe', 'about', 'privacy_policy', 'tos', 'apps', 'creators', 'notifications',
}  # fmt: skip
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


async def _poe_bot(handle: str) -> dict:
    body, _ = await _get(f'https://poe.com/{handle}', POE_HOSTS, SEARCH_MAX_BYTES)
    match = NEXT_DATA_RE.search(body.decode('utf-8', 'replace'))
    try:
        data = json.loads(match.group(1)) if match else {}
    except json.JSONDecodeError:
        data = {}
    page = ((data.get('props') or {}).get('pageProps') or {}).get('data') or {}
    bot = (page.get('mainQuery') or {}).get('bot')
    if not isinstance(bot, dict) or bot.get('isDeleted'):
        raise SourceError(f'Poe has no bot named "{handle}".')
    return bot


def poe_card(bot: dict) -> dict:
    """A V2 card from a Poe bot: from its prompt when public, else from its description and greeting."""
    from open_webui.utils.roleplay.convert import parse_system_prompt

    handle = _text(bot.get('handle'))
    display_name = _text(bot.get('displayName')) or handle
    description = _text(bot.get('description'))
    introduction = _text(bot.get('introduction'))
    prompt = _text(bot.get('promptPlaintext')) if bot.get('isPromptPublic') else ''
    link = f'Imported from Poe: https://poe.com/{handle}'

    if prompt:
        character = parse_system_prompt(prompt, display_name).character
        notes = [description, link]
    elif description or introduction:
        # The prompt is private: the public description is the best available picture of the character.
        character = {'name': display_name, 'description': description}
        notes = [
            link,
            'This bot keeps its prompt private on Poe, so this character was built from its public '
            'description and greeting only. Add personality, scenario and example dialogue in the editor.',
        ]
    else:
        raise SourceError(
            f'"{handle}" has a private prompt and no public description or greeting on Poe, so there is '
            'nothing to import.'
        )

    system_prompt = character.get('system_prompt') or ''
    return {
        'spec': 'chara_card_v2',
        'spec_version': '2.0',
        'data': {
            'name': character.get('name') or display_name,
            'description': character.get('description') or '',
            'personality': character.get('personality') or '',
            'scenario': character.get('scenario') or '',
            'first_mes': character.get('first_mes') or introduction,
            'mes_example': character.get('mes_example') or '',
            'creator_notes': '\n\n'.join(part for part in notes if part),
            # The author's rules supplement the default roleplay instruction rather than replace it.
            'system_prompt': f'{{{{original}}}}\n\n{system_prompt}' if system_prompt else '',
            'post_history_instructions': character.get('post_history_instructions') or '',
            'alternate_greetings': [],
            'tags': ['Poe'],
            'creator': _text((bot.get('creator') or {}).get('handle')),
            'character_version': '',
            'extensions': {'poe': {'handle': handle, 'model': _text(bot.get('model')), 'prompt_public': bool(prompt)}},
        },
    }


POE_LOOKUP_LIMIT = 12


def _poe_handles(query: str) -> list[str]:
    """Bot handles from pasted Poe links and/or bot names, in order, without duplicates."""
    handles: list[str] = []
    for token in re.split(r'[\s,]+', query or ''):
        token = token.strip().strip('<>()[]"\'')
        if not token:
            continue
        if '/' in token or token.lower().startswith('poe.com'):
            link = parse_card_link(token if '://' in token else f'https://{token}')
            handle = link[1] if link and link[0] == 'poe' else None
        else:
            handle = token.lstrip('@')
        if handle and POE_ID.match(handle) and handle.lower() not in POE_RESERVED and handle not in handles:
            handles.append(handle)
    return handles[:POE_LOOKUP_LIMIT]


def _poe_summary(bot: dict) -> CardSummary:
    handle = _text(bot.get('handle'))
    prompt_public = bool(bot.get('isPromptPublic') and _text(bot.get('promptPlaintext')))
    picture = _text((bot.get('picture') or {}).get('url'))
    return CardSummary(
        source='poe',
        id=handle,
        name=_text(bot.get('displayName')) or handle,
        tagline=_text(bot.get('description'))[:400],
        creator=_text((bot.get('creator') or {}).get('handle')),
        tags=['public prompt' if prompt_public else 'private prompt'],
        image_url=picture if _allowed(picture, POE_HOSTS) else '',
        page_url=f'https://poe.com/{handle}',
    )


async def _poe_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    """Poe's own search needs a signed-in, signed request, so this looks up the bots the user
    names instead: pasted links or bot names, several at once."""
    handles = _poe_handles(query) if page == 1 else []
    results = await asyncio.gather(*(_poe_bot(handle) for handle in handles), return_exceptions=True)
    items = []
    for handle, result in zip(handles, results):
        if isinstance(result, Exception):
            log.info('Poe lookup of %s failed: %s', handle, result)
        else:
            items.append(_poe_summary(result))
    if handles and not items:
        raise SourceError('None of those Poe bots could be found. Check the names or links.')
    return SearchPage(items=items, page=page, has_more=False)


async def _poe_download(handle: str) -> tuple[bytes, str]:
    if handle.lower() in POE_RESERVED:
        raise SourceError('That is not a Poe bot link.')
    bot = await _poe_bot(handle)
    card_json = json.dumps(poe_card(bot), ensure_ascii=False)
    picture = _text((bot.get('picture') or {}).get('url'))
    png = None
    if picture and _allowed(picture, POE_HOSTS):
        try:
            image, _ = await _get(picture, POE_HOSTS, CHARACTER_CARD_MAX_BYTES)
            png = _to_png(image)
        except SourceError as error:
            log.info('Importing Poe bot %s without its picture: %s', handle, error)
    if png is None:
        return card_json.encode('utf-8'), 'card.json'
    encoded = base64.b64encode(card_json.encode('utf-8')).decode('ascii')
    return write_png_text_chunks(png, {'chara': encoded}), 'card.png'


####################
# Registry
####################


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    id_pattern: re.Pattern
    search: object
    download: object
    nsfw_filter: bool  # whether the site itself can include adult cards on request
    searchable: bool = True  # False: import by link only
    tags_in_results: bool = True  # results list the card's tags, so tag filters can be checked here
    supports_tags: bool = True


SOURCES: dict[str, Source] = {
    source.id: source
    for source in (
        Source('chub', 'Chub', 'https://chub.ai', CHUB_ID, _chub_search, _chub_download, True),
        Source('risu', 'RisuRealm', 'https://realm.risuai.net', RISU_ID, _risu_search, _risu_download, True),
        Source(
            'tavern',
            'Character Tavern',
            'https://character-tavern.com',
            TAVERN_ID,
            _tavern_search,
            _tavern_download,
            False,
            tags_in_results=False,  # only content warnings come back; the site filters by tag itself
        ),
        Source('poe', 'Poe', 'https://poe.com', POE_ID, _poe_search, _poe_download, False, supports_tags=False),
    )
}


def get_source(source_id: str) -> Source:
    source = SOURCES.get(source_id)
    if source is None:
        raise SourceError('Unknown card source.')
    return source


async def search_cards(
    source_id: str,
    query: str = '',
    page: int = 1,
    sort: str = 'popular',
    nsfw: bool = False,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
) -> SearchPage:
    source = get_source(source_id)
    tag_filter = TagFilter(clean_tags(tags), clean_tags(exclude_tags)) if source.supports_tags else TagFilter()
    result = await source.search(
        query.strip()[:200], max(1, page), sort if sort in SORTS else 'popular', nsfw, tag_filter
    )
    if source.tags_in_results and (tag_filter.include or tag_filter.exclude):
        # Sites apply tags their own way (or not at all); make sure every result matches.
        result.items = [item for item in result.items if tag_filter.matches(item.tags)]
    return result


# Recent downloads, so importing a card right after previewing it does not fetch it again.
CACHE_TTL = 600
CACHE_MAX_ITEMS = 16
CACHE_MAX_BYTES = 200 * 1024 * 1024
_download_cache: OrderedDict[tuple[str, str], tuple[float, bytes, str]] = OrderedDict()


def _cache_get(key: tuple[str, str]) -> tuple[bytes, str] | None:
    entry = _download_cache.get(key)
    if entry is None or time.monotonic() - entry[0] > CACHE_TTL:
        _download_cache.pop(key, None)
        return None
    _download_cache.move_to_end(key)
    return entry[1], entry[2]


def _cache_put(key: tuple[str, str], data: bytes, filename: str) -> None:
    _download_cache[key] = (time.monotonic(), data, filename)
    _download_cache.move_to_end(key)
    while len(_download_cache) > CACHE_MAX_ITEMS or (
        len(_download_cache) > 1 and sum(len(entry[1]) for entry in _download_cache.values()) > CACHE_MAX_BYTES
    ):
        _download_cache.popitem(last=False)


async def download_card(source_id: str, card_id: str) -> tuple[bytes, str]:
    source = get_source(source_id)
    if not source.id_pattern.match(card_id or ''):
        raise SourceError('That is not a valid card id for this site.')
    key = (source.id, card_id)
    if cached := _cache_get(key):
        return cached
    data, filename = await source.download(card_id)
    _cache_put(key, data, filename)
    return data, filename


def parse_card_link(url: str) -> tuple[str, str] | None:
    """``(source, id)`` for a card page link on one of the sites, else ``None``."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or '').lower().removeprefix('www.')
    parts = [part for part in parsed.path.split('/') if part]
    if host in ('chub.ai', 'venus.chub.ai', 'characterhub.org') and len(parts) >= 3 and parts[0] == 'characters':
        card_id = '/'.join(parts[1:3])
        return ('chub', card_id) if CHUB_ID.match(card_id) else None
    if host == 'realm.risuai.net' and len(parts) >= 2 and parts[0] == 'character':
        return ('risu', parts[1]) if RISU_ID.match(parts[1]) else None
    if host == 'poe.com' and len(parts) == 1 and parts[0].lower() not in POE_RESERVED:
        return ('poe', parts[0]) if POE_ID.match(parts[0]) else None
    if host == 'character-tavern.com' and len(parts) >= 3 and parts[0] == 'character':
        card_id = '/'.join(parts[1:3])
        return ('tavern', card_id) if TAVERN_ID.match(card_id) else None
    return None
