from __future__ import annotations

from bss_branding import THEMES, branding_css_block


def test_block_shape() -> None:
    block = branding_css_block(THEMES["phosphor"])
    assert block.startswith(":root{")
    assert block.endswith("}")
    assert "--accent:#74d535" in block
    assert "--on-accent:#0e1014" in block


def test_all_fifteen_slots_present_for_every_theme() -> None:
    for theme in THEMES.values():
        block = branding_css_block(theme)
        assert block.count(":") == 16 + 1  # 16 declarations + :root
        for prop in (
            "--bg",
            "--bg-elev",
            "--bg-inset",
            "--bg-code",
            "--fg",
            "--fg-muted",
            "--fg-dim",
            "--accent",
            "--accent-bright",
            "--accent-dim",
            "--accent-amber",
            "--accent-alt",
            "--accent-error",
            "--border",
            "--border-strong",
            "--on-accent",
        ):
            assert f"{prop}:" in block, f"{theme.id} missing {prop}"
