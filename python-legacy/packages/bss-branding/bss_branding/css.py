"""Generate the ``:root{…}`` CSS-variable block for the active theme.

Both portals inject this inline *after* their stylesheet links, so it
wins over the literal phosphor fallback in ``portal_base.css`` by
document order. Inline (rather than a CSS route or a generated file)
keeps hot-reload trivial: the block is re-emitted per render.
"""

from __future__ import annotations

from .themes import ThemePalette

# ThemePalette field → CSS custom property. Fonts are not themable.
_CSS_SLOTS: tuple[tuple[str, str], ...] = (
    ("bg", "--bg"),
    ("bg_elev", "--bg-elev"),
    ("bg_inset", "--bg-inset"),
    ("bg_code", "--bg-code"),
    ("fg", "--fg"),
    ("fg_muted", "--fg-muted"),
    ("fg_dim", "--fg-dim"),
    ("accent", "--accent"),
    ("accent_bright", "--accent-bright"),
    ("accent_dim", "--accent-dim"),
    ("accent_amber", "--accent-amber"),
    ("accent_alt", "--accent-alt"),
    ("accent_error", "--accent-error"),
    ("border", "--border"),
    ("border_strong", "--border-strong"),
    ("on_accent", "--on-accent"),
)


def branding_css_block(theme: ThemePalette) -> str:
    """Return ``:root{--bg:#…;…}`` for the given palette."""
    decls = ";".join(f"{prop}:{getattr(theme, field)}" for field, prop in _CSS_SLOTS)
    return f":root{{{decls}}}"
