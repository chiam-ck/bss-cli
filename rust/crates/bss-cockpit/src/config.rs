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
/// (`rust/crates/bss-cockpit` → repo root); only used when `BSS_COCKPIT_DIR` is
/// unset (deployed cockpit containers always set it to a bind-mounted volume).
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
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
