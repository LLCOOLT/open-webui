"""Character card parsing, normalization and PNG embedding.

Reads Character Card V1, V2 (``chara_card_v2``), V3 (``chara_card_v3``), Text Generation and
Character.AI style JSON, PNG cards with ``chara`` and ``ccv3`` text chunks, and CHARX archives.
Writes V3 JSON, a V2 projection of it, and PNG cards carrying both.

All card content is untrusted: nothing here evaluates card text or fetches remote URLs. HTTP(S)
asset URIs are recorded but never resolved.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any

from open_webui.env import CHARACTER_CARD_MAX_BYTES
from open_webui.utils.roleplay.lorebook import normalize_lorebook, strip_decorators
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
ZIP_SIGNATURE = b'PK\x03\x04'
PNG_ASSET_KEYWORD_PREFIX = 'chara-ext-asset_'
CARD_TEXT_KEYWORDS = ('ccv3', 'chara')

RAW_MAX_BYTES = 1024 * 1024

V2_EXPORT_WARNING = (
    'This character card is Character Card V3, but it is loaded as a Character Card V2. '
    'Please use a Character Card V3 compatible application to use this character card properly.'
)

TEXT_FIELDS = (
    'name',
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'creator_notes',
    'system_prompt',
    'post_history_instructions',
    'creator',
    'character_version',
)
LIST_FIELDS = ('alternate_greetings', 'tags', 'group_only_greetings', 'source')
V2_DATA_FIELDS = (
    'name',
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'creator_notes',
    'system_prompt',
    'post_history_instructions',
    'alternate_greetings',
    'character_book',
    'tags',
    'creator',
    'character_version',
    'extensions',
)

# Field paths for formats that predate the V2 spec. The first non-empty path wins.
TEXTGEN_ALIASES = {
    'name': ('char_name',),
    'description': ('char_persona',),
    'scenario': ('world_scenario',),
    'first_mes': ('char_greeting',),
    'mes_example': ('example_dialogue',),
}
CAI_ALIASES = {
    'name': ('name',),
    'description': ('description',),
    'creator_notes': ('title',),
    'first_mes': ('greeting',),
    'mes_example': ('definition',),
}

DEFAULT_ASSETS = [{'type': 'icon', 'uri': 'ccdefault:', 'name': 'main', 'ext': 'png'}]


class CardParseError(ValueError):
    pass


class CardTooLargeError(CardParseError):
    pass


class CharacterData(BaseModel):
    """A character card normalized to the V3 field set."""

    spec_version: str = '3.0'
    name: str
    nickname: str | None = None
    description: str = ''
    personality: str = ''
    scenario: str = ''
    first_mes: str = ''
    alternate_greetings: list[str] = Field(default_factory=list)
    group_only_greetings: list[str] = Field(default_factory=list)
    mes_example: str = ''
    system_prompt: str = ''
    post_history_instructions: str = ''
    creator: str = ''
    creator_notes: str = ''
    creator_notes_multilingual: dict[str, str] = Field(default_factory=dict)
    character_version: str = ''
    tags: list[str] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)
    creation_date: int | None = None
    modification_date: int | None = None
    character_book: dict | None = None
    assets: list[dict] = Field(default_factory=list)
    extensions: dict = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)
    raw_truncated: bool = False


@dataclass
class ParsedCard:
    character: CharacterData
    source_format: str  # 'json' | 'png' | 'charx'
    avatar: bytes | None = None
    avatar_content_type: str | None = None
    background: bytes | None = None
    background_content_type: str | None = None
    # (name, image, content type) for each emotion asset, as named in the card.
    expressions: list[tuple[str, bytes, str]] = field(default_factory=list)


@dataclass
class _AssetSources:
    default_image: bytes | None = None
    png_assets: dict[str, bytes] = field(default_factory=dict)
    archive: zipfile.ZipFile | None = None


####################
# Field coercion
####################


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _lookup(source: dict, path: str) -> Any:
    value: Any = source
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


####################
# Normalization
####################


def detect_card_format(card: dict) -> str:
    """Return one of ``v3``, ``v2``, ``v1``, ``textgen`` or ``cai``."""
    spec = card.get('spec')
    if spec == 'chara_card_v3':
        return 'v3'
    if spec == 'chara_card_v2' or (isinstance(card.get('data'), dict) and 'name' in card['data']):
        return 'v2'
    if 'char_name' in card:
        return 'textgen'
    if isinstance(card.get('character'), dict) or isinstance(_lookup(card, 'info.character'), dict):
        return 'cai'
    if 'name' in card:
        return 'v1'
    raise CardParseError('Unrecognized character card format.')


def _fields_from_spec_data(data: dict) -> dict:
    fields: dict[str, Any] = {name: _text(data.get(name)) for name in TEXT_FIELDS}
    fields.update({name: _text_list(data.get(name)) for name in LIST_FIELDS})
    nickname = data.get('nickname')
    multilingual = data.get('creator_notes_multilingual')
    assets = data.get('assets')
    fields.update(
        nickname=nickname if isinstance(nickname, str) and nickname else None,
        creator_notes_multilingual={key: value for key, value in (multilingual or {}).items() if isinstance(value, str)}
        if isinstance(multilingual, dict)
        else {},
        creation_date=_timestamp(data.get('creation_date')),
        modification_date=_timestamp(data.get('modification_date')),
        character_book=normalize_lorebook(data.get('character_book')),
        assets=[asset for asset in assets if isinstance(asset, dict)] if isinstance(assets, list) else [],
        extensions=data.get('extensions') if isinstance(data.get('extensions'), dict) else {},
    )
    return fields


def _fields_from_aliases(source: dict, aliases: dict[str, tuple[str, ...]]) -> dict:
    fields: dict[str, str] = {}
    for name, paths in aliases.items():
        for path in paths:
            value = _lookup(source, path)
            if isinstance(value, str) and value.strip():
                fields[name] = value.strip()
                break
    return fields


def _cap_raw(card: dict) -> tuple[dict, bool]:
    size = len(json.dumps(card, ensure_ascii=False).encode('utf-8'))
    if size > RAW_MAX_BYTES:
        log.info('Character card JSON is %d bytes; not keeping the raw copy', size)
        return {}, True
    return card, False


def normalize_card(card: Any) -> CharacterData:
    """Normalize any supported card JSON object to ``CharacterData``. Unknown fields stay in ``raw``."""
    if not isinstance(card, dict):
        raise CardParseError('Character card must be a JSON object.')

    card_format = detect_card_format(card)
    if card_format in ('v3', 'v2'):
        fields = _fields_from_spec_data(card['data'] if isinstance(card.get('data'), dict) else {})
        spec_version = str(card.get('spec_version') or '3.0') if card_format == 'v3' else '2.0'
    elif card_format == 'v1':
        fields = _fields_from_spec_data(card)
        spec_version = '1.0'
    elif card_format == 'textgen':
        fields = _fields_from_aliases(card, TEXTGEN_ALIASES)
        spec_version = 'textgen'
    else:
        character = card.get('character') if isinstance(card.get('character'), dict) else card['info']['character']
        fields = _fields_from_aliases(character, CAI_ALIASES)
        spec_version = 'cai'

    if not fields.get('name', '').strip():
        raise CardParseError('Character card has no name.')

    raw, raw_truncated = _cap_raw(card)
    return CharacterData(spec_version=spec_version, raw=raw, raw_truncated=raw_truncated, **fields)


####################
# PNG chunks
####################


def read_png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Return ``(type, data)`` for every chunk up to and including IEND, verifying CRCs."""
    if not data.startswith(PNG_SIGNATURE):
        raise CardParseError('Invalid PNG file.')

    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        (length,) = struct.unpack('>I', data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 8 + length
        if end + 4 > len(data):
            raise CardParseError('Truncated PNG chunk.')
        chunk_data = data[offset + 8 : end]
        (crc,) = struct.unpack('>I', data[end : end + 4])
        # RisuAI writes IEND with a zero CRC; IEND carries no data, so that is harmless.
        if zlib.crc32(chunk_type + chunk_data) != crc and not (chunk_type == b'IEND' and not chunk_data):
            raise CardParseError(f'Invalid CRC for PNG chunk {chunk_type!r}.')
        chunks.append((chunk_type, chunk_data))
        offset = end + 4
        if chunk_type == b'IEND':
            return chunks

    raise CardParseError('PNG file has no IEND chunk.')


def _encode_png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + chunk_data)
    return struct.pack('>I', len(chunk_data)) + chunk_type + chunk_data + struct.pack('>I', crc)


