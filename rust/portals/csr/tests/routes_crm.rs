//! v1.6.1 confirm-gate pins for the cockpit CRM screens — the Rust side of
//! `portals/csr/tests/test_routes_crm.py`.
//!
//! Destructive + money-moving verbs ARE direct CRUD (no orchestrator hop), but
//! every such POST must carry `confirm=yes` from the expanded danger panel. A bare
//! POST must bounce with an error flash and **must not execute**.
//!
//! These build the state with `clients: None` on purpose. That makes the test
//! independent of whether a perimeter token happens to be in the environment, and
//! it pins something stronger than a mock could: the gate is checked **before any
//! client is touched**, so a bare POST cannot reach a write even in principle.
//! `confirm=yes` then falls through to the client layer (503 without clients) —
//! which is exactly how we tell "the gate refused" from "the gate let it past".
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use bss_csr::{build_router, config::Settings, inflight::Inflight, templating, AppState};

fn state_without_clients() -> AppState {
    AppState {
        env: templating::build_environment(),
        settings: Arc::new(Settings::from_env()),
        clients: None,
        store: None,
        chat_registry: None,
        autonomy_mode: bss_orchestrator::AutonomyMode::Granular,
        inflight: Inflight::new(),
    }
}

async fn post(path: &str, body: &str) -> (StatusCode, String) {
    let resp = build_router(state_without_clients())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/x-www-form-urlencoded")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let location = resp
        .headers()
        .get("location")
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    (status, location)
}

/// The full `_CONFIRM_GATED` table from the oracle's `test_routes_crm.py`, all ten
/// entries across every CRM screen. Each tuple is `(path, extra_form_fields)` — the
/// confirm test appends `confirm=yes` to the extras.
///
/// Note `/catalog/PLAN_M/retire` refuses with its OWN copy ("Check the confirm
/// box…"), not the shared `CONFIRM_REQUIRED` string — the gate is what's pinned,
/// so the test asserts a gate-refusal message is present rather than one exact
/// string.
const CONFIRM_GATED: [(&str, &str); 10] = [
    ("/customers/CUST-001/close", ""),
    ("/customers/CUST-001/contact/CM-1/remove", ""),
    ("/case/CASE-042/close", "resolution_code=no_fault_found"),
    ("/case/CASE-042/ticket/TKT-101/cancel", ""),
    ("/orders/ORD-014/submit", ""),
    ("/orders/ORD-014/cancel", ""),
    ("/subscriptions/SUB-007/renew", ""),
    ("/subscriptions/SUB-007/vas", "vas_offering_id=VAS_1GB"),
    ("/subscriptions/SUB-007/terminate", ""),
    ("/catalog/PLAN_M/retire", ""),
];

#[tokio::test]
async fn destructive_posts_refuse_without_confirm() {
    for (path, data) in CONFIRM_GATED {
        let (status, location) = post(path, data).await;
        assert_eq!(status, StatusCode::SEE_OTHER, "{path}");
        assert!(location.contains("err="), "{path}: {location}");
        // The refusal is the gate's own copy, not a downstream failure. Most
        // screens share `CONFIRM_REQUIRED` ("…expanded confirm step"); the catalog
        // retire uses its own checkbox wording. Either is a gate refusal — what
        // must NOT appear is a service-error flash (that would mean it executed).
        assert!(
            location.contains("expanded+confirm+step") || location.contains("confirm+box"),
            "{path}: {location}"
        );
    }
}

#[tokio::test]
async fn destructive_posts_pass_the_gate_with_confirm() {
    for (path, data) in CONFIRM_GATED {
        let body = if data.is_empty() {
            "confirm=yes".to_string()
        } else {
            format!("{data}&confirm=yes")
        };
        let (status, location) = post(path, &body).await;
        // Past the gate → reaches the client layer, which is absent here. The
        // point is that it is NOT a 303-with-err bounce.
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{path}");
        assert!(
            location.is_empty(),
            "{path}: unexpected redirect {location}"
        );
    }
}

/// Anything other than the exact string `yes` is not authorisation.
#[tokio::test]
async fn near_miss_confirm_values_do_not_authorise() {
    for body in [
        "confirm=",
        "confirm=no",
        "confirm=YES",
        "confirm=yes+",
        "confirm=true",
    ] {
        let (status, location) = post("/customers/CUST-001/close", body).await;
        assert_eq!(status, StatusCode::SEE_OTHER, "{body}");
        assert!(location.contains("err="), "{body}: {location}");
    }
}
