//! Lifespan pepper validator — presence + sentinel + length. Port of
//! `packages/bss-portal-auth/tests/test_startup.py`.
//!
//! `validate_pepper_present` reads process env, so the cases run in one
//! sequential test (parallel env mutation would race).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_portal_auth::validate_pepper_present;

#[test]
fn pepper_validation() {
    // real value passes
    std::env::set_var("BSS_PORTAL_TOKEN_PEPPER", "a".repeat(64));
    assert!(validate_pepper_present().is_ok());

    // unset raises "unset"
    std::env::set_var("BSS_PORTAL_TOKEN_PEPPER", "");
    let err = validate_pepper_present().unwrap_err();
    assert!(err.contains("unset"), "{err}");

    // sentinel raises "sentinel"
    std::env::set_var("BSS_PORTAL_TOKEN_PEPPER", "changeme");
    let err = validate_pepper_present().unwrap_err();
    assert!(err.contains("sentinel"), "{err}");

    // too short raises "too short"
    std::env::set_var("BSS_PORTAL_TOKEN_PEPPER", "a".repeat(16));
    let err = validate_pepper_present().unwrap_err();
    assert!(err.contains("too short"), "{err}");

    std::env::remove_var("BSS_PORTAL_TOKEN_PEPPER");
}
