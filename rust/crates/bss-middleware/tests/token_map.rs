//! TokenMap loader/validator tests — ports `test_api_token.py` + golden-vector
//! conformance against the Python oracle (`golden_vectors.json`, risk R4).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use bss_middleware::{
    hash_token, identity_from_env_var, load_token_map, validate_token_map,
    validate_token_map_present, TokenMap, TEST_TOKEN,
};
use serde_json::Value;

const PORTAL_TOKEN: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
const CSR_TOKEN: &str = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";

fn env(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

// ─── Golden-vector conformance (R4) ─────────────────────────────────────────

#[test]
fn hmac_matches_python_oracle() {
    let golden: Value = serde_json::from_str(include_str!("golden_vectors.json")).unwrap();
    let tokens = golden["tokens"].as_object().unwrap();
    let hashes = golden["hmac_sha256_hex"].as_object().unwrap();
    assert!(!tokens.is_empty());
    for (name, tok) in tokens {
        let tok = tok.as_str().unwrap();
        let want = hashes[name].as_str().unwrap();
        let got = hex(&hash_token(tok));
        assert_eq!(got, want, "HMAC mismatch for {name:?} ({tok:?})");
    }
    // The salt/sentinel/min-length constants match too.
    assert_eq!(golden["salt_utf8"], "bss-cli-token-map-v0.9-fixed-salt");
    assert_eq!(golden["sentinel"], "changeme");
    assert_eq!(golden["min_length"], 32);
    assert_eq!(golden["test_token"], TEST_TOKEN);
}

#[test]
fn identity_derivation_matches_python_oracle() {
    let golden: Value = serde_json::from_str(include_str!("golden_vectors.json")).unwrap();
    for (name, want) in golden["identity_from_env_var"].as_object().unwrap() {
        assert_eq!(
            identity_from_env_var(name).as_deref(),
            want.as_str(),
            "identity for {name:?}"
        );
    }
    for pair in golden["env_var_rejected"].as_array().unwrap() {
        let name = pair[0].as_str().unwrap();
        assert_eq!(
            identity_from_env_var(name),
            None,
            "{name:?} must be rejected"
        );
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

// ─── loader ─────────────────────────────────────────────────────────────────

#[test]
fn loader_single_default_token() {
    let m = load_token_map(&env(&[("BSS_API_TOKEN", TEST_TOKEN)]));
    assert_eq!(m.identities(), vec!["default"]);
    assert_eq!(m.lookup(TEST_TOKEN).as_deref(), Some("default"));
}

#[test]
fn loader_default_plus_portal() {
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN),
    ]));
    assert_eq!(m.identities(), vec!["default", "portal_self_serve"]);
    assert_eq!(m.lookup(TEST_TOKEN).as_deref(), Some("default"));
    assert_eq!(m.lookup(PORTAL_TOKEN).as_deref(), Some("portal_self_serve"));
}

#[test]
fn loader_named_only_no_default() {
    let m = load_token_map(&env(&[("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)]));
    assert_eq!(m.identities(), vec!["portal_self_serve"]);
    assert_eq!(m.lookup(TEST_TOKEN), None);
}

#[test]
fn loader_partner_underscore_name_ignored() {
    // BSS_PARTNER_API_TOKEN_ACME does not match ^BSS_(.+)_API_TOKEN$.
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PARTNER_API_TOKEN_ACME", PORTAL_TOKEN),
    ]));
    assert_eq!(m.identities(), vec!["default"]);
}

#[test]
fn loader_partner_canonical_name_works() {
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PARTNER_ACME_API_TOKEN", PORTAL_TOKEN),
    ]));
    assert!(m.identities().contains(&"partner_acme"));
    assert_eq!(m.lookup(PORTAL_TOKEN).as_deref(), Some("partner_acme"));
}

#[test]
fn loader_ignores_unrelated_and_empty() {
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_DB_URL", "postgres://..."),
        ("PATH", "/usr/bin"),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", ""), // empty value skipped
    ]));
    assert_eq!(m.identities(), vec!["default"]);
}

