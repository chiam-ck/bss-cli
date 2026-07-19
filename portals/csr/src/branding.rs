//! Operator-cockpit `/settings/branding` page (v1.8). Port of
//! `bss_csr.routes.branding`.
//!
//! The visual front door for operator branding: brand name, theme picker (palette
//! swatches), logo mark (built-in glyphs or a 1-3 char custom mark), logo image
//! upload, and an HTMX live preview that writes nothing until Save.
//!
//! Doctrine — these are NON-destructive config writes (the same class as the
//! `/settings` POSTs, not `DESTRUCTIVE_TOOLS`/money verbs), so there is
//! deliberately no two-step confirm panel. One POST → one `bss_cockpit` writer; the
//! writers are the validation gate (toml_edit round-trip + whole-document
//! re-validate), so this module never touches settings.toml or the logo file
//! directly. Upload security: the cap is enforced by BYTES READ (Content-Length is
//! browser-asserted fiction), the type by magic bytes inside `write_branding_logo`
//! (PNG/JPEG/WebP — never SVG), the destination filename fixed.

use axum::extract::{Multipart, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_branding::{DEFAULT_BRAND_NAME, DEFAULT_THEME_ID, LOGO_MARKS, MAX_LOGO_BYTES, THEMES};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{render, render_with_status};
use crate::AppState;

/// `mark_choice=="custom"` → the trimmed custom text; otherwise the chosen glyph.
fn resolve_mark(mark_choice: &str, mark_custom: &str) -> String {
    if mark_choice == "custom" {
        mark_custom.trim().to_string()
    } else {
        mark_choice.to_string()
    }
}

/// The palette for `theme`, or the default when unknown/blank. Serialized for the
/// template's `t` (swatches + preview).
fn theme_or_default(theme: &str) -> Value {
    let t = THEMES
        .get(theme)
        .unwrap_or_else(|| &THEMES[DEFAULT_THEME_ID]);
    serde_json::to_value(t).unwrap_or_else(|_| json!({}))
}

/// Build the template context. Form values come from the FILE (never the
/// env-overridden view) so an env override can't get silently baked in on the next
/// save. `overrides` echoes rejected input back onto the form.
fn context(
    flash: Option<&str>,
    error: Option<&str>,
    error_section: Option<&str>,
    overrides: Option<(&str, &str, &str)>,
) -> Value {
    let saved = bss_branding::file_settings(None);
    let (brand_name, theme, mark) = match overrides {
        Some((b, t, m)) => (b.to_string(), t.to_string(), m.to_string()),
        None => (saved.brand_name, saved.theme, saved.mark),
    };
    let mark_is_custom = !LOGO_MARKS.contains(&mark.as_str());
    let view = bss_branding::current(None);

    json!({
        "active_page": "branding",
        "themes": THEMES.values().map(|t| serde_json::to_value(t).unwrap_or_default()).collect::<Vec<_>>(),
        "logo_marks": LOGO_MARKS,
        "values": { "brand_name": brand_name, "theme": theme, "mark": mark },
        "mark_is_custom": mark_is_custom,
        "logo_view": { "logo_version": view.logo_version },
        "max_logo_kb": MAX_LOGO_BYTES / 1024,
        // The initial (non-HTMX) render of the preview partial.
        "t": theme_or_default(&theme),
        "name": brand_name,
        "mark": mark,
        "flash": flash,
        "error": error,
        "error_section": error_section,
    })
}

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
}

/// `GET /settings/branding`.
pub async fn branding_page(State(state): State<AppState>, Query(q): Query<FlashQuery>) -> Response {
    let flash = (!q.flash.is_empty()).then_some(q.flash.as_str());
    render(
        &state,
        "branding.html",
        minijinja::Value::from_serialize(context(flash, None, None, None)),
    )
}

#[derive(Deserialize)]
pub struct BrandingForm {
    brand_name: String,
    theme: String,
    mark_choice: String,
    #[serde(default)]
    mark_custom: String,
}

