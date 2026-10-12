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
    return await _card_file(poe_card(bot), _text((bot.get('picture') or {}).get('url')), POE_HOSTS)


async def _card_file(card: dict, image_url: str, hosts: tuple[str, ...]) -> tuple[bytes, str]:
    """A card built from a site's API data, as a PNG card with its picture when there is one."""
    card_json = json.dumps(card, ensure_ascii=False)
    png = None
    if image_url and _allowed(image_url, hosts):
        try:
            image, _ = await _get(image_url, hosts, CHARACTER_CARD_MAX_BYTES)
            png = _to_png(image)
        except SourceError as error:
            log.info('Importing %s without its picture: %s', card.get('data', {}).get('name'), error)
    if png is None:
        return card_json.encode('utf-8'), 'card.json'
    encoded = base64.b64encode(card_json.encode('utf-8')).decode('ascii')
    return write_png_text_chunks(png, {'chara': encoded}), 'card.png'


def _v2_card(**data) -> dict:
    """A V2 card from the fields given; the rest empty."""
    fields = {
        'name': '',
        'description': '',
        'personality': '',
        'scenario': '',
        'first_mes': '',
        'mes_example': '',
        'creator_notes': '',
        'system_prompt': '',
        'post_history_instructions': '',
        'alternate_greetings': [],
        'tags': [],
        'creator': '',
        'character_version': '',
        'extensions': {},
    }
    fields.update({key: value for key, value in data.items() if value is not None})
    return {'spec': 'chara_card_v2', 'spec_version': '2.0', 'data': fields}


####################
# Pygmalion (pygmalion.chat): public Connect-RPC API, JSON over GET
####################

PYG_API = 'https://server.pygmalion.chat/galatea.v1.PublicCharacterService'
PYG_HOSTS = ('pygmalion.chat',)
PYG_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
PYG_SORTS = {'popular': 'downloads', 'newest': 'approved_at', 'trending': 'stars'}


async def _pyg_call(method: str, message: dict):
    query = urlencode({'connect': 'v1', 'encoding': 'json', 'message': json.dumps(message)})
    return await _get_json(f'{PYG_API}/{method}?{query}', PYG_HOSTS)


def _pyg_summary(character: dict) -> CardSummary | None:
    card_id = _text(character.get('id'))
    if not PYG_ID.match(card_id):
        return None
    owner = character.get('owner') or {}
    tokens = character.get('personalityTokenCount')
    return CardSummary(
        source='pygmalion',
        id=card_id,
        name=_text(character.get('displayName')) or 'Unnamed',
        tagline=_text(character.get('description'))[:300],
        creator=_text(owner.get('displayName')) or _text(owner.get('username')),
        tags=_strings(character.get('tags')),
        image_url=_text(character.get('avatarUrl')),
        page_url=f'https://pygmalion.chat/character/{card_id}',
        nsfw=bool(character.get('isSensitive')),
        tokens=int(tokens) if str(tokens or '').isdigit() else None,
        downloads=str(character.get('downloads') or ''),
    )


async def _pyg_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    message = {
        'query': query,
        'page': page - 1,
        'pageSize': PAGE_SIZE,
        'orderBy': PYG_SORTS[sort],
        'orderDescending': True,
        'includeSensitive': nsfw,
    }
    if tags.include:
        message['tagsNamesInclude'] = tags.include
        message['tagMatchCountNeeded'] = len(tags.include)
    if tags.exclude:
        message['tagsNamesExclude'] = tags.exclude
    data = await _pyg_call('CharacterSearch', message)
    characters = data.get('characters') if isinstance(data, dict) else None
    items = [item for c in characters or [] if isinstance(c, dict) and (item := _pyg_summary(c))]
    try:
        total = int(data.get('totalItems') or 0)
    except (TypeError, ValueError):
        total = 0
    return SearchPage(items=items, page=page, has_more=page * PAGE_SIZE < total)


