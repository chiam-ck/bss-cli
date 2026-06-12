"""Hot-reload + autobootstrap tests for bss_cockpit.config (v0.13 PR3).

The loader is filesystem-backed; tests use ``tmp_path`` as the
``.bss-cli/`` root and seed the two files plus their templates.
"""

from __future__ import annotations

import os
import time

import pytest
from bss_cockpit.config import (
    CockpitSettings,
    current,
    reset_cache,
    write_operator_md,
    write_settings_toml,
)

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

_GOOD_OPERATOR_MD = "# Operator\n\nI am Ck.\n"


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def root(tmp_path):
    """A fake .bss-cli/ root with the two files + their templates."""
    (tmp_path / "OPERATOR.md").write_text(_GOOD_OPERATOR_MD)
    (tmp_path / "settings.toml").write_text(_GOOD_TOML)
    (tmp_path / "OPERATOR.md.template").write_text("# Default operator\n")
    (tmp_path / "settings.toml.template").write_text(_GOOD_TOML)
    return tmp_path


# ── Happy path ────────────────────────────────────────────────────────


def test_current_returns_validated_view(root) -> None:
    cfg = current(root=root)
    assert cfg.operator_md == _GOOD_OPERATOR_MD
    assert isinstance(cfg.settings, CockpitSettings)
    assert cfg.settings.llm.model == "google/gemma-4-26b-a4b-it"
    assert cfg.settings.cockpit.allow_destructive_default is False
    assert cfg.settings.ports.csr_portal == 9002


def test_current_caches_until_mtime_advances(root) -> None:
    a = current(root=root)
    b = current(root=root)
    assert a is b  # same dataclass instance returned from cache

    # Bump mtime + content
    time.sleep(0.01)
    (root / "OPERATOR.md").write_text("# changed\n")
    os.utime(
        root / "OPERATOR.md",
        (time.time() + 1, time.time() + 1),
    )

    c = current(root=root)
    assert c is not a
    assert c.operator_md == "# changed\n"


# ── Autobootstrap ─────────────────────────────────────────────────────


def test_autobootstrap_copies_templates_when_missing(tmp_path) -> None:
    (tmp_path / "OPERATOR.md.template").write_text("# Default\n")
    (tmp_path / "settings.toml.template").write_text(_GOOD_TOML)

    cfg = current(root=tmp_path)
    assert (tmp_path / "OPERATOR.md").read_text() == "# Default\n"
    assert (tmp_path / "settings.toml").exists()
    assert cfg.settings.llm.model == "google/gemma-4-26b-a4b-it"


def test_autobootstrap_does_not_overwrite_existing(root) -> None:
    """If OPERATOR.md already exists, the template is ignored."""
    (root / "OPERATOR.md").write_text("# Custom\n")
    cfg = current(root=root)
    assert cfg.operator_md == "# Custom\n"


# ── Recovery from invalid input ───────────────────────────────────────


def test_invalid_toml_after_good_load_keeps_serving_last_good(root) -> None:
    good = current(root=root)
    assert good.settings.llm.model == "google/gemma-4-26b-a4b-it"

    # Corrupt the file
    (root / "settings.toml").write_text("not = valid = toml")
    os.utime(
        root / "settings.toml",
        (time.time() + 1, time.time() + 1),
    )

    served = current(root=root)
    # Same dataclass instance — last good is what we get
    assert served is good


def test_invalid_toml_on_first_load_raises(tmp_path) -> None:
    (tmp_path / "OPERATOR.md").write_text("# x\n")
    (tmp_path / "settings.toml").write_text("not = valid = toml")
    with pytest.raises(Exception):  # tomllib.TOMLDecodeError
        current(root=tmp_path)


def test_pydantic_validation_failure_keeps_last_good(root) -> None:
    good = current(root=root)

    # temperature is float-typed; a string trips Pydantic validation.
    (root / "settings.toml").write_text(
        _GOOD_TOML.replace("temperature = 0.2", 'temperature = "hot"')
    )
    os.utime(
        root / "settings.toml",
        (time.time() + 1, time.time() + 1),
    )

    served = current(root=root)
    assert served is good  # same instance — last good is preserved


# ── Write helpers ─────────────────────────────────────────────────────


def test_write_operator_md_persists_and_invalidates_cache(root) -> None:
    a = current(root=root)
    write_operator_md("# Updated\n", root=root)
    b = current(root=root)
    assert b is not a
    assert b.operator_md == "# Updated\n"


def test_write_operator_md_rejects_empty(root) -> None:
    with pytest.raises(ValueError):
        write_operator_md("", root=root)


def test_write_settings_toml_validates_first(root) -> None:
    with pytest.raises(Exception):
        write_settings_toml("not = valid = toml", root=root)
    # File on disk was not touched
    assert "ck" in (root / "settings.toml").read_text()


def test_write_settings_toml_persists_and_returns_validated(root) -> None:
    new = _GOOD_TOML.replace("temperature = 0.2", "temperature = 0.7")
    validated = write_settings_toml(new, root=root)
    assert validated.llm.temperature == 0.7
    cfg = current(root=root)
    assert cfg.settings.llm.temperature == 0.7
