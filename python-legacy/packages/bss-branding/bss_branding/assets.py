"""Logo-image validation: magic-byte sniffing + size cap.

The upload route trusts nothing the browser says — not the filename,
not the Content-Type header, not Content-Length. The bytes decide.
PNG, JPEG and WebP only; **never SVG** (scriptable → XSS on both
portals). 256 KB cap keeps a header logo a header logo.
"""

from __future__ import annotations

from typing import Literal

MAX_LOGO_BYTES = 262_144  # 256 KB

ImageType = Literal["png", "jpeg", "webp"]

# Sniffed type → the fixed on-disk filename under .bss-cli/branding/.
# Fixed names are the anti-path-traversal story: no user-controlled
# path component ever reaches the filesystem.
LOGO_FILENAMES: dict[ImageType, str] = {
    "png": "logo.png",
    "jpeg": "logo.jpg",
    "webp": "logo.webp",
}

CONTENT_TYPES: dict[ImageType, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

# Reverse map for serving: fixed filename → content type.
CONTENT_TYPE_BY_FILENAME: dict[str, str] = {filename: CONTENT_TYPES[kind] for kind, filename in LOGO_FILENAMES.items()}


def sniff_image_type(data: bytes) -> ImageType | None:
    """Identify PNG/JPEG/WebP by magic bytes; ``None`` for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None