def pygmalion_card(character: dict) -> dict:
    """A V2 card from a Pygmalion character, mapped as the site's own card export does."""
    personality = character.get('personality') or {}
    notes = _text(personality.get('characterNotes'))
    extensions = {'pygmalion_id': _text(character.get('id'))}
    if notes:
        extensions['depth_prompt'] = {'prompt': notes, 'depth': 4, 'role': 'system'}
    return _v2_card(
        name=_text(character.get('displayName')) or _text(personality.get('name')),
        description=_text(personality.get('persona')),
        first_mes=_text(personality.get('greeting')),
        alternate_greetings=_strings(personality.get('alternateGreetings')),
        mes_example=_text(personality.get('mesExample')),
        system_prompt=_text(personality.get('systemPrompt')),
        post_history_instructions=_text(personality.get('postHistoryInstructions')),
        creator=_text(personality.get('creator')) or _text((character.get('owner') or {}).get('displayName')),
        character_version=_text(personality.get('characterVersion')),
        creator_notes=_text(character.get('description')),
        tags=_strings(character.get('tags')),
        extensions=extensions,
    )


async def _pyg_download(card_id: str) -> tuple[bytes, str]:
    try:
        data = await _pyg_call('Character', {'characterMetaId': card_id})
    except SourceError as error:
        if '500' in str(error):
            raise SourceError('Pygmalion has no public character with that id.') from error
        raise
    character = data.get('character') if isinstance(data, dict) else None
    if not isinstance(character, dict):
        raise SourceError('Pygmalion returned no character.')
    return await _card_file(pygmalion_card(character), _text(character.get('avatarUrl')), PYG_HOSTS)


####################
# Backyard AI hub (backyard.ai): the hub's public tRPC API
####################

BACKYARD_API = 'https://backyard.ai/api/trpc/hub.browse'
BACKYARD_HOSTS = ('backyard.ai',)
BACKYARD_ID = re.compile(r'^c[a-z0-9]{24}$')
BACKYARD_PAGE = 50  # the hub's own page size; its cursor is an offset
BACKYARD_SORTS = {'popular': 'Popularity', 'newest': 'Newest', 'trending': 'Trending'}
# Character placeholders: {_cfg&:<id>:cfg&_} is the character, {user} the user.
BACKYARD_CHAR_RE = re.compile(r'\{_cfg&:[^:}]*:cfg&_\}')


async def _backyard_call(procedure: str, payload: dict):
    url = f'{BACKYARD_API}.{procedure}?' + urlencode({'input': json.dumps({'json': payload})})
    data = await _get_json(url, BACKYARD_HOSTS)
    try:
        return data['result']['data']['json']
    except (TypeError, KeyError) as error:
        raise SourceError('The Backyard hub returned something unexpected.') from error


def _backyard_text(text) -> str:
    text = BACKYARD_CHAR_RE.sub('{{char}}', _text(text))
    return re.sub(r'(?<!\{)\{user\}(?!\})', '{{user}}', text)


def _backyard_summary(group: dict) -> CardSummary | None:
    characters = [c for c in group.get('CharacterConfigs') or [] if isinstance(c, dict)]
    card_id = _text(characters[0].get('id')) if characters else _text(group.get('id'))
    if not BACKYARD_ID.match(card_id):
        return None
    images = (characters[0].get('Images') if characters else None) or []
    image = next((_text(i.get('imageUrl')) for i in images if isinstance(i, dict) and i.get('imageUrl')), '')
    return CardSummary(
        source='backyard',
        id=card_id,
        name=_text(group.get('name')) or (_text(characters[0].get('name')) if characters else 'Unnamed'),
        tagline=_text(group.get('tagline'))[:300],
        creator=_text((group.get('Author') or {}).get('username')),
        tags=[_text(tag.get('name')) for tag in group.get('Tags') or [] if isinstance(tag, dict) and tag.get('name')],
        image_url=image,
        page_url=f'https://backyard.ai/hub/character/{card_id}',
        nsfw=bool(group.get('isNSFW')),
        downloads=str(group.get('downloadCount') or ''),
    )


