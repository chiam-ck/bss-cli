//! Read path for the `[branding]` section of `.bss-cli/settings.toml`. Port of
//! `bss_branding.config`.
//!
//! Same contract as [`bss_cockpit::config`](../../bss_cockpit) — one `stat()`
//! per [`current`] call, reload on mtime change, keep serving the last good view
//! on a parse/validation error — with two deliberate deltas (doctrine,
//! phases/V1_8_0.md):
//!
//! * **Never bootstraps files.** `bss_cockpit.config` owns creating
//!   `settings.toml`; this module only reads whatever is there.
//! * **Never crashes on absence.** A container without the `.bss-cli/` mount (or
//!   a fresh checkout before first cockpit boot) gets pure defaults + env
//!   overrides. Branding must never take a service down.
//!
//! Writes stay in `bss_cockpit.config` — the v0.13 "single write path" seam is
//! unchanged; v1.8 only amends the *read* side.
//!
//! Env overrides (`BSS_BRAND_NAME` / `BSS_BRAND_THEME` / `BSS_BRAND_MARK`) are
//! applied inside [`current`] on every call. This is deliberately different from
//! the v0.9 "tokens load once" rule: that rule is about secrets; branding is
//! non-secret preference whose whole point is hot-reload.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Deserialize;

use crate::assets::is_legal_logo_filename;
use crate::marks::validate_mark;
use crate::themes::{ThemePalette, DEFAULT_THEME_ID, THEMES};

pub const DEFAULT_BRAND_NAME: &str = "bss-cli";

/// Subdirectory of the branding dir where the uploaded logo lives.
pub const LOGO_SUBDIR: &str = "branding";

/// The `[branding]` table exactly as it appears in TOML, before validation.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
struct RawBrandingSettings {
    brand_name: String,
    theme: String,
    mark: String,
    logo_image: String,
}

impl Default for RawBrandingSettings {
    fn default() -> Self {
        Self {
            brand_name: DEFAULT_BRAND_NAME.to_string(),
            theme: DEFAULT_THEME_ID.to_string(),
            mark: "$".to_string(),
            logo_image: String::new(),
        }
    }
}

/// Whole-document view so unknown sections (`[llm]`, `[cockpit]`, …) are ignored
/// and a missing `[branding]` table yields defaults (Python's `.get("branding",
/// {})`).
#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct TomlDoc {
    branding: RawBrandingSettings,
}

/// Validated view of the `[branding]` TOML table. Fields are public for the
/// write-side seeding callers (`file_settings`); construction from disk always
/// runs [`BrandingSettings::from_raw`], which enforces the same invariants as
/// the Pydantic field validators.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrandingSettings {
    pub brand_name: String,
    pub theme: String,
    pub mark: String,
    /// `""` = no uploaded logo. Only the fixed filenames the upload handler
    /// writes are legal — never a free-form path.
    pub logo_image: String,
}

impl Default for BrandingSettings {
    fn default() -> Self {
        // The defaults are valid by construction, so we skip re-validation.
        Self {
            brand_name: DEFAULT_BRAND_NAME.to_string(),
            theme: DEFAULT_THEME_ID.to_string(),
            mark: "$".to_string(),
            logo_image: String::new(),
        }
    }
}

impl BrandingSettings {
    fn from_raw(raw: RawBrandingSettings) -> Result<Self, String> {
        let brand_name = {
            let name = raw.brand_name.trim().to_string();
            let len = name.chars().count();
            if !(1..=40).contains(&len) {
                return Err("brand_name must be 1-40 characters".to_string());
            }
            name
        };
        if !THEMES.contains_key(raw.theme.as_str()) {
            let known = THEMES.keys().copied().collect::<Vec<_>>().join(", ");
            return Err(format!(
                "unknown theme {:?} — pick one of: {known}",
                raw.theme
            ));
        }
        let mark = validate_mark(&raw.mark)?;
        if !raw.logo_image.is_empty() && !is_legal_logo_filename(&raw.logo_image) {
            return Err(format!(
                "logo_image must be one of: {} (or empty)",
                crate::assets::LOGO_FILENAMES.join(", ")
            ));
        }
        Ok(Self {
            brand_name,
            theme: raw.theme,
            mark,
            logo_image: raw.logo_image,
        })
    }

