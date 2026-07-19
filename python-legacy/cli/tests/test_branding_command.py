"""v1.8 — `bss branding` command group + REPL banner branding.

The CLI verbs are thin wrappers over bss_cockpit.config writers (the
validation gate); the banner reads bss_branding.current() per render
with the version demoted to a dim product-attribution footnote.
"""

from __future__ import annotations

from pathlib import Path

import bss_branding
import pytest
from bss_cockpit import config as cockpit_config
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def branding_root(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "OPERATOR.md").write_text("# op\n", encoding="utf-8")
    (tmp_path / "settings.toml").write_text("[llm]\ntemperature = 0.2\n", encoding="utf-8")
    monkeypatch.setattr(cockpit_config, "_bss_cli_dir", lambda: tmp_path)
    monkeypatch.setenv("BSS_BRANDING_DIR", str(tmp_path))
    for var in ("BSS_BRAND_NAME", "BSS_BRAND_THEME", "BSS_BRAND_MARK"):
        monkeypatch.delenv(var, raising=False)
    cockpit_config.reset_cache()
    bss_branding.reset_cache()
    yield tmp_path
    cockpit_config.reset_cache()
    bss_branding.reset_cache()


def _app():
    from bss_cli.commands.branding import app

    return app


def test_show_defaults(branding_root) -> None:
    result = runner.invoke(_app(), ["show"])
    assert result.exit_code == 0
    assert "bss-cli" in result.output
    assert "phosphor" in result.output


def test_set_theme_persists(branding_root: Path) -> None:
    result = runner.invoke(_app(), ["set-theme", "solarized-dark"])
    assert result.exit_code == 0
    assert 'theme = "solarized-dark"' in (branding_root / "settings.toml").read_text()
    assert bss_branding.current(root=branding_root).theme.id == "solarized-dark"


def test_set_theme_rejects_unknown(branding_root) -> None:
    result = runner.invoke(_app(), ["set-theme", "hotdog-stand"])
    assert result.exit_code == 1
    assert "unknown theme" in result.output


def test_set_name_and_mark(branding_root: Path) -> None:
    assert runner.invoke(_app(), ["set-name", "Kopi Mobile"]).exit_code == 0
    assert runner.invoke(_app(), ["set-mark", "✦"]).exit_code == 0
    view = bss_branding.current(root=branding_root)
    assert view.brand_name == "Kopi Mobile"
    assert view.mark == "✦"


def test_set_mark_rejects_html(branding_root) -> None:
    result = runner.invoke(_app(), ["set-mark", "<b>"])
    assert result.exit_code == 1


def test_themes_lists_all(branding_root) -> None:
    result = runner.invoke(_app(), ["themes"])
    assert result.exit_code == 0
    for theme_id in ("phosphor", "amber-crt", "ice", "magenta", "paper", "solarized-dark"):
        assert theme_id in result.output


def test_banner_brand_and_demoted_version(branding_root, monkeypatch) -> None:
    from bss_cli import repl
    from rich.console import Console

    (branding_root / "settings.toml").write_text(
        '[llm]\ntemperature = 0.2\n\n[branding]\nbrand_name = "Kopi Mobile"\ntheme = "ice"\n',
        encoding="utf-8",
    )
    bss_branding.reset_cache()
    panel = repl._render_banner(
        actor="operator",
        model="test-model",
        session_id="SES-1",
        customer_focus=None,
        allow_destructive_default=False,
    )
    console = Console(width=120, record=True)
    console.print(panel)
    text = console.export_text()
    assert "Kopi Mobile" in text
    # Version demoted: only the product-attribution footnote carries it.
    assert f"bss-cli v{repl.BSS_RELEASE}" in text
    assert f"operator cockpit v{repl.BSS_RELEASE}" not in text