async def _backyard_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    payload: dict = {'sortBy': {'type': BACKYARD_SORTS[sort], 'direction': 'desc'}}
    if page > 1:
        payload['cursor'] = (page - 1) * BACKYARD_PAGE
    if len(query) >= 3:
        procedure = 'getHubGroupConfigsBySearch'
        payload['query'] = query
        if sort == 'popular':
            payload['sortBy']['type'] = 'Relevance'
    elif tags.include:
        procedure = 'getHubGroupConfigsForTag'
        payload.update(tagNames=[tag.lower() for tag in tags.include], type='all')
    else:
        procedure = 'getHubGroupConfigsBySortType'
    data = await _backyard_call(procedure, payload)
    groups = data.get('hubGroupConfigs') if isinstance(data, dict) else None
    items = [item for g in groups or [] if isinstance(g, dict) and (item := _backyard_summary(g))]
    return SearchPage(items=items, page=page, has_more=bool(isinstance(data, dict) and data.get('nextCursor')))


def backyard_card(character: dict) -> dict:
    """A V2 card from a Backyard hub character and its own chat setup (scenario, greetings, examples)."""
    group = character.get('standaloneGroupConfig') or {}
    chat = group.get('PrimaryChat') or {}
    greetings = [
        _backyard_text(message.get('text'))
        for message in sorted(
            (m for m in chat.get('HubGreetingMessages') or [] if isinstance(m, dict)),
            key=lambda m: str(m.get('position') or ''),
        )
        if _text(message.get('text'))
    ]
    examples = []
    for message in sorted(
        (m for m in chat.get('HubExampleMessages') or [] if isinstance(m, dict)),
        key=lambda m: str(m.get('position') or ''),
    ):
        speaker = '{{user}}' if message.get('hubCharacterConfigId') == 'global-user-persona' else '{{char}}'
        if _text(message.get('text')):
            examples.append(f'{speaker}: {_backyard_text(message.get("text"))}')
    instructions = chat.get('modelInstructions') or {}
    system_prompt = _text(instructions.get('customText')) if instructions.get('type') == 'custom' else ''
    entries = [
        {
            'keys': [key.strip() for key in _text(item.get('key')).split(',') if key.strip()],
            'content': _backyard_text(item.get('value')),
            'enabled': True,
            'insertion_order': index,
        }
        for index, item in enumerate(character.get('LorebookItems') or [])
        if isinstance(item, dict) and _text(item.get('value'))
    ]
    card = _v2_card(
        name=_text(character.get('name')) or _text(character.get('displayName')),
        description=_backyard_text(character.get('persona')),
        scenario=_backyard_text(chat.get('context')),
        first_mes=greetings[0] if greetings else '',
        alternate_greetings=greetings[1:],
        mes_example='<START>\n' + '\n'.join(examples) if examples else '',
        system_prompt=f'{{{{original}}}}\n\n{_backyard_text(system_prompt)}' if system_prompt else '',
        creator_notes=_text(character.get('creatorNotes')) or _text(character.get('tagline')),
        tags=[_text(t.get('name')) for t in character.get('Tags') or [] if isinstance(t, dict) and t.get('name')],
        creator=_text((character.get('Author') or {}).get('username')),
        extensions={'backyard_id': _text(character.get('id'))},
    )
    if entries:
        card['data']['character_book'] = {'name': card['data']['name'], 'entries': entries}
    return card


