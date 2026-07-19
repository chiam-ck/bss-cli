//! MiniJinja environment + branding globals. Port of `bss_csr.templating`.
//!
//! Same shape as the self-serve portal's: the Rust cockpit **reuses the existing
//! Jinja templates** unchanged via a two-directory loader mirroring the Python
//! `ChoiceLoader` — the portal's own `templates/` first, then `bss_portal_ui`'s
//! shared `templates/` (the agent-log widget + `agent_event.html`, shared with
//! self-serve).
//!
//! Branding globals (`branding()` / `branding_style()`) are **functions**,
//! evaluated per render, so a `settings.toml` theme change hot-reloads on the next
//! request. `bss_release` is the footer footnote (product attribution — never the
//! header brand tag; v1.8 doctrine). `asset_v` is the process-start cache-buster.
//!
//! The CRM screens' `fmt_dt` / `tone` filters come from [`crate::views`].

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use minijinja::{Environment, Value};
use serde::Serialize;

use bss_models::BSS_RELEASE;

/// `<repo>/rust/portals/csr` → repo root (`../../..`).
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."))
}

fn local_template_dir() -> PathBuf {
    match std::env::var("BSS_CSR_TEMPLATE_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("portals/csr/bss_csr/templates"),
    }
}

fn shared_template_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_SHARED_TEMPLATE_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("packages/bss-portal-ui/bss_portal_ui/templates"),
    }
}

pub fn local_static_dir() -> PathBuf {
    match std::env::var("BSS_CSR_STATIC_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("portals/csr/bss_csr/static"),
    }
}

pub fn shared_static_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_SHARED_STATIC_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("packages/bss-portal-ui/bss_portal_ui/static"),
    }
}

/// The branding view exposed to templates (`branding().brand_name` etc.).
#[derive(Serialize)]
struct BrandingCtx {
    brand_name: String,
    mark: String,
    logo_version: i64,
    theme: ThemeCtx,
}

#[derive(Serialize)]
struct ThemeCtx {
    id: String,
    label: String,
}

fn branding_value() -> Value {
    let view = bss_branding::current(None);
    Value::from_serialize(&BrandingCtx {
        brand_name: view.brand_name,
        mark: view.mark,
        logo_version: view.logo_version,
        theme: ThemeCtx {
            id: view.theme.id.to_string(),
            label: view.theme.label.to_string(),
        },
    })
}

fn branding_style_value() -> Value {
    let view = bss_branding::current(None);
    let block = bss_branding::branding_css_block(&view.theme);
    Value::from_safe_string(format!("<style>{block}</style>"))
}

/// Build the shared [`Environment`]. `asset_v` is stamped once here (process
/// start), matching the Python module-load-time stamp.
pub fn build_environment() -> Arc<Environment<'static>> {
    let local = local_template_dir();
    let shared = shared_template_dir();

    let mut env = Environment::new();
    // Two-dir loader (ChoiceLoader equivalent): local overrides shared.
    env.set_loader(move |name| {
        for dir in [&local, &shared] {
            let path = dir.join(name);
            match std::fs::read_to_string(&path) {
                Ok(src) => return Ok(Some(src)),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => {
                    return Err(minijinja::Error::new(
                        minijinja::ErrorKind::InvalidOperation,
                        format!("failed to read template {name}: {e}"),
                    ))
                }
            }
        }
        Ok(None)
    });

    let asset_v = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
        .to_string();

    env.add_global("bss_release", Value::from(BSS_RELEASE));
    env.add_global("asset_v", Value::from(asset_v));
    env.add_function("branding", branding_value);
    env.add_function("branding_style", branding_style_value);

    // v1.6 — the CRM screens' shared payload filters.
    env.add_filter("fmt_dt", |v: Value| crate::views::fmt_dt_value(&v));
    env.add_filter("tone", |v: Value| {
        crate::views::state_tone(&v.to_string()).to_string()
    });

    Arc::new(env)
}
