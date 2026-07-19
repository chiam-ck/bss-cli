//! MiniJinja environment + branding globals. Port of `bss_self_serve.templating`.
//!
//! The Rust portal **reuses the existing Jinja templates** (they are
//! Jinja-compatible and MiniJinja renders them unchanged) via a two-directory
//! loader mirroring the Python `ChoiceLoader`: the portal's own `templates/`
//! first, then `bss_portal_ui`'s shared `templates/`. This keeps a single source
//! of template truth during the bilingual migration period.
//!
//! Branding globals (`branding()` / `branding_style()`) are **functions** —
//! evaluated per render — so a `settings.toml` theme change hot-reloads on the
//! next request (a static value would freeze the brand at process start). `bss_release`
//! is the footer footnote; `asset_v` is the process-start cache-buster.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use minijinja::{context, Environment, Value};
use serde::Serialize;

use bss_models::BSS_RELEASE;

/// `<repo>/portals/self-serve` → repo root (`../..`).
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

fn local_template_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_TEMPLATE_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("portals/self-serve/assets/templates"),
    }
}

fn shared_template_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_SHARED_TEMPLATE_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("crates/bss-portal-ui/assets/templates"),
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

    Arc::new(env)
}

/// A `request`-shaped context object for `base.html` (`request.state.identity`,
/// `request.url.path`). `identity` is `None` for anonymous/public pages.
pub fn request_ctx(path: &str, identity_email: Option<&str>) -> Value {
    let identity = identity_email.map(|email| context! { email => email });
    context! {
        state => context! { identity => identity },
        url => context! { path => path },
    }
}