async def _backyard_download(card_id: str) -> tuple[bytes, str]:
    character = await _backyard_call(
        'getHubCharacterConfigById', {'hubCharacterConfigId': card_id, 'includeStandaloneGroupConfig': True}
    )
    if not isinstance(character, dict) or character.get('notFound') or not character.get('id'):
        raise SourceError('The Backyard hub has no public character with that id.')
    if character.get('banned'):
        raise SourceError('That character was removed from the Backyard hub.')
    images = sorted(
        (i for i in character.get('Images') or [] if isinstance(i, dict) and i.get('imageUrl')),
        key=lambda i: i.get('order') or 0,
    )
    return await _card_file(backyard_card(character), _text(images[0]['imageUrl']) if images else '', BACKYARD_HOSTS)


####################
# Wyvern (app.wyvern.chat): public REST API
####################

WYVERN_API = 'https://api.wyvern.chat'
WYVERN_HOSTS = ('api.wyvern.chat', 'imagedelivery.net')
WYVERN_ID = re.compile(r'^_[A-Za-z0-9_-]{21}$')
WYVERN_SORTS = {'popular': 'votes', 'newest': 'created_at', 'trending': 'popular'}


def _wyvern_summary(character: dict) -> CardSummary | None:
    card_id = _text(character.get('id'))
    if not WYVERN_ID.match(card_id):
        return None
    tokens = character.get('token_count')
    return CardSummary(
        source='wyvern',
        id=card_id,
        name=_text(character.get('chat_name')) or _text(character.get('name')) or 'Unnamed',
        tagline=(_text(character.get('tagline')) or _text(character.get('creator_notes')))[:300],
        creator=_text((character.get('creator') or {}).get('displayName')),
        tags=_strings(character.get('tags')),
        image_url=_text(character.get('avatar')),
        page_url=f'https://app.wyvern.chat/characters/{card_id}',
        nsfw=_text(character.get('rating')) not in ('', 'none'),
        has_lorebook=bool(character.get('lorebooks')),
        tokens=tokens if isinstance(tokens, int) else None,
    )


async def _wyvern_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    params = {
        'q': query,
        'page': page,
        'limit': PAGE_SIZE,
        'sort': 'relevance' if query and sort == 'popular' else WYVERN_SORTS[sort],
        'order': 'DESC',
    }
    if tags.include:
        params['tags'] = ','.join(tags.include)
    if not nsfw:
        params['rating'] = 'none'
    data = await _get_json(f'{WYVERN_API}/exploreSearch/characters?{urlencode(params)}', WYVERN_HOSTS)
    results = data.get('results') if isinstance(data, dict) else None
    items = [item for c in results or [] if isinstance(c, dict) and (item := _wyvern_summary(c))]
    return SearchPage(items=items, page=page, has_more=bool(isinstance(data, dict) and data.get('hasMore')))


def wyvern_card(character: dict) -> dict:
    """A V2 card from a Wyvern character; its lorebooks are merged into the card's book."""
    entries = []
    for book in character.get('lorebooks') or []:
        # Newer books keep their entries in ``lexicon`` (same entry fields), older ones in ``entries``.
        book = book if isinstance(book, dict) else {}
        for entry in [*(book.get('entries') or []), *(book.get('lexicon') or [])]:
            if isinstance(entry, dict) and _text(entry.get('content')):
                entries.append({**entry, 'id': len(entries)})
    note = _text(character.get('character_note'))
    extensions = {'wyvern_id': _text(character.get('id'))}
    if note:
        extensions['depth_prompt'] = {'prompt': note, 'depth': 4, 'role': 'system'}
    card = _v2_card(
        name=_text(character.get('name')) or _text(character.get('chat_name')),
        nickname=_text(character.get('chat_name')) or None,
        description=_text(character.get('description')),
        personality=_text(character.get('personality')),
        scenario=_text(character.get('scenario')),
        first_mes=_text(character.get('first_mes')),
        alternate_greetings=_strings(character.get('alternate_greetings')),
        mes_example=_text(character.get('mes_example')),
        system_prompt=_text(character.get('pre_history_instructions')),
        post_history_instructions=_text(character.get('post_history_instructions')),
        creator_notes=_text(character.get('creator_notes')) or _text(character.get('tagline')),
        tags=_strings(character.get('tags')),
        creator=_text((character.get('creator') or {}).get('displayName')),
        extensions=extensions,
    )
    if entries:
        card['data']['character_book'] = {'name': card['data']['name'], 'entries': entries}
    return card


