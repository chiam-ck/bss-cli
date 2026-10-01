//! Generate the `:root{…}` CSS-variable block for the active theme. Port of
//! `bss_branding.css`.
//!
//! Both portals inject this inline *after* their stylesheet links, so it wins
//! over the literal phosphor fallback in `portal_base.css` by document order.
//! Inline (rather than a route or generated file) keeps hot-reload trivial: the
//! block is re-emitted per render.

use crate::themes::ThemePalette;

/// `(ThemePalette field, CSS custom property)` — fonts are not themable.
type Slot = (fn(&ThemePalette) -> &'static str, &'static str);

const CSS_SLOTS: &[Slot] = &[
    (|t| t.bg, "--bg"),
    (|t| t.bg_elev, "--bg-elev"),
    (|t| t.bg_inset, "--bg-inset"),
    (|t| t.bg_code, "--bg-code"),
    (|t| t.fg, "--fg"),
    (|t| t.fg_muted, "--fg-muted"),
    (|t| t.fg_dim, "--fg-dim"),
    (|t| t.accent, "--accent"),
    (|t| t.accent_bright, "--accent-bright"),
    (|t| t.accent_dim, "--accent-dim"),
    (|t| t.accent_amber, "--accent-amber"),
    (|t| t.accent_alt, "--accent-alt"),
    (|t| t.accent_error, "--accent-error"),
    (|t| t.border, "--border"),
    (|t| t.border_strong, "--border-strong"),
    (|t| t.on_accent, "--on-accent"),
];

/// Return `:root{--bg:#…;…}` for the given palette.
pub fn branding_css_block(theme: &ThemePalette) -> String {
    let decls = CSS_SLOTS
        .iter()
        .map(|(get, prop)| format!("{prop}:{}", get(theme)))
        .collect::<Vec<_>>()
        .join(";");
    format!(":root{{{decls}}}")
}

/// Browser-only appearance rules; unselected/system mode follows the OS even
/// without JavaScript. Explicit selectors override the media-query branch.
pub fn portal_branding_css(theme: &ThemePalette) -> String {
    fn scoped(theme: &ThemePalette, selector: &str, mode: &str) -> String {
        let block = branding_css_block(theme).replacen(":root", selector, 1);
        let semantic = if mode == "light" {
            crate::themes::LIGHT_SURFACES
        } else {
            crate::themes::DARK_SURFACES
        };
        format!("{block}{selector}{{color-scheme:{mode};{semantic}--chat-user-bg:var(--accent);--chat-user-fg:var(--on-accent);}}")
    }
    let light = crate::themes::light_palette(theme);
    format!(
        "{}@media(prefers-color-scheme:light){{{}}}{}{}",
        scoped(theme, ":root", "dark"),
        scoped(&light, ":root:not([data-appearance])", "light"),
        scoped(theme, ":root[data-appearance=dark]", "dark"),
        scoped(&light, ":root[data-appearance=light]", "light"),
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::themes::THEMES;

    #[test]
    fn block_shape() {
        let block = branding_css_block(&THEMES["phosphor"]);
        assert!(block.starts_with(":root{"));
        assert!(block.ends_with('}'));
        assert!(block.contains("--accent:#74d535"));
        assert!(block.contains("--on-accent:#0e1014"));
    }

    #[test]
    fn all_sixteen_slots_present_for_every_theme() {
        let props = [
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
        ];
        for theme in THEMES.values() {
            let block = branding_css_block(theme);
            // 16 declarations + the `:root` colon.
            assert_eq!(block.matches(':').count(), 16 + 1);
            for p in props {
                assert!(block.contains(&format!("{p}:")), "{} missing {p}", theme.id);
            }
        }
    }

    /// Doctrine pin: the phosphor palette's values must stay byte-identical to
    /// the literal `:root` fallback in `bss-portal-ui/.../portal_base.css`. We
    /// assert the exact generated block so a palette edit that diverges from the
    /// hand-written fallback fails here (the portal_base.css literal is the
    /// no-branding render).
    #[test]
    fn phosphor_block_is_exact() {
        let block = branding_css_block(&THEMES["phosphor"]);
        assert_eq!(
            block,
            ":root{--bg:#0e1014;--bg-elev:#171a20;--bg-inset:#1f232b;\
             --bg-code:#0a0c0f;--fg:#d8d8d4;--fg-muted:#8a8f99;--fg-dim:#5a5e66;\
             --accent:#74d535;--accent-bright:#9fe870;--accent-dim:#4d8a22;\
             --accent-amber:#ffb454;--accent-alt:#ffb454;--accent-error:#ff6b6b;\
             --border:#2a2e38;--border-strong:#3a3f4a;--on-accent:#0e1014}"
        );
    }
}

#[cfg(test)]
mod appearance_tests {
    use super::*;
    #[test]
    fn browser_rules_support_os_and_explicit_modes_without_changing_email_css() {
        for dark in crate::themes::THEMES.values() {
            let css = portal_branding_css(dark);
            assert!(css.contains("@media(prefers-color-scheme:light)"));
            assert!(css.contains(":root:not([data-appearance])"));
            assert!(css.contains(":root[data-appearance=dark]"));
            assert!(css.contains(":root[data-appearance=light]"));
            assert!(css.contains("color-scheme:light"));
            assert!(css.contains("--chat-user-fg:var(--on-accent)"));
            assert!(css.contains(&branding_css_block(dark)));
            assert!(!branding_css_block(dark).contains("color-scheme"));
        }
    }
}
