//! `OPERATOR.md` + `settings.toml` loader with mtime-based hot-reload. Port of
//! the read path of `packages/bss-cockpit/bss_cockpit/config.py`.
//!
//! Two operator-editable files under `.bss-cli/`:
//! * `OPERATOR.md` — persona + house rules, prepended verbatim to the prompt.
//! * `settings.toml` — machine-tunable, non-secret preference, validated on load.
//!
//! Doctrine: the mtime check runs per [`current`] call (cheap `stat`, no watcher).
//! On a parse/validation failure the loader serves the last-good view and logs a
//! warning (an editor typo must not brick the cockpit). Secrets never live here.
//!
//! Deferred to P6 (lands with `bss-branding`): the `[branding]` settings field
//! and every `write_*` helper (the WebUI `/settings` write side). The `[branding]`
//! table in `settings.toml` is simply ignored on load until then (serde skips
//! unknown fields), so an operator's existing file loads unchanged.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use chrono::{DateTime, Utc};
use serde::Deserialize;

/// Audit-attribution actor. The cockpit is single-operator-by-design behind a
/// secure perimeter; the name is always `"operator"` (v0.13.1 — removed from
/// settings.toml to kill cross-surface actor drift).
pub const OPERATOR_ACTOR: &str = "operator";

const DEFAULT_OPERATOR_MD: &str = include_str!("default_operator.md");
const DEFAULT_SETTINGS_TOML: &str = include_str!("default_settings.toml");

// ── settings.toml schema ────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct LlmSection {
    pub model: Option<String>,
    pub temperature: f64,
}

impl Default for LlmSection {
    fn default() -> Self {
        Self {
            model: None,
            temperature: 0.2,
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct CockpitSection {
    pub allow_destructive_default: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct PortsSection {
    pub csr_portal: u16,
}

impl Default for PortsSection {
    fn default() -> Self {
        Self { csr_portal: 9002 }
    }
}

/// Validated view of `.bss-cli/settings.toml`. Sections are discrete so a typo
/// is locatable by section name. Unknown tables (e.g. `[branding]`) are ignored
/// until P6.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct CockpitSettings {
    pub llm: LlmSection,
    pub cockpit: CockpitSection,
    pub ports: PortsSection,
    pub dev_service_urls: HashMap<String, String>,
}

/// Snapshot of the two operator files. Returned by [`current`].
#[derive(Debug, Clone)]
pub struct CockpitConfig {
    pub operator_md: String,
    pub settings: CockpitSettings,
    pub last_loaded_at: DateTime<Utc>,
    pub operator_md_path: PathBuf,
    pub settings_path: PathBuf,
}

/// Errors from [`current`]. `Load` only escapes on a first-load failure (no prior
/// good); once a good config is cached, reload failures serve the cache instead.
#[derive(Debug)]
pub enum ConfigError {
    Io(std::io::Error),
    Parse(String),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io(e) => write!(f, "cockpit config io error: {e}"),
            ConfigError::Parse(e) => write!(f, "cockpit settings.toml invalid: {e}"),
        }
    }
}

impl std::error::Error for ConfigError {}

// ── loader + cache ──────────────────────────────────────────────────────────

#[derive(Default)]
struct Cache {
    config: Option<CockpitConfig>,
    operator_mtime: Option<SystemTime>,
    settings_mtime: Option<SystemTime>,
}

static CACHE: Mutex<Cache> = Mutex::new(Cache {
    config: None,
    operator_mtime: None,
    settings_mtime: None,
});

/// Repo-root fallback for the dev `.bss-cli/` location. Compile-time path
/// (`crates/bss-cockpit` → repo root); only used when `BSS_COCKPIT_DIR` is
/// unset (deployed cockpit containers always set it to a bind-mounted volume).
/// Was `../../..` pre-flip (`rust/crates/bss-cockpit`); the flip moved the crate
/// up one level, so it's now `../..`.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .to_path_buf()
}