async def _wyvern_download(card_id: str) -> tuple[bytes, str]:
    try:
        character = await _get_json(f'{WYVERN_API}/characters/{card_id}', WYVERN_HOSTS)
    except SourceError as error:
        if '404' in str(error):
            raise SourceError('Wyvern has no public character with that id.') from error
        raise
    if not isinstance(character, dict) or not character.get('id'):
        raise SourceError('Wyvern returned no character.')
    return await _card_file(wyvern_card(character), _text(character.get('avatar')), WYVERN_HOSTS)


####################
# JannyAI (jannyai.com): public JanitorAI characters as cards. The download is the endpoint
# SillyTavern's importer uses. Search is best effort: the site's Meilisearch key is read from its own
# pages, which its bot protection often hides from servers (less so from home connections).
####################

JANNY_HOSTS = ('jannyai.com',)
JANNY_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


JANNY_SEARCH_HOST = 'search.jannyai.com'
JANNY_UNAVAILABLE = (
    'JannyAI’s bot protection blocked this server from reading its search setup. An admin can add the '
    'site’s search key in Admin Settings > General (JannyAI search key) to search it anyway.'
)
JANNY_SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+\.js)["\']|["\'](/_(?:astro|next)/[^"\']+\.js)["\']')
JANNY_KEY_RES = (
    # apiKey: "...", api_key = '...', "Authorization": "Bearer ..."
    re.compile(r'(?:apiKey|api_key|searchKey|key)["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{24,128})["\']'),
    re.compile(r'Bearer ([A-Za-z0-9_-]{24,128})'),
)
JANNY_INDEX_RES = (
    re.compile(r'indexUid["\']?\s*[:=]\s*["\']([\w-]{2,64})["\']'),
    re.compile(r'\.index\(\s*["\']([\w-]{2,64})["\']\s*\)'),
    re.compile(r'/indexes/([\w-]{2,64})/search'),
)
JANNY_INDEX_GUESSES = ('janny-characters', 'characters', 'character')
JANNY_CONFIG_TTL = 6 * 3600
_janny_config: dict = {}  # {'key', 'index', 'at'} once found


def find_janny_search_config(texts: list[str]) -> tuple[str | None, str | None]:
    """The Meilisearch key and index name from the site's page and scripts, as far as they show them.

    A key counts only from text that also names the search host, so other keys in the bundle are not
    mistaken for it.
    """
    key = index = None
    for text in texts:
        # Look next to each mention of the search host first: the client is set up with both together.
        for position in (m.start() for m in re.finditer(re.escape(JANNY_SEARCH_HOST), text)):
            if key is not None:
                break
            window = text[max(0, position - 400) : position + 400]
            for pattern in JANNY_KEY_RES:
                match = pattern.search(window)
                if match:
                    key = match.group(1)
                    break
        if index is None:
            for pattern in JANNY_INDEX_RES:
                match = pattern.search(text)
                if match:
                    index = match.group(1)
                    break
    return key, index


def janny_script_urls(page: str) -> list[str]:
    """The page's own script files, in order."""
    urls: list[str] = []
    for match in JANNY_SCRIPT_RE.finditer(page):
        src = match.group(1) or match.group(2)
        url = src if src.startswith('https://') else f'https://jannyai.com{src}' if src.startswith('/') else ''
        if url and url not in urls:
            urls.append(url)
    return urls


async def _janny_text(url: str) -> str:
    body, _ = await _get(url, JANNY_HOSTS, 4 * 1024 * 1024)
    return body.decode('utf-8', 'replace')


