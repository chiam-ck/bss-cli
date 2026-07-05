from __future__ import annotations

from bss_branding import MAX_LOGO_BYTES, sniff_image_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 16


def test_accepts_png_jpeg_webp() -> None:
    assert sniff_image_type(PNG) == "png"
    assert sniff_image_type(JPEG) == "jpeg"
    assert sniff_image_type(WEBP) == "webp"


def test_rejects_svg_regardless_of_claimed_type() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'
    assert sniff_image_type(svg) is None


def test_rejects_gif_and_garbage_and_truncated() -> None:
    assert sniff_image_type(b"GIF89a" + b"\x00" * 16) is None
    assert sniff_image_type(b"hello world") is None
    assert sniff_image_type(b"") is None
    assert sniff_image_type(b"\x89PN") is None  # truncated PNG magic
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WAVE") is None  # RIFF, not WEBP


def test_cap_is_256k() -> None:
    assert MAX_LOGO_BYTES == 262_144
