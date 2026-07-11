//! Behaviour tests for the process-local scenario clock.
//!
//! Faithful port of `packages/bss-clock/tests/test_clock.py`. The clock is a
//! process global, so every test acquires `SERIAL` (which also resets state) to
//! serialise what pytest ran sequentially by default.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::await_holding_lock)]

use std::sync::{Mutex, MutexGuard};

use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    Router,
};
use bss_clock::{
    advance, clock_admin_router, freeze, now, parse_duration, reset_for_tests, state, ClockError,
    Mode,
};
use chrono::{Duration, TimeZone, Utc};
use serde_json::{json, Value};
use tower::ServiceExt;

static SERIAL: Mutex<()> = Mutex::new(());

/// Acquire the global serialisation lock and reset the clock — the equivalent
/// of pytest's autouse `_fresh_clock` fixture.
fn serial() -> MutexGuard<'static, ()> {
    let g = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    reset_for_tests();
    g
}

// ─── Clock behaviour ────────────────────────────────────────────────────────

#[test]
fn now_returns_wall_clock_utc_by_default() {
    let _g = serial();
    let before = Utc::now();
    let t = now();
    let after = Utc::now();
    assert!(before <= t && t <= after);
}

#[test]
fn freeze_pins_now_to_provided_instant() {
    let _g = serial();
    let target = Utc.with_ymd_and_hms(2026, 6, 1, 12, 0, 0).unwrap();
    freeze(Some(target));
    assert_eq!(now(), target);
    // Calling twice with different instants shifts to the new one.
    freeze(Some(target + Duration::hours(1)));
    assert_eq!(now(), target + Duration::hours(1));
}

#[test]
fn freeze_without_arg_pins_to_current_wall_now() {
    let _g = serial();
    let frozen = freeze(None);
    // Any subsequent now() returns the same instant — no ticking.
    assert_eq!(now(), frozen);
    assert_eq!(now(), frozen);
}

#[test]
fn advance_on_frozen_clock_shifts_frozen_instant() {
    let _g = serial();
    freeze(Some(Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap()));
    advance(Duration::days(30)).unwrap();
    assert_eq!(now(), Utc.with_ymd_and_hms(2026, 1, 31, 0, 0, 0).unwrap());
}

#[test]
fn advance_on_unfrozen_clock_adds_offset_and_keeps_ticking() {
    let _g = serial();
    let t0 = now();
    advance(parse_duration("1h").unwrap()).unwrap();
    let t1 = now();
    // Roughly 1 hour ahead of t0 (give or take a few ms).
    assert!(t1 - t0 > Duration::minutes(59));
    assert!(t1 - t0 < Duration::minutes(61));
}

#[test]
fn unfreeze_drops_back_into_wall_clock() {
    let _g = serial();
    freeze(Some(Utc.with_ymd_and_hms(2026, 6, 1, 0, 0, 0).unwrap()));
    bss_clock::unfreeze();
    let t = now();
    // Close to real wall clock, not the frozen value.
    assert!((t - Utc::now()).num_seconds().abs() < 2);
}

#[test]
fn advance_rejects_negative_duration() {
    let _g = serial();
    assert_eq!(
        advance(Duration::seconds(-1)),
        Err(ClockError::NegativeDuration)
    );
}

#[test]
fn parse_duration_handles_common_forms() {
    let cases = [
        ("45s", Duration::seconds(45)),
        ("15m", Duration::minutes(15)),
        ("2h", Duration::hours(2)),
        ("30d", Duration::days(30)),
        ("  30d  ", Duration::days(30)),
    ];
    for (text, expected) in cases {
        assert_eq!(parse_duration(text).unwrap(), expected, "input {text:?}");
    }
}

#[test]
fn parse_duration_rejects_invalid() {
    for bad in ["", "30", "30x", "1w", "1h30m", "1.5h"] {
        assert!(parse_duration(bad).is_err(), "should reject {bad:?}");
    }
}

#[test]
fn state_snapshot_reflects_freeze_and_offset() {
    let _g = serial();
    advance(parse_duration("10s").unwrap()).unwrap();
    let s = state();
    assert_eq!(s.mode, Mode::Wall);
    assert_eq!(s.offset_seconds, 10.0);
    assert_eq!(s.frozen_at, None);

    freeze(Some(Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap()));
    let s2 = state();
    assert_eq!(s2.mode, Mode::Frozen);
    assert_eq!(
        s2.frozen_at,
        Some(Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap())
    );
}

// ─── Admin router ───────────────────────────────────────────────────────────

fn app() -> Router {
    Router::new().nest("/admin-api/v1", clock_admin_router())
}

async fn call(method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
    let builder = Request::builder().method(method).uri(uri);
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&v).unwrap()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = app().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let json = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, json)
}

#[tokio::test]
async fn get_clock_now_is_unguarded() {
    let _g = serial();
    std::env::remove_var("BSS_ALLOW_ADMIN_RESET");
    let (status, body) = call("GET", "/admin-api/v1/clock/now", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["mode"], "wall");
}

#[tokio::test]
async fn mutating_endpoints_403_without_flag() {
    let _g = serial();
    std::env::remove_var("BSS_ALLOW_ADMIN_RESET");
    for (path, body) in [
        ("/admin-api/v1/clock/freeze", json!({})),
        ("/admin-api/v1/clock/unfreeze", json!({})),
        ("/admin-api/v1/clock/advance", json!({"duration": "1h"})),
    ] {
        let (status, _) = call("POST", path, Some(body)).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "path {path}");
    }
}

#[tokio::test]
async fn freeze_then_advance_via_http() {
    let _g = serial();
    std::env::set_var("BSS_ALLOW_ADMIN_RESET", "true");
    let (status, body) = call(
        "POST",
        "/admin-api/v1/clock/freeze",
        Some(json!({"at": "2026-06-01T12:00:00+00:00"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["mode"], "frozen");
    assert_eq!(body["frozenAt"], "2026-06-01T12:00:00+00:00");

    let (status, body) = call(
        "POST",
        "/admin-api/v1/clock/advance",
        Some(json!({"duration": "2h"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["frozenAt"], "2026-06-01T14:00:00+00:00");
}

#[tokio::test]
async fn advance_rejects_missing_or_bad_duration() {
    let _g = serial();
    std::env::set_var("BSS_ALLOW_ADMIN_RESET", "true");
    let (status, _) = call("POST", "/admin-api/v1/clock/advance", Some(json!({}))).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    let (status, _) = call(
        "POST",
        "/admin-api/v1/clock/advance",
        Some(json!({"duration": "bogus"})),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn freeze_rejects_bad_iso() {
    let _g = serial();
    std::env::set_var("BSS_ALLOW_ADMIN_RESET", "true");
    let (status, _) = call(
        "POST",
        "/admin-api/v1/clock/freeze",
        Some(json!({"at": "not-a-date"})),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}