async def _janny_meili(path: str, key: str, payload: dict | None = None) -> tuple[int, object]:
    session = await get_session()
    headers = {**HEADERS, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    url = f'https://{JANNY_SEARCH_HOST}{path}'
    try:
        request = (
            session.post(url, json=payload, headers=headers, timeout=TIMEOUT, allow_redirects=False)
            if (payload is not None)
            else session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False)
        )
        async with request as response:
            body = await _read_capped(response, SEARCH_MAX_BYTES)
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, None
    except aiohttp.ClientError as error:
        raise SourceError(f'Could not reach JannyAI search: {error}') from error
    except TimeoutError as error:
        raise SourceError('JannyAI search did not answer in time.') from error


async def _janny_search_config() -> tuple[str, str]:
    """Find (and cache) the site's public search key and index, as its own pages use them."""
    from open_webui.models.config import Config

    configured_key = (await Config.get('characters.sources.janny.search_key') or '').strip()
    configured_index = (await Config.get('characters.sources.janny.search_index') or '').strip()
    if _janny_config.get('key') and time.monotonic() - _janny_config.get('at', 0) < JANNY_CONFIG_TTL:
        if not configured_key or _janny_config['key'] == configured_key:
            return _janny_config['key'], _janny_config['index']
    if configured_key:
        # Set by the admin (copied from the site in a browser): no need to read the protected pages.
        return await _janny_pick_index(configured_key, configured_index)
    try:
        page = await _janny_text('https://jannyai.com/characters/search')
    except SourceError as error:
        log.info('JannyAI search page unavailable: %s', error)
        raise SourceError(JANNY_UNAVAILABLE) from error
    texts = [page]
    key, index = find_janny_search_config(texts)
    scripts = janny_script_urls(page)
    for url in scripts[:40]:
        if key and index:
            break
        try:
            text = await _janny_text(url)
        except SourceError:
            continue
        found_key, found_index = find_janny_search_config([text])
        key, index = key or found_key, index or found_index
    if not key:
        log.info('JannyAI search key not found in %d scripts', len(scripts))
        raise SourceError(JANNY_UNAVAILABLE)
    return await _janny_pick_index(key, index or '')


async def _janny_pick_index(key: str, index: str) -> tuple[str, str]:
    """The first index name that the key can search (the given one, then common names)."""
    candidates = [index] if index else []
    candidates += [guess for guess in JANNY_INDEX_GUESSES if guess not in candidates]
    for candidate in candidates:
        status, _ = await _janny_meili('/multi-search', key, {'queries': [{'indexUid': candidate, 'limit': 1}]})
        if status == 200:
            _janny_config.update(key=key, index=candidate, at=time.monotonic())
            return key, candidate
        if status in (401, 403):
            log.info('JannyAI search key was refused (%s)', status)
            raise SourceError('JannyAI search refused the search key. Check it in Admin Settings > General.')
    log.info('JannyAI search index not found (tried %s)', candidates)
    raise SourceError(
        'JannyAI search did not find its index. Set the index name in Admin Settings > General '
        '(it is the "indexUid" in the site\'s search request).'
    )


# Recent search hits by character id: the index holds the definitions too, so a card can be built from
# a hit when the site's download endpoint is blocked.
_janny_hits: OrderedDict[str, dict] = OrderedDict()
JANNY_HITS_MAX = 500


def _janny_remember(card_id: str, hit: dict) -> None:
    _janny_hits[card_id] = hit
    _janny_hits.move_to_end(card_id)
    while len(_janny_hits) > JANNY_HITS_MAX:
        _janny_hits.popitem(last=False)