def _text_chunk_keyword(chunk_data: bytes) -> str:
    return chunk_data.split(b'\x00', 1)[0].decode('latin-1')


def _read_text_chunks(chunks: list[tuple[bytes, bytes]]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for chunk_type, chunk_data in chunks:
        if chunk_type == b'tEXt' and b'\x00' in chunk_data:
            keyword, text = chunk_data.split(b'\x00', 1)
            texts.setdefault(keyword.decode('latin-1'), text.decode('latin-1'))
    return texts


def _is_card_keyword(keyword: str) -> bool:
    return keyword in CARD_TEXT_KEYWORDS or keyword.startswith(PNG_ASSET_KEYWORD_PREFIX)


def write_png_text_chunks(png: bytes, texts: dict[str, str]) -> bytes:
    """Return ``png`` with existing card chunks removed and ``texts`` added as tEXt chunks before IEND."""
    out = [PNG_SIGNATURE]
    for chunk_type, chunk_data in read_png_chunks(png):
        if chunk_type == b'tEXt' and _is_card_keyword(_text_chunk_keyword(chunk_data)):
            continue
        if chunk_type == b'IEND':
            for keyword, text in texts.items():
                out.append(_encode_png_chunk(b'tEXt', keyword.encode('latin-1') + b'\x00' + text.encode('latin-1')))
        out.append(_encode_png_chunk(chunk_type, chunk_data))
    return b''.join(out)


def strip_card_chunks(png: bytes) -> bytes:
    """Return ``png`` without any embedded card or asset chunks."""
    return write_png_text_chunks(png, {})


def _decode_card_text(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith('{'):
        return json.loads(stripped)
    try:
        return json.loads(base64.b64decode(stripped).decode('utf-8-sig'))
    except (binascii.Error, UnicodeDecodeError) as error:
        raise CardParseError('PNG card text is not valid base64 JSON.') from error


####################
# Assets
####################


def sniff_image_type(data: bytes | None) -> str | None:
    if not data:
        return None
    if data.startswith(PNG_SIGNATURE):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return None


def _read_archive_entry(archive: zipfile.ZipFile, path: str) -> bytes | None:
    try:
        return archive.read(path.lstrip('/'))
    except KeyError:
        return None


def _decode_data_uri(uri: str) -> bytes | None:
    header, _, payload = uri.partition(',')
    if ';base64' not in header:
        return None
    try:
        return base64.b64decode(payload)
    except binascii.Error:
        return None


def _resolve_asset(uri: Any, sources: _AssetSources) -> bytes | None:
    if not isinstance(uri, str):
        return None
    if uri.startswith('ccdefault:'):
        return sources.default_image
    if uri.startswith('__asset:'):
        return sources.png_assets.get(uri[len('__asset:') :])
    for prefix in ('embeded://', 'embedded://'):
        if uri.startswith(prefix) and sources.archive is not None:
            return _read_archive_entry(sources.archive, uri[len(prefix) :])
    if uri.startswith('data:'):
        return _decode_data_uri(uri)
    # Remote and unknown URIs are recorded in the card but never fetched.
    return None


def _pick_asset(assets: list[dict], asset_type: str, preferred_name: str | None) -> dict | None:
    candidates = [asset for asset in assets if asset.get('type') == asset_type]
    if preferred_name is not None:
        for asset in candidates:
            if asset.get('name') == preferred_name:
                return asset
    return candidates[0] if candidates else None


def _attach_assets(parsed: ParsedCard, sources: _AssetSources) -> ParsedCard:
    assets = parsed.character.assets or DEFAULT_ASSETS
    icon = _pick_asset(assets, 'icon', 'main')
    background = _pick_asset(assets, 'background', None)

    if icon is not None:
        avatar = _resolve_asset(icon.get('uri'), sources)
        if content_type := sniff_image_type(avatar):
            parsed.avatar, parsed.avatar_content_type = avatar, content_type
    if background is not None:
        image = _resolve_asset(background.get('uri'), sources)
        if content_type := sniff_image_type(image):
            parsed.background, parsed.background_content_type = image, content_type
    seen: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('type') != 'emotion':
            continue
        name = asset.get('name')
        if not isinstance(name, str) or not name.strip() or name in seen:
            continue
        image = _resolve_asset(asset.get('uri'), sources)
        if content_type := sniff_image_type(image):
            seen.add(name)
            parsed.expressions.append((name, image, content_type))
    return parsed


####################
# Container formats
####################


def _parse_json(data: bytes) -> ParsedCard:
    try:
        card = json.loads(data.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CardParseError('Character card is not valid JSON.') from error
    return ParsedCard(character=normalize_card(card), source_format='json')


def _png_assets(texts: dict[str, str]) -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    for keyword, text in texts.items():
        if not keyword.startswith(PNG_ASSET_KEYWORD_PREFIX):
            continue
        path = keyword[len(PNG_ASSET_KEYWORD_PREFIX) :].removeprefix(':')
        try:
            assets[path] = base64.b64decode(text)
        except binascii.Error:
            log.debug('Skipping undecodable PNG card asset %s', path)
    return assets


def _parse_png(data: bytes) -> ParsedCard:
    texts = _read_text_chunks(read_png_chunks(data))
    card_text = next((texts[keyword] for keyword in CARD_TEXT_KEYWORDS if keyword in texts), None)
    if card_text is None:
        raise CardParseError('PNG file does not contain a character card.')
    try:
        card = _decode_card_text(card_text)
    except json.JSONDecodeError as error:
        raise CardParseError('PNG card text is not valid JSON.') from error

    parsed = ParsedCard(character=normalize_card(card), source_format='png')
    sources = _AssetSources(default_image=strip_card_chunks(data), png_assets=_png_assets(texts))
    return _attach_assets(parsed, sources)


def _check_archive(archive: zipfile.ZipFile, max_bytes: int) -> None:
    infos = archive.infolist()
    if any(info.flag_bits & 0x1 for info in infos):
        raise CardParseError('Encrypted CHARX archives are not supported.')
    if sum(info.file_size for info in infos) > max_bytes:
        raise CardTooLargeError('CHARX archive is too large.')


def _parse_charx(data: bytes, max_bytes: int) -> ParsedCard:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise CardParseError('Invalid CHARX archive.') from error

    with archive:
        _check_archive(archive, max_bytes)
        card_bytes = _read_archive_entry(archive, 'card.json')
        if card_bytes is None:
            raise CardParseError('CHARX archive has no card.json.')
        parsed = _parse_json(card_bytes)
        parsed.source_format = 'charx'
        return _attach_assets(parsed, _AssetSources(archive=archive))


def parse_card_bytes(data: bytes, filename: str = '', max_bytes: int | None = None) -> ParsedCard:
    """Parse a character card file, dispatching on its content rather than its name."""
    max_bytes = CHARACTER_CARD_MAX_BYTES if max_bytes is None else max_bytes
    if len(data) > max_bytes:
        label = f' "{filename}"' if filename else ''
        raise CardTooLargeError(f'Character card{label} is too large.')
    if data.startswith(PNG_SIGNATURE):
        return _parse_png(data)
    if data.startswith(ZIP_SIGNATURE) or _is_embedded_zip(data):
        return _parse_charx(data, max_bytes)
    return _parse_json(data)


def _is_embedded_zip(data: bytes) -> bool:
    """RisuRealm serves some CHARX files as a JPEG with the archive appended, so the file still
    previews as an image; ``zipfile`` reads those from the end."""
    if data.lstrip()[:1] in (b'{', b'['):
        return False
    return zipfile.is_zipfile(io.BytesIO(data))


####################
# Export
####################


def character_to_v3(character: CharacterData, assets: list[dict] | None = None) -> dict:
    """Build a V3 card. Passthrough fields from the original card are kept, edited fields win.

    ``assets`` replaces the card's asset list; by default the card points at its own PNG image.
    """
    raw_data = character.raw.get('data') if character.raw.get('spec') in ('chara_card_v2', 'chara_card_v3') else None
    data = dict(raw_data) if isinstance(raw_data, dict) else {}
    data.update(character.model_dump(include=set(TEXT_FIELDS) | set(LIST_FIELDS)))
    data.update(
        creator_notes_multilingual=character.creator_notes_multilingual,
        extensions=character.extensions,
        assets=DEFAULT_ASSETS if assets is None else assets,
    )
    optional = {
        'nickname': character.nickname,
        'creation_date': character.creation_date,
        'modification_date': character.modification_date,
        'character_book': character.character_book,
    }
    for key, value in optional.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return {'spec': 'chara_card_v3', 'spec_version': '3.0', 'data': data}


def _v2_book(book: dict) -> dict:
    entries = []
    for entry in book.get('entries', []):
        v2_entry = {key: value for key, value in entry.items() if key != 'use_regex'}
        v2_entry['content'] = strip_decorators(entry.get('content', ''))
        entries.append(v2_entry)
    return {**book, 'entries': entries}


def v3_to_v2(card: dict) -> dict:
    """Project a V3 card onto the V2 spec for applications that only read the ``chara`` chunk."""
    data = card.get('data', {})
    v2_data = {key: data[key] for key in V2_DATA_FIELDS if key in data}
    v2_data.setdefault('extensions', {})
    if isinstance(v2_data.get('character_book'), dict):
        v2_data['character_book'] = _v2_book(v2_data['character_book'])
    notes = v2_data.get('creator_notes', '')
    v2_data['creator_notes'] = f'{notes}\n\n{V2_EXPORT_WARNING}' if notes else V2_EXPORT_WARNING
    return {'spec': 'chara_card_v2', 'spec_version': '2.0', 'data': v2_data}


def _b64_json(value: dict) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode('utf-8')).decode('ascii')


def write_card_png(png: bytes, card: dict) -> bytes:
    """Embed a V3 card (``ccv3``) and its V2 projection (``chara``) in ``png``."""
    return write_png_text_chunks(png, {'chara': _b64_json(v3_to_v2(card)), 'ccv3': _b64_json(card)})