/// Where to find `OPERATOR.md` + `settings.toml`: `BSS_COCKPIT_DIR` if set, else
/// `<repo_root>/.bss-cli`.
fn bss_cli_dir() -> PathBuf {
    match std::env::var("BSS_COCKPIT_DIR") {
        Ok(v) if !v.trim().is_empty() => PathBuf::from(v),
        _ => repo_root().join(".bss-cli"),
    }
}

/// The `.bss-cli` config directory (`BSS_COCKPIT_DIR` override, else
/// `<repo_root>/.bss-cli`). Exposed so the REPL's `/config edit` / `/operator edit`
/// can open `settings.toml` / `OPERATOR.md` in `$EDITOR`; programmatic reads still go
/// through [`current`] (the mtime hot-reload contract).
pub fn config_dir(root: Option<&Path>) -> PathBuf {
    root.map(Path::to_path_buf).unwrap_or_else(bss_cli_dir)
}

fn embedded_default(filename: &str) -> Option<&'static str> {
    match filename {
        "OPERATOR.md" => Some(DEFAULT_OPERATOR_MD),
        "settings.toml" => Some(DEFAULT_SETTINGS_TOML),
        _ => None,
    }
}

/// Materialize `path` from a sibling `.template`, or from the embedded package
/// default. Idempotent (returns if `path` exists); creates the parent dir.
fn autobootstrap_if_missing(path: &Path) -> Result<(), std::io::Error> {
    if path.exists() {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let filename = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
    let template = path.with_file_name(format!("{filename}.template"));
    if template.exists() {
        std::fs::copy(&template, path)?;
        tracing::info!(target = %path.display(), source = %template.display(), "cockpit.config.autobootstrap");
        return Ok(());
    }
    if let Some(embedded) = embedded_default(filename) {
        std::fs::write(path, embedded)?;
        tracing::info!(target = %path.display(), "cockpit.config.autobootstrap_embedded");
    }
    Ok(())
}

fn load_from_disk(
    operator_md_path: &Path,
    settings_path: &Path,
) -> Result<CockpitConfig, ConfigError> {
    let operator_md = std::fs::read_to_string(operator_md_path).map_err(ConfigError::Io)?;
    let raw = std::fs::read_to_string(settings_path).map_err(ConfigError::Io)?;
    let settings: CockpitSettings =
        toml::from_str(&raw).map_err(|e| ConfigError::Parse(e.to_string()))?;
    Ok(CockpitConfig {
        operator_md,
        settings,
        last_loaded_at: bss_clock::now(),
        operator_md_path: operator_md_path.to_path_buf(),
        settings_path: settings_path.to_path_buf(),
    })
}

/// Return the current [`CockpitConfig`], reloading on mtime change. First call
/// autobootstraps both files. On a parse/validation failure the last-good view
/// is served (unless there is none, in which case the error escapes). `root`
/// overrides the auto-located `.bss-cli/` (used in tests).
pub fn current(root: Option<&Path>) -> Result<CockpitConfig, ConfigError> {
    let bss_cli = root.map(Path::to_path_buf).unwrap_or_else(bss_cli_dir);
    let operator_md_path = bss_cli.join("OPERATOR.md");
    let settings_path = bss_cli.join("settings.toml");

    autobootstrap_if_missing(&operator_md_path).map_err(ConfigError::Io)?;
    autobootstrap_if_missing(&settings_path).map_err(ConfigError::Io)?;

    let op_mtime = std::fs::metadata(&operator_md_path)
        .and_then(|m| m.modified())
        .map_err(ConfigError::Io)?;
    let cf_mtime = std::fs::metadata(&settings_path)
        .and_then(|m| m.modified())
        .map_err(ConfigError::Io)?;

    #[allow(clippy::unwrap_used)]
    let mut cache = CACHE.lock().unwrap();
    let fresh_enough = cache.config.is_some()
        && cache.operator_mtime.is_some_and(|c| op_mtime <= c)
        && cache.settings_mtime.is_some_and(|c| cf_mtime <= c);
    if fresh_enough {
        if let Some(cfg) = &cache.config {
            return Ok(cfg.clone());
        }
    }

    match load_from_disk(&operator_md_path, &settings_path) {
        Ok(fresh) => {
            cache.config = Some(fresh.clone());
            cache.operator_mtime = Some(op_mtime);
            cache.settings_mtime = Some(cf_mtime);
            Ok(fresh)
        }
        Err(e) => {
            // No prior good — surface the failure; the operator must fix the file.
            let Some(cached) = &cache.config else {
                return Err(e);
            };
            tracing::warn!(
                error = %e,
                serving_last_good = %cached.last_loaded_at,
                "cockpit.config.reload_failed"
            );
            Ok(cached.clone())
        }
    }
}

/// Clear the cache. Tests use this between cases.
pub fn reset_cache() {
    #[allow(clippy::unwrap_used)]
    let mut cache = CACHE.lock().unwrap();
    *cache = Cache::default();
}

// ── write side (v0.13 PR8 + v1.8 branding) ──────────────────────────────────
//
// The WebUI `/settings` + `/settings/branding` pages are the only write path to
// these files outside the REPL's `/operator edit` / `/config edit` commands. Each
// writer is the validation gate — bypassing it risks an operator typo bricking the
// cockpit. Port of the `write_*` helpers in `bss_cockpit.config`.

/// Errors from the write side. `Validation` carries the parser/validator's own
/// message, which the WebUI echoes verbatim in its 400 page.
#[derive(Debug)]
pub enum WriteError {
    Io(std::io::Error),
    Validation(String),
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::Io(e) => write!(f, "{e}"),
            WriteError::Validation(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for WriteError {}

fn resolve_dir(root: Option<&Path>) -> PathBuf {
    root.map(Path::to_path_buf).unwrap_or_else(bss_cli_dir)
}

/// Persist new `OPERATOR.md`. Validation is only "non-empty after trim" (anything
/// fancier — markdown lint — is out of scope), then the cache is invalidated.
pub fn write_operator_md(content: &str, root: Option<&Path>) -> Result<(), WriteError> {
    if content.trim().is_empty() {
        return Err(WriteError::Validation(
            "OPERATOR.md cannot be empty".to_string(),
        ));
    }
    let bss_cli = resolve_dir(root);
    std::fs::create_dir_all(&bss_cli).map_err(WriteError::Io)?;
    std::fs::write(bss_cli.join("OPERATOR.md"), content).map_err(WriteError::Io)?;
    reset_cache();
    Ok(())
}

/// Persist new `settings.toml`, validating it parses into [`CockpitSettings`]
/// first; then invalidate both this cache and the branding read cache (a raw edit
/// can change `[branding]`). Returns the validated settings so the WebUI can echo
/// them.
pub fn write_settings_toml(
    content: &str,
    root: Option<&Path>,
) -> Result<CockpitSettings, WriteError> {
    let validated: CockpitSettings =
        toml::from_str(content).map_err(|e| WriteError::Validation(e.to_string()))?;
    let bss_cli = resolve_dir(root);
    std::fs::create_dir_all(&bss_cli).map_err(WriteError::Io)?;
    std::fs::write(bss_cli.join("settings.toml"), content).map_err(WriteError::Io)?;
    reset_cache();
    bss_branding::reset_cache();
    Ok(validated)
}

/// Replace the `[branding]` table, preserving the rest of the file. `toml_edit`
/// round-trips the document so operator comments in other sections ([llm],
/// [dev_service_urls], …) survive; comments *inside* [branding] are machine-owned
/// and replaced wholesale. The whole document is re-validated before it hits disk.
pub fn write_branding_settings(
    update: &bss_branding::BrandingSettings,
    root: Option<&Path>,
) -> Result<(), WriteError> {
    let bss_cli = resolve_dir(root);
    let settings_path = bss_cli.join("settings.toml");
    autobootstrap_if_missing(&settings_path).map_err(WriteError::Io)?;

    let existing = std::fs::read_to_string(&settings_path).map_err(WriteError::Io)?;
    let mut doc = existing
        .parse::<toml_edit::DocumentMut>()
        .map_err(|e| WriteError::Validation(e.to_string()))?;

    let mut table = toml_edit::Table::new();
    table["brand_name"] = toml_edit::value(update.brand_name.clone());
    table["theme"] = toml_edit::value(update.theme.clone());
    table["mark"] = toml_edit::value(update.mark.clone());
    table["logo_image"] = toml_edit::value(update.logo_image.clone());
    doc["branding"] = toml_edit::Item::Table(table);

    let content = doc.to_string();
    // Re-validate the whole document parses (the branding fields are already
    // validated by `BrandingSettings::validate` at the route; this catches a
    // corrupt sibling section).
    toml::from_str::<CockpitSettings>(&content)
        .map_err(|e| WriteError::Validation(e.to_string()))?;
    std::fs::write(&settings_path, content).map_err(WriteError::Io)?;
    reset_cache();
    bss_branding::reset_cache();
    tracing::info!(theme = %update.theme, mark = %update.mark, "cockpit.branding.settings_saved");
    Ok(())
}

/// Persist an uploaded logo image; returns the fixed filename. The bytes decide
/// everything: a magic-byte sniff picks the type (PNG/JPEG/WebP only — never SVG),
/// the type picks the fixed filename. No user-controlled path component ever
/// reaches the filesystem; stale siblings of other extensions are removed. Logs
/// size + type only.
pub fn write_branding_logo(data: &[u8], root: Option<&Path>) -> Result<String, WriteError> {
    use bss_branding::{sniff_image_type, MAX_LOGO_BYTES};

    if data.len() > MAX_LOGO_BYTES {
        return Err(WriteError::Validation(format!(
            "logo is {} bytes — the cap is {MAX_LOGO_BYTES} (256 KB)",
            data.len()
        )));
    }
    let kind = sniff_image_type(data).ok_or_else(|| {
        WriteError::Validation(
            "logo must be a PNG, JPEG or WebP image (SVG is not accepted)".to_string(),
        )
    })?;

    let bss_cli = resolve_dir(root);
    let logo_dir = bss_cli.join(bss_branding::LOGO_SUBDIR);
    std::fs::create_dir_all(&logo_dir).map_err(WriteError::Io)?;
    let filename = kind.filename();
    std::fs::write(logo_dir.join(filename), data).map_err(WriteError::Io)?;
    // Remove stale siblings so `logo_image` always names the only file present.
    for stale in bss_branding::LOGO_FILENAMES {
        if *stale != filename {
            let _ = std::fs::remove_file(logo_dir.join(stale));
        }
    }

    let branding = bss_branding::file_settings(Some(&bss_cli));
    let update = bss_branding::BrandingSettings {
        logo_image: filename.to_string(),
        ..branding
    };
    write_branding_settings(&update, Some(&bss_cli))?;
    tracing::info!(size = data.len(), file = %filename, "cockpit.branding.logo_saved");
    Ok(filename.to_string())
}

/// Delete the uploaded logo and clear `logo_image`. Portals fall back to the text
/// mark on the next render.
pub fn remove_branding_logo(root: Option<&Path>) -> Result<(), WriteError> {
    let bss_cli = resolve_dir(root);
    let logo_dir = bss_cli.join(bss_branding::LOGO_SUBDIR);
    for filename in bss_branding::LOGO_FILENAMES {
        let _ = std::fs::remove_file(logo_dir.join(filename));
    }
    let branding = bss_branding::file_settings(Some(&bss_cli));
    let update = bss_branding::BrandingSettings {
        logo_image: String::new(),
        ..branding
    };
    write_branding_settings(&update, Some(&bss_cli))?;
    tracing::info!("cockpit.branding.logo_removed");
    Ok(())
}

#[cfg(test)]
mod write_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    /// A unique temp dir under the OS temp root. `std::env::temp_dir` + a nonce
    /// avoids pulling in a `tempfile` dev-dep just for these.
    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bss-cockpit-write-{tag}-{}-{}",
            std::process::id(),
            bss_clock::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn operator_md_rejects_empty_and_persists() {
        let dir = scratch("op");
        assert!(write_operator_md("   \n ", Some(&dir)).is_err());
        write_operator_md("# House rules\nBe kind.", Some(&dir)).unwrap();
        let got = std::fs::read_to_string(dir.join("OPERATOR.md")).unwrap();
        assert_eq!(got, "# House rules\nBe kind.");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn settings_toml_rejects_bad_and_persists_good() {
        let dir = scratch("set");
        // A type mismatch fails validation (temperature must be a number).
        assert!(write_settings_toml("[llm]\ntemperature = \"hot\"\n", Some(&dir)).is_err());
        let good = "[llm]\nmodel = \"deepseek/deepseek-v4-pro\"\ntemperature = 0.2\n";
        let validated = write_settings_toml(good, Some(&dir)).unwrap();
        assert_eq!(
            validated.llm.model.as_deref(),
            Some("deepseek/deepseek-v4-pro")
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The v1.8 doctrine property: a `[branding]` save preserves operator comments
    /// and values in every OTHER section.
    #[test]
    fn branding_save_preserves_other_sections_and_comments() {
        let dir = scratch("brand");
        let original = "\
# my house model
[llm]
model = \"deepseek/deepseek-v4-pro\"  # do not change
temperature = 0.3

[dev_service_urls]
crm = \"http://crm:8000\"
";
        std::fs::write(dir.join("settings.toml"), original).unwrap();

        let update = bss_branding::BrandingSettings::validate("Octopus", "phosphor", "$", "")
            .expect("valid branding");
        write_branding_settings(&update, Some(&dir)).unwrap();

        let after = std::fs::read_to_string(dir.join("settings.toml")).unwrap();
        // Other sections + their comments survive verbatim.
        assert!(after.contains("# my house model"), "{after}");
        assert!(
            after.contains("model = \"deepseek/deepseek-v4-pro\"  # do not change"),
            "{after}"
        );
        assert!(after.contains("crm = \"http://crm:8000\""), "{after}");
        // The new branding table landed.
        assert!(after.contains("[branding]"), "{after}");
        assert!(after.contains("brand_name = \"Octopus\""), "{after}");
        // And it still parses back as a whole.
        let reparsed = bss_branding::file_settings(Some(&dir));
        assert_eq!(reparsed.brand_name, "Octopus");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn logo_write_sniffs_and_rejects_non_image() {
        let dir = scratch("logo");
        std::fs::write(dir.join("settings.toml"), "[llm]\ntemperature = 0.2\n").unwrap();
        // A bare text blob is not a PNG/JPEG/WebP.
        assert!(write_branding_logo(b"<svg></svg>", Some(&dir)).is_err());
        // A minimal PNG signature is accepted and writes logo.png.
        let png = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0];
        let name = write_branding_logo(&png, Some(&dir)).unwrap();
        assert_eq!(name, "logo.png");
        assert!(dir.join("branding/logo.png").exists());
        // Clearing removes the file and blanks logo_image.
        remove_branding_logo(Some(&dir)).unwrap();
        assert!(!dir.join("branding/logo.png").exists());
        assert_eq!(bss_branding::file_settings(Some(&dir)).logo_image, "");
        std::fs::remove_dir_all(&dir).ok();
    }
}
