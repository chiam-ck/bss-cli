//! Route gating helpers + public-route allowlist. Port of the pure parts of
//! `bss_self_serve.security`: the allowlist, `safe_next_path` (open-redirect
//! defence), and the sensitive/signup action-label catalogues. The step-up
//! consume flow + the FastAPI gating dependencies land with the auth slice.

/// Paths reachable without a session (exact match).
pub const PUBLIC_EXACT_PATHS: &[&str] = &[
    "/health",
    "/health/ready",
    "/health/live",
    "/welcome",
    "/plans",
    "/terms",
    "/privacy",
    "/signup/step/kyc/callback",
    "/branding/logo",
];

/// Path prefixes reachable without a session.
pub const PUBLIC_PATH_PREFIXES: &[&str] = &[
    "/auth/",
    "/static/",
    "/portal-ui/static/",
    "/plans/",
    "/webhooks/",
];

/// Greppable source of truth for step-up-gated sensitive writes.
pub const SENSITIVE_ACTION_LABELS: &[&str] = &[
    "vas_purchase",
    "payment_method_add",
    "payment_method_remove",
    "payment_method_set_default",
    "subscription_terminate",
    "email_change",
    "phone_update",
    "address_update",
    "name_update",
    "plan_change_schedule",
    "plan_change_cancel",
];

/// Signup-chain audit labels (writes before a linked-customer session exists;
/// audit-only, never step-up-gated).
pub const SIGNUP_ACTION_LABELS: &[&str] = &[
    "signup_create_customer",
    "signup_attest_kyc",
    "signup_add_card",
    "signup_create_order",
];

/// True iff `path` is reachable without a session.
pub fn is_public_path(path: &str) -> bool {
    PUBLIC_EXACT_PATHS.contains(&path) || PUBLIC_PATH_PREFIXES.iter().any(|p| path.starts_with(p))
}

/// Validate a `?next=` redirect target against an internal-only allowlist
/// (open-redirect defence). Only absolute internal paths survive; anything with
/// an embedded host/scheme/CRLF/backslash falls back to `default`.
pub fn safe_next_path(raw: Option<&str>, default: &str) -> String {
    let candidate = match raw {
        Some(r) if !r.trim().is_empty() => r.trim(),
        _ => return default.to_string(),
    };
    if !candidate.starts_with('/') {
        return default.to_string();
    }
    if candidate.starts_with("//") || candidate.starts_with("/\\") {
        return default.to_string();
    }
    for token in ["://", "\r", "\n", "\\"] {
        if candidate.contains(token) {
            return default.to_string();
        }
    }
    candidate.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_paths() {
        assert!(is_public_path("/welcome"));
        assert!(is_public_path("/plans"));
        assert!(is_public_path("/auth/login"));
        assert!(is_public_path("/static/css/portal.css"));
        assert!(!is_public_path("/"));
        assert!(!is_public_path("/profile"));
    }

    #[test]
    fn safe_next() {
        assert_eq!(safe_next_path(Some("/dashboard"), "/"), "/dashboard");
        assert_eq!(safe_next_path(Some("/a?b=c"), "/"), "/a?b=c");
        assert_eq!(safe_next_path(None, "/"), "/");
        assert_eq!(safe_next_path(Some(""), "/"), "/");
        // open-redirect attempts
        assert_eq!(safe_next_path(Some("//evil.com"), "/"), "/");
        assert_eq!(safe_next_path(Some("https://evil.com"), "/"), "/");
        assert_eq!(safe_next_path(Some("/\\evil"), "/"), "/");
        assert_eq!(safe_next_path(Some("/a\r\nSet-Cookie: x"), "/"), "/");
        assert_eq!(safe_next_path(Some("relative"), "/"), "/");
    }
}
