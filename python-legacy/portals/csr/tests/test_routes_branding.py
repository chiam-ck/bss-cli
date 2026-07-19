"""v1.8 cockpit Branding screen — /settings/branding.

Doctrine pins:

* One POST → one bss_cockpit.config writer; the tomlkit round-trip
  preserves the operator's comments in other sections.
* Non-destructive config writes — NO two-step confirm (same class as
  the /settings POSTs).
* Upload: magic bytes decide (content-type header is ignored), the cap
  is enforced by bytes read, SVG is never accepted.
* The preview endpoint writes nothing.
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

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'

_BASE_TOML = """\
[llm]
# hand-tuned — must survive branding saves
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
    for var in ("BSS_BRAND_NAME", "BSS_BRAND_THEME", "BSS_BRAND_MARK"):
        monkeypatch.delenv(var, raising=False)
    reset_cache()
    bss_branding.reset_cache()
    yield tmp_path
    reset_cache()
    bss_branding.reset_cache()


@pytest.fixture
def client(branding_root, monkeypatch):
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


def _settings_text(root: Path) -> str:
    return (root / "settings.toml").read_text(encoding="utf-8")


def test_get_renders_pickers_and_preview(client) -> None:
    r = client.get("/settings/branding")
    assert r.status_code == 200
    body = r.text
    for theme_id in ("phosphor", "amber-crt", "ice", "magenta", "paper", "solarized-dark"):
        assert f'value="{theme_id}"' in body
    for mark in ("$", "●", "▲", "✦", "►"):
        assert f'value="{mark}"' in body
    assert 'id="branding-preview"' in body
    assert 'enctype="multipart/form-data"' in body


def test_save_persists_and_preserves_comments(client, branding_root: Path) -> None:
    r = client.post(
        "/settings/branding",
        data={
            "brand_name": "Kopi Mobile",
            "theme": "ice",
            "mark_choice": "custom",
            "mark_custom": "☕",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=branding_saved" in r.headers["location"]
    text = _settings_text(branding_root)
    assert "# hand-tuned — must survive branding saves" in text
    assert 'brand_name = "Kopi Mobile"' in text
    view = bss_branding.current(root=branding_root)
    assert view.theme.id == "ice"
    assert view.mark == "☕"


def test_save_rejects_unknown_theme_with_400(client, branding_root: Path) -> None:
    before = _settings_text(branding_root)
    r = client.post(
        "/settings/branding",
        data={"brand_name": "x", "theme": "hotdog-stand", "mark_choice": "$"},
    )
    assert r.status_code == 400
    assert "hotdog-stand" in r.text  # error echoed
    assert _settings_text(branding_root) == before


def test_save_rejects_bad_custom_mark_with_400(client, branding_root: Path) -> None:
    r = client.post(
        "/settings/branding",
        data={
            "brand_name": "x",
            "theme": "phosphor",
            "mark_choice": "custom",
            "mark_custom": "<b>!",
        },
    )
    assert r.status_code == 400


def test_logo_upload_lifecycle(client, branding_root: Path) -> None:
    r = client.post(
        "/settings/branding/logo",
        files={"logo": ("anything.png", PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (branding_root / "branding" / "logo.png").exists()
    assert 'logo_image = "logo.png"' in _settings_text(branding_root)

    r = client.post("/settings/branding/logo/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (branding_root / "branding" / "logo.png").exists()
    assert 'logo_image = ""' in _settings_text(branding_root)


def test_logo_upload_magic_bytes_beat_content_type(client, branding_root: Path) -> None:
    # SVG smuggled under an image/png content-type: the bytes decide.
    r = client.post(
        "/settings/branding/logo",
        files={"logo": ("logo.png", SVG_BYTES, "image/png")},
    )
    assert r.status_code == 400
    assert not (branding_root / "branding").exists()


def test_logo_upload_rejects_oversize_by_bytes_read(client, branding_root: Path) -> None:
    oversize = PNG_BYTES + b"\x00" * bss_branding.MAX_LOGO_BYTES
    r = client.post(
        "/settings/branding/logo",
        files={"logo": ("logo.png", oversize, "image/png")},
    )
    assert r.status_code == 400
    assert not (branding_root / "branding").exists()


def test_preview_writes_nothing(client, branding_root: Path) -> None:
    before = _settings_text(branding_root)
    r = client.get(
        "/settings/branding/preview",
        params={"theme": "magenta", "brand_name": "PreviewCo", "mark_choice": "▲"},
    )
    assert r.status_code == 200
    assert "PreviewCo" in r.text
    assert "--accent:#e85ad1" in r.text  # magenta palette, scoped inline
    assert _settings_text(branding_root) == before
    assert bss_branding.current(root=branding_root).brand_name == "bss-cli"


def test_preview_degrades_on_unknown_theme(client) -> None:
    r = client.get("/settings/branding/preview", params={"theme": "nope"})
    assert r.status_code == 200
    assert "--accent:#74d535" in r.text  # falls back to phosphor