#[test]
fn loader_named_tokens_sorted_for_determinism() {
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_ZED_API_TOKEN", &"z".repeat(64)),
        ("BSS_ALPHA_API_TOKEN", &"a".repeat(64)),
    ]));
    assert_eq!(m.identities(), vec!["default", "alpha", "zed"]);
}

// ─── hashing / lookup ───────────────────────────────────────────────────────

#[test]
fn map_stores_hashes_not_raw() {
    let m = load_token_map(&env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN),
    ]));
    // Every entry is a 32-byte hash, not the raw token bytes.
    assert_eq!(m.len(), 2);
    assert_ne!(hash_token(TEST_TOKEN), hash_token(PORTAL_TOKEN));
}

#[test]
fn lookup_returns_none_for_unknown_or_empty() {
    let m = load_token_map(&env(&[("BSS_API_TOKEN", TEST_TOKEN)]));
    assert_eq!(m.lookup(CSR_TOKEN), None);
    assert_eq!(m.lookup(""), None);
    assert_eq!(m.lookup("not-a-real-token"), None);
}

#[test]
fn same_token_hashes_stably() {
    let a = load_token_map(&env(&[("BSS_API_TOKEN", TEST_TOKEN)]));
    let b = load_token_map(&env(&[("BSS_API_TOKEN", TEST_TOKEN)]));
    assert_eq!(a, b);
}

#[test]
fn single_token_compat() {
    let m = TokenMap::single(TEST_TOKEN);
    assert_eq!(m.lookup(TEST_TOKEN).as_deref(), Some("default"));
}

// ─── validation ─────────────────────────────────────────────────────────────

fn err(map: &TokenMap, e: &BTreeMap<String, String>) -> String {
    validate_token_map(map, e).unwrap_err().0
}

#[test]
fn validate_requires_default() {
    let e = env(&[("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN)]);
    assert!(err(&load_token_map(&e), &e).contains("BSS_API_TOKEN is unset"));
}

#[test]
fn validate_rejects_sentinel() {
    let e = env(&[("BSS_API_TOKEN", "changeme")]);
    assert!(err(&load_token_map(&e), &e).contains("sentinel"));

    let e2 = env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", "changeme"),
    ]);
    let msg = err(&load_token_map(&e2), &e2);
    assert!(msg.contains("BSS_PORTAL_SELF_SERVE_API_TOKEN") && msg.contains("sentinel"));
}

#[test]
fn validate_length_boundary() {
    let short = env(&[("BSS_API_TOKEN", &"a".repeat(31))]);
    assert!(err(&load_token_map(&short), &short).contains("too short"));

    let ok = env(&[("BSS_API_TOKEN", &"a".repeat(32))]);
    assert!(validate_token_map(&load_token_map(&ok), &ok).is_ok());

    let short_named = env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", &"a".repeat(31)),
    ]);
    let msg = err(&load_token_map(&short_named), &short_named);
    assert!(msg.contains("BSS_PORTAL_SELF_SERVE_API_TOKEN") && msg.contains("too short"));
}

#[test]
fn validate_rejects_shared_token() {
    let e = env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", TEST_TOKEN), // same value → reject
    ]);
    assert!(err(&load_token_map(&e), &e).contains("sharing a token"));
}

#[test]
fn validate_error_never_echoes_raw_token() {
    let e = env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", &"a".repeat(31)),
    ]);
    let msg = err(&load_token_map(&e), &e);
    assert!(!msg.contains(&"a".repeat(31)));
    assert!(!msg.contains(TEST_TOKEN));
}

#[test]
fn validate_present_combined() {
    let e = env(&[
        ("BSS_API_TOKEN", TEST_TOKEN),
        ("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_TOKEN),
    ]);
    let m = validate_token_map_present(&e).unwrap();
    assert_eq!(m.lookup(TEST_TOKEN).as_deref(), Some("default"));
    assert_eq!(m.lookup(PORTAL_TOKEN).as_deref(), Some("portal_self_serve"));

    assert!(validate_token_map_present(&env(&[])).is_err());
}
