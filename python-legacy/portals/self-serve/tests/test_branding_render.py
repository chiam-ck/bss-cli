"""v1.8 branding on the self-serve chrome.

Pins the doctrine seams: operator brand (name/mark/theme) is
hot-config from ``[branding]``; the version number is DEMOTED to the
footer as literal product attribution ("bss-cli vX") and never appears
in the header brand-tag; the injected ``:root`` style block follows the
active theme; ``/branding/logo`` is public-by-allowlist and 404s when
no logo is uploaded.
"""

from __future__ import annotations

from pathlib import Path

import bss_branding
import pytest

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def branding_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BSS_BRANDING_DIR", str(tmp_path))
    bss_branding.reset_cache()
    yield tmp_path
    bss_branding.reset_cache()


def test_default_brand_header_and_demoted_version(client, branding_root) -> None:
    r = client.get("/welcome")
    assert r.status_code == 200
    body = r.text
    assert '<span class="brand-name">bss-cli</span>' in body
    assert '<span class="brand-mark">$</span>' in body
    # Version is demoted: never in the brand-tag, always in the footer.
    assert '<span class="brand-tag">/ self-serve</span>' in body
    assert 'class="portal-footer-version">bss-cli v' in body
    # Default theme block is injected (phosphor accent).
    assert "<style>:root{" in body
    assert "--accent:#74d535" in body


def test_custom_brand_name_theme_and_mark(client, branding_root: Path) -> None:
    (branding_root / "settings.toml").write_text(
        '[branding]\nbrand_name = "Kopi Mobile"\ntheme = "ice"\nmark = "▲"\n',
        encoding="utf-8",
    )
    bss_branding.reset_cache()
    r = client.get("/welcome")
    body = r.text
    assert '<span class="brand-name">Kopi Mobile</span>' in body
    assert '<span class="brand-mark">▲</span>' in body
    assert "<title>Welcome · Kopi Mobile self-serve</title>" in body
    assert "--accent:#4dc4ff" in body  # ice
    # Product attribution in the footer stays literal bss-cli.
    assert 'class="portal-footer-version">bss-cli v' in body


def test_logo_route_404_when_absent(client, branding_root) -> None:
    assert client.get("/branding/logo").status_code == 404


def test_logo_route_serves_uploaded_image(client, branding_root: Path) -> None:
    (branding_root / "settings.toml").write_text('[branding]\nlogo_image = "logo.png"\n', encoding="utf-8")
    (branding_root / "branding").mkdir()
    (branding_root / "branding" / "logo.png").write_bytes(PNG_BYTES)
    bss_branding.reset_cache()

    r = client.get("/branding/logo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert "immutable" in r.headers["cache-control"]
    assert r.content == PNG_BYTES

    # Header swaps the text mark for the image, cache-busted by mtime.
    page = client.get("/welcome").text
    assert '<img class="brand-logo" src="/branding/logo?v=' in page
    assert 'class="brand-mark"' not in page


def test_logo_route_is_public_allowlisted() -> None:
    from bss_self_serve.security import PUBLIC_EXACT_PATHS, is_public_path

    assert "/branding/logo" in PUBLIC_EXACT_PATHS
    assert is_public_path("/branding/logo")