/// `POST /settings/branding`. The logo is preserved (it has its own forms), so the
/// save carries the file's current `logo_image` through unchanged.
pub async fn branding_save(
    State(state): State<AppState>,
    axum::extract::Form(form): axum::extract::Form<BrandingForm>,
) -> Response {
    let mark = resolve_mark(&form.mark_choice, &form.mark_custom);
    let saved = bss_branding::file_settings(None);
    let validated = bss_branding::BrandingSettings::validate(
        &form.brand_name,
        &form.theme,
        &mark,
        &saved.logo_image,
    );
    match validated
        .and_then(|u| bss_cockpit::write_branding_settings(&u, None).map_err(|e| e.to_string()))
    {
        Ok(()) => Redirect::to("/settings/branding?flash=branding_saved").into_response(),
        Err(msg) => render_with_status(
            &state,
            "branding.html",
            minijinja::Value::from_serialize(context(
                None,
                Some(&msg),
                Some("branding"),
                Some((&form.brand_name, &form.theme, &mark)),
            )),
            StatusCode::BAD_REQUEST,
        ),
    }
}

/// `POST /settings/branding/logo` (multipart). Reads one byte past the cap so an
/// oversize file is caught regardless of what Content-Length claimed.
pub async fn branding_logo_upload(
    State(state): State<AppState>,
    mut multipart: Multipart,
) -> Response {
    let mut data: Vec<u8> = Vec::new();
    while let Ok(Some(field)) = multipart.next_field().await {
        if field.name() == Some("logo") {
            match field.bytes().await {
                Ok(b) => data = b.to_vec(),
                Err(e) => {
                    tracing::warn!(error = %e, "csr.branding.logo_read_failed");
                    return render_with_status(
                        &state,
                        "branding.html",
                        minijinja::Value::from_serialize(context(
                            None,
                            Some("could not read the uploaded file"),
                            Some("logo"),
                            None,
                        )),
                        StatusCode::BAD_REQUEST,
                    );
                }
            }
            break;
        }
    }

    // `write_branding_logo` enforces the byte cap AND magic-byte type — reading the
    // whole field then handing it over keeps the size check authoritative on our
    // side (the cap is `data.len()`, not a header).
    match bss_cockpit::write_branding_logo(&data, None) {
        Ok(_) => Redirect::to("/settings/branding?flash=logo_saved").into_response(),
        Err(e) => render_with_status(
            &state,
            "branding.html",
            minijinja::Value::from_serialize(context(
                None,
                Some(&e.to_string()),
                Some("logo"),
                None,
            )),
            StatusCode::BAD_REQUEST,
        ),
    }
}

/// `POST /settings/branding/logo/delete`.
pub async fn branding_logo_delete(State(_state): State<AppState>) -> Response {
    if let Err(e) = bss_cockpit::remove_branding_logo(None) {
        tracing::warn!(error = %e, "csr.branding.logo_delete_failed");
    }
    Redirect::to("/settings/branding?flash=logo_removed").into_response()
}

#[derive(Deserialize, Default)]
pub struct PreviewQuery {
    #[serde(default)]
    theme: String,
    #[serde(default)]
    brand_name: String,
    #[serde(default = "dollar")]
    mark_choice: String,
    #[serde(default)]
    mark_custom: String,
}

fn dollar() -> String {
    "$".to_string()
}

/// `GET /settings/branding/preview` — the HTMX fragment. Renders the preview card
/// from form state WITHOUT writing. Blank/unknown values degrade to defaults so
/// half-typed input never 4xxes the fragment.
pub async fn branding_preview(
    State(state): State<AppState>,
    Query(q): Query<PreviewQuery>,
) -> Response {
    let mark = {
        let m = resolve_mark(&q.mark_choice, &q.mark_custom);
        let m = if m.is_empty() { "$".to_string() } else { m };
        // `mark[:3]` counts characters, not bytes.
        m.chars().take(3).collect::<String>()
    };
    let name = {
        let n = q.brand_name.trim();
        if n.is_empty() {
            DEFAULT_BRAND_NAME.to_string()
        } else {
            n.to_string()
        }
    };
    render(
        &state,
        "partials/branding_preview.html",
        minijinja::Value::from_serialize(json!({
            "t": theme_or_default(&q.theme),
            "name": name,
            "mark": mark,
        })),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_mark_honours_custom() {
        assert_eq!(resolve_mark("custom", "  AB "), "AB");
        assert_eq!(resolve_mark("$", "ignored"), "$");
    }

    #[test]
    fn theme_or_default_falls_back() {
        // A known theme serializes with its id; an unknown one degrades to default.
        let known = theme_or_default("phosphor");
        assert_eq!(known["id"], "phosphor");
        let unknown = theme_or_default("no-such-theme");
        assert_eq!(unknown["id"], DEFAULT_THEME_ID);
    }
}
