//! Behaviour tests for bss-context.
//!
//! Ports the intent of `services/*/tests/test_auth_context.py` (default identity,
//! header→context bridge, service-identity from the token layer not a header) and
//! `packages/bss-clients/tests/test_header_propagation.py` (outbound propagation +
//! uuid fallback), plus task-local isolation the Python ContextVar gave for free.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use axum::{
    body::{to_bytes, Body},
    extract::Request,
    http::StatusCode,
    middleware::{from_fn, Next},
    response::Response,
    routing::get,
    Extension, Router,
};
use bss_context::{
    current, propagate_context, scope, RequestCtx, ServiceIdentity, OUT_ACTOR, OUT_CHANNEL,
    OUT_REQUEST_ID,
};
use tower::ServiceExt;

// ─── RequestCtx defaults & helpers ──────────────────────────────────────────

#[test]
fn default_ctx_matches_python_authcontext() {
    let c = RequestCtx::default();
    assert_eq!(c.actor, "system");
    assert_eq!(c.tenant, "DEFAULT");
    assert_eq!(c.channel, "system");
    assert_eq!(c.service_identity, "default");
    assert_eq!(c.roles, vec!["admin"]);
    assert_eq!(c.permissions, vec!["*"]);
}

#[test]
fn has_permission_honours_wildcard_and_exact() {
    let c = RequestCtx::default(); // permissions = ["*"]
    assert!(c.has_permission("anything"));

    let scoped = RequestCtx {
        permissions: vec!["case.read".to_string(), "case.close".to_string()],
        ..RequestCtx::default()
    };
    assert!(scoped.has_permission("case.close"));
    assert!(!scoped.has_permission("case.delete"));
}

#[test]
fn from_headers_extracts_and_defaults() {
    use axum::http::HeaderMap;
    let mut h = HeaderMap::new();
    h.insert("x-bss-actor", "alice".parse().unwrap());
    h.insert("x-bss-channel", "cli".parse().unwrap());
    h.insert("x-request-id", "req-42".parse().unwrap());
    // token layer said this request authenticated as the customer portal:
    let c = RequestCtx::from_headers(&h, Some("portal_self_serve".to_string()));
    assert_eq!(c.actor, "alice");
    assert_eq!(c.channel, "cli");
    assert_eq!(c.request_id, "req-42");
    assert_eq!(c.tenant, "DEFAULT"); // absent header → default
    assert_eq!(c.service_identity, "portal_self_serve");
}

#[test]
fn from_headers_defaults_service_identity_when_token_layer_absent() {
    use axum::http::HeaderMap;
    // No ServiceIdentity from the token layer (perimeter bypassed, e.g. tests).
    let c = RequestCtx::from_headers(&HeaderMap::new(), None);
    assert_eq!(c.service_identity, "default");
    assert_eq!(c.actor, "system");
    // Missing x-request-id → a fresh uuid is generated.
    assert!(!c.request_id.is_empty());
}

#[test]
fn outbound_headers_propagate_and_backfill_request_id() {
    let c = RequestCtx {
        actor: "alice".to_string(),
        channel: "cli".to_string(),
        request_id: "req-42".to_string(),
        ..RequestCtx::default()
    };
    let h = c.outbound_headers();
    assert_eq!(h[0], (OUT_ACTOR, "alice".to_string()));
    assert_eq!(h[1], (OUT_CHANNEL, "cli".to_string()));
    assert_eq!(h[2], (OUT_REQUEST_ID, "req-42".to_string()));

    // Empty request id → a generated one (test_default_context_values).
    let c2 = RequestCtx::default();
    let h2 = c2.outbound_headers();
    assert_eq!(h2[0].1, "system");
    assert!(!h2[2].1.is_empty(), "request id auto-generated when empty");
}

// ─── task-local scope ───────────────────────────────────────────────────────

#[tokio::test]
async fn current_defaults_outside_scope() {
    // Mirrors the ContextVar default: reading with no active request is legal.
    assert_eq!(current(), RequestCtx::default());
}

#[tokio::test]
async fn scope_sets_and_nests() {
    let outer = RequestCtx {
        actor: "alice".to_string(),
        ..RequestCtx::default()
    };
    scope(outer.clone(), async {
        assert_eq!(current().actor, "alice");
        let inner = RequestCtx {
            actor: "bob".to_string(),
            ..RequestCtx::default()
        };
        scope(inner, async {
            assert_eq!(current().actor, "bob");
        })
        .await;
        // back to the outer frame
        assert_eq!(current().actor, "alice");
    })
    .await;
    // out of all scopes → default again
    assert_eq!(current(), RequestCtx::default());
}

#[tokio::test]
async fn concurrent_tasks_are_isolated() {
    let a = tokio::spawn(scope(
        RequestCtx {
            actor: "alice".to_string(),
            ..RequestCtx::default()
        },
        async {
            tokio::task::yield_now().await;
            current().actor
        },
    ));
    let b = tokio::spawn(scope(
        RequestCtx {
            actor: "bob".to_string(),
            ..RequestCtx::default()
        },
        async {
            tokio::task::yield_now().await;
            current().actor
        },
    ));
    assert_eq!(a.await.unwrap(), "alice");
    assert_eq!(b.await.unwrap(), "bob");
}

// ─── middleware bridge (RequestIdMiddleware port) ───────────────────────────

async fn handler(Extension(ctx): Extension<RequestCtx>) -> String {
    // Prove the extension ctx and the task-local ctx agree during the request.
    assert_eq!(current(), ctx);
    format!("{}|{}|{}", ctx.actor, ctx.service_identity, ctx.request_id)
}

/// Stand-in for the token middleware: stashes a resolved identity in extensions.
async fn insert_identity(mut req: Request, next: Next) -> Response {
    req.extensions_mut()
        .insert(ServiceIdentity("portal_self_serve".to_string()));
    next.run(req).await
}

fn app_with_identity() -> Router {
    Router::new()
        .route("/x", get(handler))
        .layer(from_fn(propagate_context)) // inner
        .layer(from_fn(insert_identity)) // outer — runs first, sets identity
}

#[tokio::test]
async fn middleware_bridges_headers_and_identity() {
    let req = Request::builder()
        .uri("/x")
        .header("x-bss-actor", "alice")
        .header("x-request-id", "req-42")
        .body(Body::empty())
        .unwrap();
    let resp = app_with_identity().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    // x-request-id echoed on the response (send_wrapper).
    assert_eq!(resp.headers().get("x-request-id").unwrap(), "req-42");
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    assert_eq!(&body[..], b"alice|portal_self_serve|req-42");
}

#[tokio::test]
async fn middleware_generates_request_id_when_absent() {
    let app = Router::new()
        .route("/x", get(handler))
        .layer(from_fn(propagate_context));
    let req = Request::builder().uri("/x").body(Body::empty()).unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    // No token layer → default identity; a request id was generated + echoed.
    let echoed = resp
        .headers()
        .get("x-request-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    assert!(!echoed.is_empty());
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let text = String::from_utf8(body.to_vec()).unwrap();
    assert!(text.starts_with("system|default|"), "got {text}");
    assert!(
        text.ends_with(&echoed),
        "body request id matches echoed header"
    );
}
