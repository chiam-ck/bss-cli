"""Tests for the v0.13 cockpit /settings page (PR8).

Each test uses a per-test ``.bss-cli`` directory (via tmp_path +
monkey-patching ``bss_cockpit.config._bss_cli_dir``) so the actual
repo file isn't mutated. Validation failures should keep the prior
good view in effect — the loader's "last good" contract is what
makes operator-typo recovery cheap.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bss_cockpit import config as cockpit_config
from bss_cockpit.config import reset_cache
from bss_csr.config import Settings
from bss_csr.main import create_app
from fastapi.testclient import TestClient

_GOOD_TOML = """\
[llm]
model = "google/gemma-4-26b-a4b-it"
temperature = 0.2

[cockpit]
allow_destructive_default = false

[ports]
csr_portal = 9002

[dev_service_urls]
"""


@pytest.fixture
def settings_root(tmp_path, monkeypatch) -> Path:
    """Replace .bss-cli/ with a tmp dir for the duration of one test."""
    (tmp_path / "OPERATOR.md").write_text("# Operator\n\nI am the test operator.\n")
    (tmp_path / "settings.toml").write_text(_GOOD_TOML)
    monkeypatch.setattr(cockpit_config, "_bss_cli_dir", lambda: tmp_path)
    reset_cache()
    yield tmp_path
    reset_cache()


@pytest.fixture
def settings_client(settings_root, monkeypatch):
    # The lifespan needs BSS_DB_URL but the settings route doesn't
    # touch the cockpit store — point at any non-empty URL so the
    # ConversationStore can be constructed.
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


def test_get_settings_renders_both_files(settings_client) -> None:
    r = settings_client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert "OPERATOR.md" in body
    assert "settings.toml" in body
    assert "I am the test operator." in body
    # The textarea content is HTML-escaped (Jinja's default escape filter).
    # Match the model line, which is a string we own in _GOOD_TOML.
    assert "google/gemma-4-26b-a4b-it" in body


def test_post_settings_operator_persists_and_reloads(
    settings_client, settings_root: Path
) -> None:
    new_md = "# Operator\n\nUpdated persona.\n"
    r = settings_client.post(
        "/settings/operator",
        data={"operator_md": new_md},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=operator_saved" in r.headers["location"]
    assert (settings_root / "OPERATOR.md").read_text() == new_md
    # Next GET should reflect the new content.
    r2 = settings_client.get("/settings?flash=operator_saved")
    assert "Updated persona." in r2.text
    assert "saved + reloaded" in r2.text


def test_post_settings_operator_rejects_empty(
    settings_client, settings_root: Path
) -> None:
    original = (settings_root / "OPERATOR.md").read_text()
    r = settings_client.post(
        "/settings/operator", data={"operator_md": "  "}, follow_redirects=False
    )
    assert r.status_code == 400
    assert "cannot be empty" in r.text
    # File on disk untouched.
    assert (settings_root / "OPERATOR.md").read_text() == original


def test_post_settings_config_persists_and_reloads(
    settings_client, settings_root: Path
) -> None:
    new_toml = _GOOD_TOML.replace("temperature = 0.2", "temperature = 0.7")
    r = settings_client.post(
        "/settings/config",
        data={"settings_toml": new_toml},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash=config_saved" in r.headers["location"]
    body = settings_client.get("/settings?flash=config_saved").text
    assert "temperature = 0.7" in body


def test_post_settings_config_rejects_invalid_toml(
    settings_client, settings_root: Path
) -> None:
    original = (settings_root / "settings.toml").read_text()
    r = settings_client.post(
        "/settings/config",
        data={"settings_toml": "not = valid = toml"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "TOMLDecodeError" in r.text
    # The unsaved input should round-trip back into the textarea.
    assert "not = valid = toml" in r.text
    # File on disk untouched.
    assert (settings_root / "settings.toml").read_text() == original


def test_post_settings_config_rejects_pydantic_failure(
    settings_client, settings_root: Path
) -> None:
    """temperature is a float — a string trips Pydantic v2 validation."""
    bad = _GOOD_TOML.replace("temperature = 0.2", 'temperature = "hot"')
    r = settings_client.post(
        "/settings/config",
        data={"settings_toml": bad},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "ValidationError" in r.text
