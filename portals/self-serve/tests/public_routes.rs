//! Slice-1 public surface: health JSON + the branding-rendered marketing/legal
//! pages actually render through the reused Jinja templates + MiniJinja.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use tower::ServiceExt;

use bss_self_serve::{build_router, build_state};

async fn get(path: &str) -> (StatusCode, String) {
    bss_branding::reset_cache();
    let router = build_router(build_state());
    let resp = router
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

#[tokio::test]
async fn health_returns_ok_json() {
    let (status, body) = get("/health").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("\"status\":\"ok\""), "{body}");
    assert!(body.contains("portal-self-serve"), "{body}");
}

#[tokio::test]
async fn welcome_renders_with_branding_and_base_layout() {
    let (status, body) = get("/welcome").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("<!DOCTYPE html>"), "no doctype: {body}");

    // BRAND-AWARE assertion (the P6 branding-hero lesson): the page renders the
    // *operator-configured* brand name from settings.toml, not a hardcoded
    // "bss-cli". Pinning the literal string is exactly the stale-assertion bug
    // that fails identically on Python + Rust once a custom brand is set. Assert
    // the structural parts + whatever brand `bss_branding::current` resolves to.
    bss_branding::reset_cache();
    let brand = bss_branding::current(None).brand_name;
    assert!(
        body.contains(&format!("{brand} self-serve")),
        "expected '{brand} self-serve' in page"
    );
    assert!(body.contains("Sign in"), "sign-in CTA missing");
    assert!(body.contains("Browse plans"), "browse-plans CTA missing");
    // branding_style() injected the active theme's :root palette block.
    assert!(
        body.contains(":root{--bg:#"),
        "branding style block missing"
    );
    // Footer product attribution — deliberately NOT rebranded (stays "bss-cli").
    // Version comes from BSS_RELEASE (doctrine: no hardcoded version literal).
    assert!(
        body.contains(&format!("bss-cli v{}", bss_models::BSS_RELEASE)),
        "version footnote missing"
    );
}

#[tokio::test]
async fn legal_pages_render() {
    let (status, body) = get("/terms").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("<!DOCTYPE html>"), "{body}");

    let (status, _) = get("/privacy").await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn branding_logo_404_when_unconfigured() {
    let router = build_router(build_state());
    let resp = router
        .oneshot(
            Request::builder()
                .uri("/branding/logo")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    // No operator logo configured in the workspace default → 404.
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}