def janny_card(hit: dict) -> dict | None:
    """A V2 card from a search hit, when the hit carries the character's definition."""
    definition = _text(_first(hit, 'personality', 'definition', 'persona'))
    first_mes = _text(_first(hit, 'firstMessage', 'first_message', 'first_mes', 'greeting'))
    if not definition and not first_mes:
        return None
    name = _text(_first(hit, 'name', 'displayName', 'characterName'))
    summary = _janny_summary(hit)
    tags = summary.tags if summary else []
    return _v2_card(
        name=name,
        description=definition,
        scenario=_text(_first(hit, 'scenario')),
        first_mes=first_mes,
        mes_example=_text(_first(hit, 'exampleDialogs', 'example_dialogs', 'exampleDialogue', 'mes_example')),
        creator_notes=_text(_first(hit, 'description', 'tagline')),
        creator=_text(_first(hit, 'creatorName', 'creator', 'authorName', 'username')),
        tags=tags,
        extensions={'jannyai_id': _text(_first(hit, 'id', 'characterId', 'uuid')).lower()},
    )


def _first(hit: dict, *names: str):
    for name in names:
        value = hit.get(name)
        if value not in (None, '', []):
            return value
    return None


def _janny_slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:80] or 'character'


def _janny_summary(hit: dict) -> CardSummary | None:
    card_id = _text(_first(hit, 'id', 'characterId', 'uuid')).lower()
    if not JANNY_ID.match(card_id):
        return None
    name = _text(_first(hit, 'name', 'displayName', 'characterName')) or 'Unnamed'
    avatar = _text(_first(hit, 'avatar', 'avatarUrl', 'avatar_url', 'image', 'imageUrl'))
    if avatar and not avatar.startswith('https://'):
        avatar = f'https://image.jannyai.com/bot-avatars/{avatar.lstrip("/")}'
    raw_tags = _first(hit, 'tags', 'tagNames', 'tag_names') or []
    tags = [
        _text(tag.get('name')) if isinstance(tag, dict) else _text(tag)
        for tag in (raw_tags if isinstance(raw_tags, list) else [])
    ]
    tokens = _first(hit, 'totalToken', 'totalTokens', 'tokenCount', 'tokens')
    tagline = re.sub(r'<[^>]+>', ' ', _text(_first(hit, 'tagline', 'description', 'summary')))
    return CardSummary(
        source='janny',
        id=card_id,
        name=name,
        tagline=re.sub(r'\s+', ' ', tagline).strip()[:300],
        creator=_text(_first(hit, 'creatorName', 'creator', 'authorName', 'username')),
        tags=[tag for tag in tags if isinstance(tag, str) and tag and not tag.isdigit()],
        image_url=avatar,
        page_url=f'https://jannyai.com/characters/{card_id}_character-{_janny_slug(name)}',
        nsfw=bool(_first(hit, 'isNsfw', 'is_nsfw', 'nsfw')),
        tokens=tokens if isinstance(tokens, int) else None,
    )


async def _janny_search(query: str, page: int, sort: str, nsfw: bool, tags: TagFilter) -> SearchPage:
    """Best effort: the site's own Meilisearch search, with its public key read from its pages.

    The index layout is not published, so results are read leniently and filtered here (adult cards,
    tags) rather than with server-side filters that might name fields the index does not have.
    """
    key, index = await _janny_search_config()
    status, data = await _janny_meili(
        '/multi-search',
        key,
        {'queries': [{'indexUid': index, 'q': query, 'limit': PAGE_SIZE * 2, 'offset': (page - 1) * PAGE_SIZE * 2}]},
    )
    if status in (401, 403):
        _janny_config.clear()  # the key changed; find it again next time
        raise SourceError('JannyAI search refused the request. Try again in a moment.')
    if status != 200 or not isinstance(data, dict):
        raise SourceError(f'JannyAI search answered {status}.')
    result = (data.get('results') or [{}])[0] if isinstance(data.get('results'), list) else {}
    hits = result.get('hits') if isinstance(result, dict) else None
    items = []
    for hit in hits or []:
        item = _janny_summary(hit) if isinstance(hit, dict) else None
        if item:
            items.append(item)
            _janny_remember(item.id, hit)
    if not nsfw:
        items = [item for item in items if not item.nsfw]
    total = result.get('estimatedTotalHits') or result.get('totalHits') or 0
    has_more = len(hits or []) == PAGE_SIZE * 2 or (isinstance(total, int) and page * PAGE_SIZE * 2 < total)
    return SearchPage(items=items[: PAGE_SIZE * 2], page=page, has_more=has_more)


