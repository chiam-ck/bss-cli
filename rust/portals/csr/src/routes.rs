//! Shared route helpers + the health endpoint.

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Redirect, Response};
use bss_clients::ClientError;
use minijinja::Value;

use crate::AppState;

/// The message a destructive POST earns when it arrives without the expanded
/// danger panel's `confirm=yes` (v1.6.1). Pinned by the confirm-gate tests.
pub const CONFIRM_REQUIRED: &str = "This action needs the expanded confirm step.";

/// Python's `urllib.parse.urlencode`, which quotes with **`quote_plus`**: space
/// becomes `+` (not `%20`), and only `[A-Za-z0-9_.-~]` pass through — note `/` is
/// escaped, unlike the self-serve portal's `next=` encoder.
pub fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'~') {
            out.push(b as char);
        } else if b == b' ' {
            out.push('+');
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// 303 back to `base`, carrying an optional flash/err. Empty values are dropped,
/// so a bare redirect stays a bare URL. Backs the `_back_to_customer` /
/// `_back_to_case` family.
///
/// 303 specifically — POST/redirect/GET, so a refresh doesn't re-submit the write.
pub fn back_to(base: &str, flash: &str, err: &str) -> Response {
    let mut params: Vec<String> = Vec::new();
    if !flash.is_empty() {
        params.push(format!("flash={}", urlencode(flash)));
    }
    if !err.is_empty() {
        params.push(format!("err={}", urlencode(err)));
    }
    let mut url = base.to_string();
    if !params.is_empty() {
        url.push('?');
        url.push_str(&params.join("&"));
    }
    Redirect::to(&url).into_response()
}

/// Run one write's result; flash the outcome back to `base`.
///
/// This is the whole error contract of the CRM screens: a policy violation shows
/// the operator the rule's own message (that text is written for a human, and is
/// operator-facing copy already), and any other client error degrades to a status
/// code — never a stack trace, never a raw downstream body.
pub fn write_result(
    base: &str,
    action: &str,
    r: Result<serde_json::Value, ClientError>,
) -> Response {
    match r {
        Ok(_) => back_to(base, action, ""),
        Err(ClientError::Policy(p)) => back_to(base, "", &p.message),
        Err(e) => back_to(base, "", &format!("CRM error ({})", e.status_code())),
    }
}

/// Render `template` with `ctx`, or a 500 when the template errors. Mirrors the
/// self-serve portal's helper.
pub fn render(state: &AppState, template: &str, ctx: Value) -> Response {
    render_with_status(state, template, ctx, StatusCode::OK)
}

/// [`render`] with an explicit status — the settings/branding error pages
/// re-render the form with a 400 and the operator's unsaved input echoed back.
pub fn render_with_status(
    state: &AppState,
    template: &str,
    ctx: Value,
    status: StatusCode,
) -> Response {
    match state.env.get_template(template) {
        Ok(tpl) => match tpl.render(ctx) {
            Ok(html) => (status, Html(html)).into_response(),
            Err(e) => {
                tracing::error!(template = %template, error = %e, "cockpit.template.render_failed");
                (StatusCode::INTERNAL_SERVER_ERROR, "template render failed").into_response()
            }
        },
        Err(e) => {
            tracing::error!(template = %template, error = %e, "cockpit.template.missing");
            (StatusCode::INTERNAL_SERVER_ERROR, "template not found").into_response()
        }
    }
}

/// `GET /health`.
pub async fn health(State(state): State<AppState>) -> Response {
    axum::Json(serde_json::json!({
        "status": "ok",
        "service": state.settings.service_name,
        "version": state.settings.version,
    }))
    .into_response()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn loc(r: Response) -> String {
        r.headers()
            .get("location")
            .unwrap()
            .to_str()
            .unwrap()
            .to_string()
    }

    /// Captured from the live oracle (`urllib.parse.quote_plus`) — the encoder is
    /// the only thing standing between an operator-facing policy message and a
    /// mangled query string.
    #[test]
    fn urlencode_matches_python_quote_plus() {
        assert_eq!(urlencode("a~b"), "a~b");
        assert_eq!(urlencode("a/b"), "a%2Fb");
        assert_eq!(urlencode("a*b"), "a%2Ab");
        assert_eq!(urlencode("a'b"), "a%27b");
        // Non-ASCII is percent-encoded per UTF-8 byte.
        assert_eq!(urlencode("café"), "caf%C3%A9");
        assert_eq!(urlencode("CRM error (503)"), "CRM+error+%28503%29");
        assert_eq!(
            urlencode("This action needs the expanded confirm step."),
            "This+action+needs+the+expanded+confirm+step."
        );
    }

    #[test]
    fn back_to_drops_empty_params_and_orders_flash_before_err() {
        assert_eq!(
            loc(back_to("/customers/CUST-1", "", "")),
            "/customers/CUST-1"
        );
        assert_eq!(
            loc(back_to("/customers/CUST-1", "name_updated", "")),
            "/customers/CUST-1?flash=name_updated"
        );
        // Python's dict-insertion order: flash first, then err.
        assert_eq!(
            loc(back_to("/case/CASE-1", "x", "y")),
            "/case/CASE-1?flash=x&err=y"
        );
    }

    #[test]
    fn write_result_maps_policy_and_other_errors() {
        let policy = ClientError::Policy(bss_db::PolicyViolation {
            rule: "case.close.requires_all_tickets_resolved".to_string(),
            message: "Resolve them first.".to_string(),
            context: serde_json::json!({}),
        });
        assert_eq!(
            loc(write_result("/case/CASE-1", "closed", Err(policy))),
            "/case/CASE-1?err=Resolve+them+first."
        );
        let server = ClientError::Server {
            status: 503,
            detail: "upstream down".to_string(),
        };
        assert_eq!(
            loc(write_result("/case/CASE-1", "closed", Err(server))),
            "/case/CASE-1?err=CRM+error+%28503%29"
        );
        assert_eq!(
            loc(write_result(
                "/case/CASE-1",
                "closed",
                Ok(serde_json::json!({}))
            )),
            "/case/CASE-1?flash=closed"
        );
    }
}
