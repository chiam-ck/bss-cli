"""Palette completeness + WCAG contrast assertions.

Contrast thresholds (per phases/V1_8_0.md): body text on elevated
surfaces >= 4.5 (AA normal text); accent on the page background >= 3.0
(AA large text / UI components). A palette that fails here ships
unreadable emails — this test is the gate."""

from __future__ import annotations

import re

from bss_branding import DEFAULT_THEME_ID, THEMES

_HEX = re.compile(r"^#[0-9a-f]{6}$")

_COLOR_FIELDS = (
    "bg",
    "bg_elev",
    "bg_inset",
    "bg_code",
    "fg",
    "fg_muted",
    "fg_dim",
    "accent",
    "accent_bright",
    "accent_dim",
    "accent_amber",
    "accent_alt",
    "accent_error",
    "border",
    "border_strong",
    "on_accent",
)


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_default_theme_exists() -> None:
    assert DEFAULT_THEME_ID in THEMES


def test_all_palettes_complete_lowercase_hex() -> None:
    for theme in THEMES.values():
        assert theme.id and theme.label
        assert theme.rich_accent
        for field in _COLOR_FIELDS:
            value = getattr(theme, field)
            assert _HEX.match(value), f"{theme.id}.{field} = {value!r}"


def test_body_text_contrast_aa() -> None:
    for theme in THEMES.values():
        ratio = _contrast(theme.fg, theme.bg_elev)
        assert ratio >= 4.5, f"{theme.id}: fg/bg_elev = {ratio:.2f}"


def test_accent_contrast() -> None:
    for theme in THEMES.values():
        ratio = _contrast(theme.accent, theme.bg)
        assert ratio >= 3.0, f"{theme.id}: accent/bg = {ratio:.2f}"
        alt_ratio = _contrast(theme.accent_alt, theme.bg)
        assert alt_ratio >= 3.0, f"{theme.id}: accent_alt/bg = {alt_ratio:.2f}"


def test_button_text_readable_on_accent() -> None:
    for theme in THEMES.values():
        ratio = _contrast(theme.on_accent, theme.accent)
        assert ratio >= 3.0, f"{theme.id}: on_accent/accent = {ratio:.2f}"


def test_phosphor_matches_portal_base_css() -> None:
    """The literal fallback in portal_base.css and THEMES['phosphor']
    must never drift — a no-branding deployment renders the CSS file,
    a default-branding deployment renders the injected block."""
    phosphor = THEMES["phosphor"]
    assert phosphor.bg == "#0e1014"
    assert phosphor.bg_elev == "#171a20"
    assert phosphor.bg_inset == "#1f232b"
    assert phosphor.bg_code == "#0a0c0f"
    assert phosphor.fg == "#d8d8d4"
    assert phosphor.fg_muted == "#8a8f99"
    assert phosphor.fg_dim == "#5a5e66"
    assert phosphor.accent == "#74d535"
    assert phosphor.accent_bright == "#9fe870"
    assert phosphor.accent_dim == "#4d8a22"
    assert phosphor.accent_amber == "#ffb454"
    assert phosphor.accent_alt == "#ffb454"  # default look stays identical
    assert phosphor.accent_error == "#ff6b6b"
    assert phosphor.border == "#2a2e38"
    assert phosphor.border_strong == "#3a3f4a"
