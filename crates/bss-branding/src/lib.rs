//! bss-branding — operator branding for BSS-CLI (v1.8). Rust port of
//! `packages/bss-branding`.
//!
//! Read path + palette definitions only. All writes to `settings.toml` and the
//! logo file live in `bss_cockpit::config` (the v0.13 single write path, amended
//! v1.8 for the `[branding]` section's read side).
//!
//! Doctrine (CLAUDE.md v1.8): dark-only themes; `phosphor` stays byte-identical
//! to the `portal_base.css` `:root` fallback (test-pinned in [`css`]); logo
//! uploads are magic-byte-sniffed PNG/JPEG/WebP, never SVG; the brand name +
//! mark are operator input (escape at every hand-built-HTML seam; [`validate_mark`]
//! rejects HTML-active characters outright); `BSS_BRAND_*` env overrides are
//! deliberately re-read per render.
#![forbid(unsafe_code)]

pub mod assets;
pub mod config;
pub mod css;
pub mod logo;
pub mod marks;
pub mod themes;

pub use assets::{
    content_type_by_filename, is_legal_logo_filename, sniff_image_type, ImageType, LOGO_FILENAMES,
    MAX_LOGO_BYTES,
};
pub use config::{
    branding_dir, current, file_settings, reset_cache, BrandingSettings, BrandingView,
    DEFAULT_BRAND_NAME, LOGO_SUBDIR,
};
pub use css::branding_css_block;
pub use logo::{logo_http, LogoHttp};
pub use marks::{validate_mark, LOGO_MARKS};
pub use themes::{ThemePalette, DEFAULT_THEME_ID, THEMES};