    /// Validate an operator-supplied `[branding]` table. Public so the write-side
    /// (cockpit UI, CLI) can validate a form before persisting.
    pub fn validate(
        brand_name: impl Into<String>,
        theme: impl Into<String>,
        mark: impl Into<String>,
        logo_image: impl Into<String>,
    ) -> Result<Self, String> {
        Self::from_raw(RawBrandingSettings {
            brand_name: brand_name.into(),
            theme: theme.into(),
            mark: mark.into(),
            logo_image: logo_image.into(),
        })
    }

    fn to_raw(&self) -> RawBrandingSettings {
        RawBrandingSettings {
            brand_name: self.brand_name.clone(),
            theme: self.theme.clone(),
            mark: self.mark.clone(),
            logo_image: self.logo_image.clone(),
        }
    }
}

/// Resolved branding snapshot handed to renderers.
///
/// `logo_path` is exists-checked at load; `logo_version` is the file's integer
/// mtime for `?v=` cache-busting (0 = no logo).
#[derive(Debug, Clone)]
pub struct BrandingView {
    pub brand_name: String,
    pub theme: ThemePalette,
    pub mark: String,
    pub logo_path: Option<PathBuf>,
    pub logo_version: i64,
}

/// `packages/bss-branding/bss_branding/config.py → parents[3]`; here
/// `rust/crates/bss-branding → ../../..`.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .to_path_buf()
}

/// Where to find `settings.toml` + `branding/logo.*`.
///
/// Resolution order: `BSS_BRANDING_DIR`, then `BSS_COCKPIT_DIR`, then
/// `<repo_root>/.bss-cli` for workspace dev.
pub fn branding_dir() -> PathBuf {
    for var in ["BSS_BRANDING_DIR", "BSS_COCKPIT_DIR"] {
        if let Ok(v) = std::env::var(var) {
            if !v.trim().is_empty() {
                return PathBuf::from(v);
            }
        }
    }
    repo_root().join(".bss-cli")
}

#[derive(Default)]
struct Cache {
    settings: Option<BrandingSettings>,
    mtime: Option<SystemTime>,
    announced: bool,
}

static CACHE: Mutex<Cache> = Mutex::new(Cache {
    settings: None,
    mtime: None,
    announced: false,
});

fn load_settings(settings_path: &Path) -> Result<BrandingSettings, String> {
    let raw = std::fs::read_to_string(settings_path).map_err(|e| e.to_string())?;
    let doc: TomlDoc = toml::from_str(&raw).map_err(|e| e.to_string())?;
    BrandingSettings::from_raw(doc.branding)
}

fn cached_settings(settings_path: &Path) -> BrandingSettings {
    let mtime: Option<SystemTime> = std::fs::metadata(settings_path)
        .and_then(|m| m.modified())
        .ok();

    #[allow(clippy::unwrap_used)]
    let mut cache = CACHE.lock().unwrap();

    if !cache.announced {
        cache.announced = true;
        tracing::info!(
            dir = %settings_path.parent().unwrap_or(settings_path).display(),
            settings_present = mtime.is_some(),
            "branding.dir_resolved",
        );
    }

    if let Some(cached) = &cache.settings {
        if mtime == cache.mtime {
            return cached.clone();
        }
    }

    let fresh = if mtime.is_none() {
        BrandingSettings::default() // absent file → pure defaults
    } else {
        match load_settings(settings_path) {
            Ok(fresh) => fresh,
            Err(err) => {
                if cache.settings.is_none() {
                    // No prior good — defaults, not a crash. Branding must never
                    // take a service down.
                    tracing::warn!(error = %err, "branding.load_failed_using_defaults");
                    BrandingSettings::default()
                } else {
                    tracing::warn!(error = %err, "branding.reload_failed");
                    #[allow(clippy::unwrap_used)]
                    return cache.settings.clone().unwrap();
                }
            }
        }
    };

    cache.settings = Some(fresh.clone());
    cache.mtime = mtime;
    fresh
}

