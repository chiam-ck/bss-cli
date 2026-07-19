//! Operator-cockpit `/settings` page (v0.13 PR8). Port of `bss_csr.routes.settings`.
//!
//! Two textareas backed by `.bss-cli/OPERATOR.md` and `.bss-cli/settings.toml`.
//! `GET /settings` shows both; the POST handlers persist + validate via
//! `bss_cockpit::write_*` and 303 back to the form. Invalid TOML / validation
//! errors re-render with a 400 and the parser's message echoed in-page.
//!
//! No auth — the single-operator-by-design contract. `actor` for any audit trail
//! is hardcoded to `OPERATOR_ACTOR`. These are the only write path to either file
//! outside the REPL's `/operator edit` and `/config edit` commands; the
//! `bss_cockpit` writers are the validation gate.

use axum::extract::{Form, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{render, render_with_status};
use crate::AppState;

/// Build the template context off the live cockpit config. `error`/`error_section`
/// are set on the re-render after a rejected save; `overrides` echoes the
/// operator's unsaved textarea input so a round-trip doesn't lose their draft.
fn settings_context(
    error: Option<&str>,
    error_section: Option<&str>,
    flash: Option<&str>,
    overrides: Option<(&str, &str)>,
) -> Value {
    let cfg = bss_cockpit::current(None);
    let (operator_md, settings_toml, model, op_path, set_path, last_loaded) = match &cfg {
        Ok(c) => (
            c.operator_md.clone(),
            std::fs::read_to_string(&c.settings_path).unwrap_or_default(),
            c.settings.llm.model.clone().filter(|m| !m.is_empty()),
            c.operator_md_path.display().to_string(),
            c.settings_path.display().to_string(),
            c.last_loaded_at.format("%Y-%m-%d %H:%M:%S").to_string(),
        ),
        Err(_) => (
            String::new(),
            String::new(),
            None,
            String::new(),
            String::new(),
            String::new(),
        ),
    };

    // Overrides echo the unsaved input back onto the right textarea.
    let (operator_md, settings_toml) = match overrides {
        Some(("operator", v)) => (v.to_string(), settings_toml),
        Some(("config", v)) => (operator_md, v.to_string()),
        _ => (operator_md, settings_toml),
    };

    json!({
        "actor": bss_cockpit::OPERATOR_ACTOR,
        "model": model.unwrap_or_else(|| "(env default)".to_string()),
        "operator_md": operator_md,
        "settings_toml": settings_toml,
        "operator_md_path": op_path,
        "settings_path": set_path,
        "last_loaded_at": last_loaded,
        "error": error,
        "error_section": error_section,
        "flash": flash,
    })
}

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
}

/// `GET /settings`.
pub async fn settings_page(State(state): State<AppState>, Query(q): Query<FlashQuery>) -> Response {
    let flash = (!q.flash.is_empty()).then_some(q.flash.as_str());
    render(
        &state,
        "settings.html",
        minijinja::Value::from_serialize(settings_context(None, None, flash, None)),
    )
}

#[derive(Deserialize)]
pub struct OperatorForm {
    operator_md: String,
}

/// `POST /settings/operator`.
pub async fn save_operator_md(
    State(state): State<AppState>,
    Form(form): Form<OperatorForm>,
) -> Response {
    match bss_cockpit::write_operator_md(&form.operator_md, None) {
        Ok(()) => Redirect::to("/settings?flash=operator_saved").into_response(),
        Err(e) => render_with_status(
            &state,
            "settings.html",
            minijinja::Value::from_serialize(settings_context(
                Some(&e.to_string()),
                Some("operator"),
                None,
                Some(("operator", &form.operator_md)),
            )),
            StatusCode::BAD_REQUEST,
        ),
    }
}

#[derive(Deserialize)]
pub struct ConfigForm {
    settings_toml: String,
}

/// `POST /settings/config`.
pub async fn save_config_toml(
    State(state): State<AppState>,
    Form(form): Form<ConfigForm>,
) -> Response {
    match bss_cockpit::write_settings_toml(&form.settings_toml, None) {
        Ok(_) => Redirect::to("/settings?flash=config_saved").into_response(),
        Err(e) => render_with_status(
            &state,
            "settings.html",
            minijinja::Value::from_serialize(settings_context(
                Some(&e.to_string()),
                Some("config"),
                None,
                Some(("config", &form.settings_toml)),
            )),
            StatusCode::BAD_REQUEST,
        ),
    }
}
