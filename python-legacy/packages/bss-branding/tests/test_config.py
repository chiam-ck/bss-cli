"""Read-path contract: defaults on absence, mtime hot-reload,
last-good on parse error, env overrides, logo resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bss_branding import (
    BrandingSettings,
    current,
    reset_cache,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _clean_cache_and_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("BSS_BRAND_NAME", "BSS_BRAND_THEME", "BSS_BRAND_MARK"):
        monkeypatch.delenv(var, raising=False)
    reset_cache()
    yield
    reset_cache()


def _write_settings(root: Path, body: str) -> Path:
    path = root / "settings.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))


def test_absent_dir_yields_defaults(tmp_path: Path) -> None:
    view = current(root=tmp_path / "nowhere")
    assert view.brand_name == "bss-cli"
    assert view.theme.id == "phosphor"
    assert view.mark == "$"
    assert view.logo_path is None
    assert view.logo_version == 0
    # And nothing was bootstrapped.
    assert not (tmp_path / "nowhere").exists()


def test_missing_branding_section_yields_defaults(tmp_path: Path) -> None:
    _write_settings(tmp_path, "[llm]\ntemperature = 0.2\n")
    view = current(root=tmp_path)
    assert view.brand_name == "bss-cli"
    assert view.theme.id == "phosphor"


def test_reads_branding_section(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        '[branding]\nbrand_name = "Kopi Mobile"\ntheme = "amber-crt"\nmark = "▲"\n',
    )
    view = current(root=tmp_path)
    assert view.brand_name == "Kopi Mobile"
    assert view.theme.id == "amber-crt"
    assert view.mark == "▲"


def test_hot_reload_on_mtime_change(tmp_path: Path) -> None:
    path = _write_settings(tmp_path, '[branding]\ntheme = "ice"\n')
    assert current(root=tmp_path).theme.id == "ice"
    path.write_text('[branding]\ntheme = "magenta"\n', encoding="utf-8")
    _bump_mtime(path)
    assert current(root=tmp_path).theme.id == "magenta"


def test_bad_toml_keeps_last_good(tmp_path: Path) -> None:
    path = _write_settings(tmp_path, '[branding]\ntheme = "ice"\n')
    assert current(root=tmp_path).theme.id == "ice"
    path.write_text("[branding\nnot toml", encoding="utf-8")
    _bump_mtime(path)
    assert current(root=tmp_path).theme.id == "ice"


def test_unknown_theme_keeps_last_good(tmp_path: Path) -> None:
    path = _write_settings(tmp_path, '[branding]\ntheme = "ice"\n')
    assert current(root=tmp_path).theme.id == "ice"
    path.write_text('[branding]\ntheme = "hotdog-stand"\n', encoding="utf-8")
    _bump_mtime(path)
    assert current(root=tmp_path).theme.id == "ice"


def test_bad_file_with_no_prior_good_yields_defaults(tmp_path: Path) -> None:
    _write_settings(tmp_path, "[branding\nnot toml")
    view = current(root=tmp_path)
    assert view.theme.id == "phosphor"


def test_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_settings(tmp_path, '[branding]\nbrand_name = "FileCo"\n')
    monkeypatch.setenv("BSS_BRAND_NAME", "EnvCo")
    monkeypatch.setenv("BSS_BRAND_THEME", "paper")
    view = current(root=tmp_path)
    assert view.brand_name == "EnvCo"
    assert view.theme.id == "paper"
    assert view.mark == "$"  # not overridden


def test_invalid_env_override_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_settings(tmp_path, '[branding]\ntheme = "ice"\n')
    monkeypatch.setenv("BSS_BRAND_THEME", "hotdog-stand")
    view = current(root=tmp_path)
    assert view.theme.id == "ice"


def test_logo_resolution(tmp_path: Path) -> None:
    _write_settings(tmp_path, '[branding]\nlogo_image = "logo.png"\n')
    logo_dir = tmp_path / "branding"
    logo_dir.mkdir()
    (logo_dir / "logo.png").write_bytes(PNG_BYTES)
    view = current(root=tmp_path)
    assert view.logo_path == logo_dir / "logo.png"
    assert view.logo_version > 0


def test_logo_configured_but_missing_degrades(tmp_path: Path) -> None:
    _write_settings(tmp_path, '[branding]\nlogo_image = "logo.png"\n')
    view = current(root=tmp_path)
    assert view.logo_path is None
    assert view.logo_version == 0


def test_settings_model_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        BrandingSettings(brand_name="")
    with pytest.raises(ValueError):
        BrandingSettings(brand_name="x" * 41)
    with pytest.raises(ValueError):
        BrandingSettings(theme="neon")
    with pytest.raises(ValueError):
        BrandingSettings(mark="<b>")
    with pytest.raises(ValueError):
        BrandingSettings(mark="toolong")
    with pytest.raises(ValueError):
        BrandingSettings(logo_image="../etc/passwd")
    with pytest.raises(ValueError):
        BrandingSettings(logo_image="logo.svg")
