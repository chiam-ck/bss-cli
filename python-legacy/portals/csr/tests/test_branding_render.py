"""v1.8 branding on the cockpit chrome.

Same doctrine pins as the self-serve twin: brand name/mark/theme are
hot-config, the version is demoted to the new fixed-height footer
(literal "bss-cli vX" product attribution — never the operator brand),
and /branding/logo serves the uploaded image.

Uses the /settings page as the render target — it extends
cockpit_layout.html and needs no stubbed service clients.
"""

from __future__ import annotations

import os
from pathlib import Path

import bss_branding
import pytest
from bss_cockpit import config as cockpit_config
from bss_cockpit.config import reset_cache
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

_BASE_TOML = """\
[llm]
temperature = 0.2

[cockpit]
allow_destructive_default = false
"""


@pytest.fixture
def branding_root(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "OPERATOR.md").write_text("# Operator\n", encoding="utf-8")
    (tmp_path / "settings.toml").write_text(_BASE_TOML, encoding="utf-8")
    monkeypatch.setattr(cockpit_config, "_bss_cli_dir", lambda: tmp_path)
    monkeypatch.setenv("BSS_BRANDING_DIR", str(tmp_path))
    reset_cache()
    bss_branding.reset_cache()
    yield tmp_path
    reset_cache()
    bss_branding.reset_cache()


@pytest.fixture
def branding_client(branding_root, monkeypatch):
    monkeypatch.setenv(
        "BSS_DB_URL",
        os.environ.get(
            "BSS_DB_URL",
            "postgresql+asyncpg://bss:bss_password@localhost:5432/bss",
        ),
    )
    app = create_app(Settings())
    with TestClient(app) as c:
        yield c


def test_default_brand_and_demoted_version(branding_client) -> None:
    r = branding_client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert '<span class="cockpit-brand-name">bss-cli</span>' in body
    assert '<span class="cockpit-brand-mark">$</span>' in body
    # Version demoted: tag is bare, footer carries product attribution.
    assert '<span class="cockpit-brand-tag">operator cockpit</span>' in body
    assert 'class="cockpit-footer">bss-cli v' in body
    assert "<style>:root{" in body
    assert "--accent:#74d535" in body


def test_custom_branding_hot_reloads(branding_client, branding_root: Path) -> None:
    (branding_root / "settings.toml").write_text(
        _BASE_TOML + '\n[branding]\nbrand_name = "Kopi Mobile"\ntheme = "amber-crt"\nmark = "✦"\n',
        encoding="utf-8",
    )
    bss_branding.reset_cache()
    body = branding_client.get("/settings").text
    assert '<span class="cockpit-brand-name">Kopi Mobile</span>' in body
    assert '<span class="cockpit-brand-mark">✦</span>' in body
    assert "<title>Kopi Mobile Cockpit" in body
    assert "--accent:#ffb000" in body  # amber-crt
    # Footer attribution never rebrands.
    assert 'class="cockpit-footer">bss-cli v' in body
    assert "Kopi Mobile Cockpit · v" not in body


def test_logo_route_and_header_image(branding_client, branding_root: Path) -> None:
    assert branding_client.get("/branding/logo").status_code == 404

    (branding_root / "settings.toml").write_text(
        _BASE_TOML + '\n[branding]\nlogo_image = "logo.png"\n', encoding="utf-8"
    )
    (branding_root / "branding").mkdir()
    (branding_root / "branding" / "logo.png").write_bytes(PNG_BYTES)
    bss_branding.reset_cache()

    r = branding_client.get("/branding/logo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")

    page = branding_client.get("/settings").text
    assert '<img class="cockpit-brand-logo" src="/branding/logo?v=' in page
    assert 'class="cockpit-brand-mark"' not in page
