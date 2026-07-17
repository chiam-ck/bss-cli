//! Curated theme palettes — the single source of truth for colors. Port of
//! `bss_branding.themes`.
//!
//! Every color the product's "face" uses comes from a [`ThemePalette`]: the
//! portals inject the active palette as a `:root{…}` CSS-variable block (see
//! [`crate::css`]), and the email renderers take the same palette per send.
//!
//! Doctrine (phases/V1_8_0.md): dark-only in v1.8; `phosphor` must stay
//! byte-identical to the literal `:root` fallback in
//! `bss-portal-ui/.../portal_base.css` (test-pinned); `on_accent` is the text
//! color painted ON an accent-filled surface; `rich_accent` is the REPL banner's
//! nearest ANSI color name.

use std::sync::LazyLock;

use indexmap::IndexMap;

/// One complete dark palette. Field names mirror the CSS custom properties in
/// `portal_base.css` (`bg_elev` → `--bg-elev`).
// `Serialize` so the cockpit `/settings/branding` page can hand a palette straight
// to its template (theme swatches + the live preview).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ThemePalette {
    pub id: &'static str,
    pub label: &'static str,

    pub bg: &'static str,
    pub bg_elev: &'static str,
    pub bg_inset: &'static str,
    pub bg_code: &'static str,
    pub fg: &'static str,
    pub fg_muted: &'static str,
    pub fg_dim: &'static str,
    pub accent: &'static str,
    pub accent_bright: &'static str,
    pub accent_dim: &'static str,
    pub accent_amber: &'static str,
    pub accent_alt: &'static str,
    pub accent_error: &'static str,
    pub border: &'static str,
    pub border_strong: &'static str,

    pub on_accent: &'static str,
    pub rich_accent: &'static str,
}

pub const DEFAULT_THEME_ID: &str = "phosphor";

// The palettes in insertion order == picker order in the cockpit Branding screen.
const PALETTES: &[ThemePalette] = &[
    ThemePalette {
        id: "phosphor",
        label: "Phosphor (default)",
        bg: "#0e1014",
        bg_elev: "#171a20",
        bg_inset: "#1f232b",
        bg_code: "#0a0c0f",
        fg: "#d8d8d4",
        fg_muted: "#8a8f99",
        fg_dim: "#5a5e66",
        accent: "#74d535",
        accent_bright: "#9fe870",
        accent_dim: "#4d8a22",
        accent_amber: "#ffb454",
        accent_alt: "#ffb454",
        accent_error: "#ff6b6b",
        border: "#2a2e38",
        border_strong: "#3a3f4a",
        on_accent: "#0e1014",
        rich_accent: "green",
    },
    ThemePalette {
        id: "amber-crt",
        label: "Amber CRT",
        bg: "#12100b",
        bg_elev: "#1a1712",
        bg_inset: "#232014",
        bg_code: "#0c0a07",
        fg: "#e8dcc8",
        fg_muted: "#9a8f7a",
        fg_dim: "#6a6152",
        accent: "#ffb000",
        accent_bright: "#ffcf4d",
        accent_dim: "#a87400",
        accent_amber: "#ff8c3f",
        accent_alt: "#ff8c3f",
        accent_error: "#ff6b5e",
        border: "#332d20",
        border_strong: "#453d2c",
        on_accent: "#12100b",
        rich_accent: "yellow",
    },
    ThemePalette {
        id: "ice",
        label: "Ice",
        bg: "#0c1116",
        bg_elev: "#141b22",
        bg_inset: "#1c2530",
        bg_code: "#080d11",
        fg: "#d4dde4",
        fg_muted: "#8595a4",
        fg_dim: "#56626e",
        accent: "#4dc4ff",
        accent_bright: "#85d8ff",
        accent_dim: "#2d7ca6",
        accent_amber: "#ffb454",
        accent_alt: "#9fb8ff",
        accent_error: "#ff6b6b",
        border: "#24303c",
        border_strong: "#34424f",
        on_accent: "#0c1116",
        rich_accent: "cyan",
    },
    ThemePalette {
        id: "magenta",
        label: "Magenta",
        bg: "#120d14",
        bg_elev: "#1a141d",
        bg_inset: "#241b28",
        bg_code: "#0d090f",
        fg: "#e0d6e4",
        fg_muted: "#988aa0",
        fg_dim: "#645a6a",
        accent: "#e85ad1",
        accent_bright: "#f48ae2",
        accent_dim: "#9c3a8c",
        accent_amber: "#ffb454",
        accent_alt: "#b08aff",
        accent_error: "#ff6b6b",
        border: "#302536",
        border_strong: "#423448",
        on_accent: "#120d14",
        rich_accent: "magenta",
    },
    ThemePalette {
        id: "paper",
        label: "Paper (mono)",
        bg: "#101010",
        bg_elev: "#191919",
        bg_inset: "#222222",
        bg_code: "#0a0a0a",
        fg: "#e0e0e0",
        fg_muted: "#969696",
        fg_dim: "#5e5e5e",
        accent: "#f5f5f5",
        accent_bright: "#ffffff",
        accent_dim: "#a8a8a8",
        accent_amber: "#ffb454",
        accent_alt: "#bdbdbd",
        accent_error: "#ff6b6b",
        border: "#2c2c2c",
        border_strong: "#3e3e3e",
        on_accent: "#101010",
        rich_accent: "white",
    },
    ThemePalette {
        id: "solarized-dark",
        label: "Solarized Dark",
        bg: "#002b36",
        bg_elev: "#073642",
        bg_inset: "#0a4250",
        bg_code: "#00212b",
        fg: "#a9b7b7",
        fg_muted: "#748a8a",
        fg_dim: "#586e75",
        accent: "#2aa198",
        accent_bright: "#3fc7bc",
        accent_dim: "#1e7770",
        accent_amber: "#b58900",
        accent_alt: "#b58900",
        accent_error: "#dc322f",
        border: "#0e4a58",
        border_strong: "#17596a",
        on_accent: "#002b36",
        rich_accent: "cyan",
    },
];

/// `id → ThemePalette`, in insertion order (matches Python's dict). Iteration
/// order is the cockpit picker order.
pub static THEMES: LazyLock<IndexMap<&'static str, ThemePalette>> = LazyLock::new(|| {
    PALETTES
        .iter()
        .map(|p| (p.id, p.clone()))
        .collect::<IndexMap<_, _>>()
});

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn six_themes_in_insertion_order() {
        let ids: Vec<&str> = THEMES.keys().copied().collect();
        assert_eq!(
            ids,
            vec![
                "phosphor",
                "amber-crt",
                "ice",
                "magenta",
                "paper",
                "solarized-dark"
            ]
        );
        assert_eq!(DEFAULT_THEME_ID, "phosphor");
        assert!(THEMES.contains_key(DEFAULT_THEME_ID));
    }
}