fn apply_env_overrides(settings: BrandingSettings) -> BrandingSettings {
    let mut raw = settings.to_raw();
    let mut any = false;
    for (field, var) in [
        (&mut raw.brand_name, "BSS_BRAND_NAME"),
        (&mut raw.theme, "BSS_BRAND_THEME"),
        (&mut raw.mark, "BSS_BRAND_MARK"),
    ] {
        if let Ok(value) = std::env::var(var) {
            let value = value.trim();
            if !value.is_empty() {
                *field = value.to_string();
                any = true;
            }
        }
    }
    if !any {
        return settings;
    }
    match BrandingSettings::from_raw(raw) {
        Ok(overridden) => overridden,
        Err(err) => {
            tracing::warn!(error = %err, "branding.env_override_invalid_ignored");
            settings
        }
    }
}

/// Return the resolved [`BrandingView`], hot-reloading on change. Cheap enough to
/// call per render / per email send. `root` overrides the auto-located directory
/// (tests).
pub fn current(root: Option<&Path>) -> BrandingView {
    let base = root.map(Path::to_path_buf).unwrap_or_else(branding_dir);
    let settings = apply_env_overrides(cached_settings(&base.join("settings.toml")));

    let mut logo_path: Option<PathBuf> = None;
    let mut logo_version: i64 = 0;
    if !settings.logo_image.is_empty() {
        let candidate = base.join(LOGO_SUBDIR).join(&settings.logo_image);
        if let Ok(mtime) = candidate.metadata().and_then(|m| m.modified()) {
            logo_version = mtime
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            logo_path = Some(candidate);
        }
        // Configured but missing on disk → degrade to the glyph, don't 500.
    }

    #[allow(clippy::unwrap_used)]
    let theme = THEMES.get(settings.theme.as_str()).unwrap().clone();
    BrandingView {
        brand_name: settings.brand_name,
        theme,
        mark: settings.mark,
        logo_path,
        logo_version,
    }
}

/// The `[branding]` table exactly as persisted — no env overrides, no cache.
/// Write-side callers seed their forms/mutations with this so a `BSS_BRAND_*`
/// override is never accidentally baked into the file.
pub fn file_settings(root: Option<&Path>) -> BrandingSettings {
    let base = root.map(Path::to_path_buf).unwrap_or_else(branding_dir);
    load_settings(&base.join("settings.toml")).unwrap_or_default()
}

/// Clear the cache. Tests use this between cases.
pub fn reset_cache() {
    #[allow(clippy::unwrap_used)]
    let mut cache = CACHE.lock().unwrap();
    cache.settings = None;
    cache.mtime = None;
    cache.announced = false;
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn settings_validate_rejects_bad_values() {
        assert!(BrandingSettings::validate("", "phosphor", "$", "").is_err());
        assert!(BrandingSettings::validate("x".repeat(41), "phosphor", "$", "").is_err());
        assert!(BrandingSettings::validate("ok", "neon", "$", "").is_err());
        assert!(BrandingSettings::validate("ok", "phosphor", "<b>", "").is_err());
        assert!(BrandingSettings::validate("ok", "phosphor", "toolong", "").is_err());
        assert!(BrandingSettings::validate("ok", "phosphor", "$", "../etc/passwd").is_err());
        assert!(BrandingSettings::validate("ok", "phosphor", "$", "logo.svg").is_err());
        // Defaults are valid.
        assert!(BrandingSettings::validate("bss-cli", "phosphor", "$", "").is_ok());
        assert!(BrandingSettings::validate("ok", "amber-crt", "\u{25b2}", "logo.png").is_ok());
        // brand_name is stripped.
        assert_eq!(
            BrandingSettings::validate("  Kopi  ", "phosphor", "$", "")
                .unwrap()
                .brand_name,
            "Kopi"
        );
    }
}
