"""Curated theme palettes — the single source of truth for colors.

Every color the product's "face" uses comes from a ``ThemePalette``:
the portals inject the active palette as a ``:root{…}`` CSS-variable
block (see :mod:`bss_branding.css`), and the email renderers in
``bss-portal-auth`` take the same palette per send. There is no other
sanctioned home for a hex literal — ``make doctrine-check`` greps
``email.py`` for stray hex to enforce this (v1.8).

Doctrine notes (per phases/V1_8_0.md):

* Dark-only in v1.8. Every component stylesheet in both portals
  assumes a dark background; a light theme needs a component audit
  and is deliberately out of scope.
* ``phosphor`` must stay byte-identical to the literal ``:root``
  fallback in ``bss-portal-ui/static/css/portal_base.css`` — that file
  is what a no-branding deployment renders.
* ``on_accent`` is the text color painted ON an accent-filled surface
  (buttons, CTAs). It replaces the ``#0e1014`` literal that used to be
  inlined in the email button markup.
* ``rich_accent`` is a Rich style name for the REPL banner — terminals
  don't do hex reliably across profiles, so each palette nominates its
  nearest ANSI color.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    """One complete dark palette. Field names mirror the CSS custom
    properties in ``portal_base.css`` (``bg_elev`` → ``--bg-elev``)."""

    id: str
    label: str

    bg: str
    bg_elev: str
    bg_inset: str
    bg_code: str
    fg: str
    fg_muted: str
    fg_dim: str
    accent: str
    accent_bright: str
    accent_dim: str
    accent_amber: str
    accent_error: str
    border: str
    border_strong: str

    on_accent: str
    rich_accent: str


# Insertion order == picker order in the cockpit Branding screen.
THEMES: dict[str, ThemePalette] = {
    t.id: t
    for t in (
        ThemePalette(
            id="phosphor",
            label="Phosphor (default)",
            bg="#0e1014",
            bg_elev="#171a20",
            bg_inset="#1f232b",
            bg_code="#0a0c0f",
            fg="#d8d8d4",
            fg_muted="#8a8f99",
            fg_dim="#5a5e66",
            accent="#74d535",
            accent_bright="#9fe870",
            accent_dim="#4d8a22",
            accent_amber="#ffb454",
            accent_error="#ff6b6b",
            border="#2a2e38",
            border_strong="#3a3f4a",
            on_accent="#0e1014",
            rich_accent="green",
        ),
        ThemePalette(
            id="amber-crt",
            label="Amber CRT",
            bg="#12100b",
            bg_elev="#1a1712",
            bg_inset="#232014",
            bg_code="#0c0a07",
            fg="#e8dcc8",
            fg_muted="#9a8f7a",
            fg_dim="#6a6152",
            accent="#ffb000",
            accent_bright="#ffcf4d",
            accent_dim="#a87400",
            accent_amber="#ff8c3f",
            accent_error="#ff6b5e",
            border="#332d20",
            border_strong="#453d2c",
            on_accent="#12100b",
            rich_accent="yellow",
        ),
        ThemePalette(
            id="ice",
            label="Ice",
            bg="#0c1116",
            bg_elev="#141b22",
            bg_inset="#1c2530",
            bg_code="#080d11",
            fg="#d4dde4",
            fg_muted="#8595a4",
            fg_dim="#56626e",
            accent="#4dc4ff",
            accent_bright="#85d8ff",
            accent_dim="#2d7ca6",
            accent_amber="#ffb454",
            accent_error="#ff6b6b",
            border="#24303c",
            border_strong="#34424f",
            on_accent="#0c1116",
            rich_accent="cyan",
        ),
        ThemePalette(
            id="magenta",
            label="Magenta",
            bg="#120d14",
            bg_elev="#1a141d",
            bg_inset="#241b28",
            bg_code="#0d090f",
            fg="#e0d6e4",
            fg_muted="#988aa0",
            fg_dim="#645a6a",
            accent="#e85ad1",
            accent_bright="#f48ae2",
            accent_dim="#9c3a8c",
            accent_amber="#ffb454",
            accent_error="#ff6b6b",
            border="#302536",
            border_strong="#423448",
            on_accent="#120d14",
            rich_accent="magenta",
        ),
        ThemePalette(
            id="paper",
            label="Paper (mono)",
            bg="#101010",
            bg_elev="#191919",
            bg_inset="#222222",
            bg_code="#0a0a0a",
            fg="#e0e0e0",
            fg_muted="#969696",
            fg_dim="#5e5e5e",
            accent="#f5f5f5",
            accent_bright="#ffffff",
            accent_dim="#a8a8a8",
            accent_amber="#ffb454",
            accent_error="#ff6b6b",
            border="#2c2c2c",
            border_strong="#3e3e3e",
            on_accent="#101010",
            rich_accent="white",
        ),
        ThemePalette(
            id="solarized-dark",
            label="Solarized Dark",
            bg="#002b36",
            bg_elev="#073642",
            bg_inset="#0a4250",
            bg_code="#00212b",
            fg="#a9b7b7",
            fg_muted="#748a8a",
            fg_dim="#586e75",
            accent="#2aa198",
            accent_bright="#3fc7bc",
            accent_dim="#1e7770",
            accent_amber="#b58900",
            accent_error="#dc322f",
            border="#0e4a58",
            border_strong="#17596a",
            on_accent="#002b36",
            rich_accent="cyan",
        ),
    )
}

DEFAULT_THEME_ID = "phosphor"