async def _janny_download(card_id: str) -> tuple[bytes, str]:
    try:
        return await _janny_download_card(card_id)
    except SourceError as error:
        hit = _janny_hits.get(card_id)
        card = janny_card(hit) if hit else None
        if card is None:
            raise
        # Blocked or failing: build the card from what the search returned.
        log.info('JannyAI download failed (%s); using the search result for %s', error, card_id)
        return await _card_file(card, _janny_summary(hit).image_url, ('image.jannyai.com',))


async def _janny_download_card(card_id: str) -> tuple[bytes, str]:
    session = await get_session()
    try:
        async with session.post(
            'https://api.jannyai.com/api/v1/download',
            json={'characterId': card_id},
            headers={**HEADERS, 'Content-Type': 'application/json'},
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            detail = (await _read_capped(response, 64 * 1024)).decode('utf-8', 'replace')
            if response.status >= 400:
                if 'cloudflare' in detail.lower() or 'just a moment' in detail.lower():
                    raise SourceError(
                        'JannyAI’s bot protection blocked this server. It usually lets home connections '
                        'through but blocks cloud hosts; you can also download the card on jannyai.com '
                        'and use Import Character Cards.'
                    )
                raise SourceError(f'JannyAI answered {response.status}.')
    except aiohttp.ClientError as error:
        raise SourceError(f'Could not reach JannyAI: {error}') from error
    except TimeoutError as error:
        raise SourceError('JannyAI did not answer in time.') from error
    try:
        url = json.loads(detail).get('downloadUrl') or ''
    except (json.JSONDecodeError, AttributeError) as error:
        raise SourceError('JannyAI returned something unexpected.') from error
    if not url:
        raise SourceError('JannyAI has no card for that character.')
    # The card file is on JannyAI's own storage; any https host it names is accepted for this one file.
    host = (urlparse(url).hostname or '').lower()
    if urlparse(url).scheme != 'https' or not host:
        raise SourceError('JannyAI returned an unusable download link.')
    data, _ = await _get(url, (host,), CHARACTER_CARD_MAX_BYTES)
    return data, 'card.png' if data.startswith(PNG_SIGNATURE) else 'card.json'


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
        Source('pygmalion', 'Pygmalion', 'https://pygmalion.chat', PYG_ID, _pyg_search, _pyg_download, False),
        Source(
            'backyard',
            'Backyard AI',
            'https://backyard.ai/hub',
            BACKYARD_ID,
            _backyard_search,
            _backyard_download,
            False,
        ),
        Source('wyvern', 'Wyvern', 'https://app.wyvern.chat', WYVERN_ID, _wyvern_search, _wyvern_download, False),
        Source(
            'janny',
            'JannyAI',
            'https://jannyai.com',
            JANNY_ID,
            _janny_search,
            _janny_download,
            True,
        ),
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
    if host == 'pygmalion.chat' and len(parts) >= 2 and parts[0] == 'character':
        return ('pygmalion', parts[1].lower()) if PYG_ID.match(parts[1].lower()) else None
    if host == 'backyard.ai' and len(parts) >= 3 and parts[:2] == ['hub', 'character']:
        return ('backyard', parts[2]) if BACKYARD_ID.match(parts[2]) else None
    if host == 'app.wyvern.chat' and 'characters' in parts[:-1] and WYVERN_ID.match(parts[-1]):
        # /characters/{id}, or a creator's /profiles/{name}/characters/{id}
        return ('wyvern', parts[-1])
    if host == 'jannyai.com' and len(parts) >= 2 and parts[0] == 'characters':
        card_id = parts[1].split('_', 1)[0].lower()
        return ('janny', card_id) if JANNY_ID.match(card_id) else None
    return None
