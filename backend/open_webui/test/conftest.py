import importlib.util
import os
import struct
import tempfile
import zlib

import pytest

# open_webui.env refuses to load without a secret key and writes under DATA_DIR on import.
os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key-not-for-production-use-0123456789')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='open-webui-test-'))
# open_webui.config empties STATIC_DIR on import; keep it away from the tracked static assets.
os.environ.setdefault('STATIC_DIR', tempfile.mkdtemp(prefix='open-webui-test-static-'))
# With only backend/requirements-slim.txt installed, run as the slim image does so config and
# storage do not import the full install's packages (chromadb, cloud storage SDKs). boto3 is
# imported by storage either way, so it is not a sign of the full install.
if importlib.util.find_spec('chromadb') is None:
    os.environ.setdefault('USE_SLIM_DOCKER', 'true')


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + chunk_type + data + struct.pack('>I', zlib.crc32(chunk_type + data))


def make_png(width: int = 1, height: int = 1, text_chunks: dict[str, str] | None = None) -> bytes:
    """Build a valid RGB PNG, optionally with tEXt chunks before IEND."""
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    pixels = b''.join(b'\x00' + b'\xff\x00\x00' * width for _ in range(height))
    chunks = [_png_chunk(b'IHDR', header), _png_chunk(b'IDAT', zlib.compress(pixels))]
    for keyword, text in (text_chunks or {}).items():
        chunks.append(_png_chunk(b'tEXt', keyword.encode('latin-1') + b'\x00' + text.encode('latin-1')))
    chunks.append(_png_chunk(b'IEND', b''))
    return b'\x89PNG\r\n\x1a\n' + b''.join(chunks)


@pytest.fixture
def png_factory():
    return make_png
