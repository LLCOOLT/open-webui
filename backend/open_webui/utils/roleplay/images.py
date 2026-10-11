"""Image helpers for character import and export."""

from __future__ import annotations

import base64
import io

from open_webui.utils.roleplay.cards import PNG_SIGNATURE
from PIL import Image, ImageOps

THUMBNAIL_SIZE = 250
MAX_PIXELS = 25_000_000
PLACEHOLDER_SIZE = (400, 600)


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    if image.width * image.height > MAX_PIXELS:
        raise ValueError('Image is too large.')
    image.load()
    return image


def avatar_thumbnail_data_uri(data: bytes) -> str:
    """A 250x250 WebP data URI, cropped to fill like the model editor's avatar upload."""
    try:
        with _open(data) as image:
            thumbnail = ImageOps.fit(image.convert('RGBA'), (THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('Invalid avatar image.') from error
    buffer = io.BytesIO()
    thumbnail.save(buffer, format='WEBP', quality=80)
    return 'data:image/webp;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def ensure_png(data: bytes | None) -> bytes:
    """Return ``data`` as PNG bytes, converting other formats. Without an image, a plain placeholder."""
    if data and data.startswith(PNG_SIGNATURE):
        return data
    converted = Image.new('RGB', PLACEHOLDER_SIZE, (64, 64, 64))
    if data:
        try:
            with _open(data) as image:
                converted = image.convert('RGBA')
        except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
            pass
    buffer = io.BytesIO()
    converted.save(buffer, format='PNG')
    return buffer.getvalue()


EXPRESSION_MAX_SIZE = 1024


def expression_image(data: bytes) -> tuple[bytes, str]:
    """An expression image as stored: WebP fitted within 1024px, or the original when animated."""
    try:
        with _open(data) as image:
            if getattr(image, 'is_animated', False) and image.format in ('GIF', 'WEBP', 'PNG'):
                return data, f'image/{image.format.lower()}'
            fitted = image.convert('RGBA')
            fitted.thumbnail((EXPRESSION_MAX_SIZE, EXPRESSION_MAX_SIZE), Image.Resampling.LANCZOS)
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('Invalid expression image.') from error
    buffer = io.BytesIO()
    fitted.save(buffer, format='WEBP', quality=85)
    return buffer.getvalue(), 'image/webp'
