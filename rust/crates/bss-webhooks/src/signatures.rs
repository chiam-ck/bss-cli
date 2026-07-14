//! HMAC signature verification for inbound provider webhooks. Port of
//! `bss_webhooks.signatures`.
//!
//! Three schemes, all HMAC-SHA-256, differing in header format + canonical
//! signed payload:
//! * `svix` (Resend) — headers `svix-id`/`svix-timestamp`/`svix-signature`;
//!   signed `"{id}.{timestamp}.{body}"`; secret is `whsec_<base64>`; the header
//!   carries space-separated `v1,<base64>` entries (rotation), any match wins.
//! * `stripe` — header `Stripe-Signature`, comma fields `t=<ts>` + `v1=<hex>`;
//!   signed `"{timestamp}.{body}"`.
//! * `didit_hmac` — header `X-Signature-V2` (or `X-Signature`) = `<hex>` over
//!   the **body alone**; `X-Timestamp` checked separately for freshness.
//!
//! All validate timestamp freshness against `max_skew_seconds` (default 300) and
//! compare timing-safe. Any failure → [`WebhookSignatureError`] with a stable
//! `code` for ops triage.

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

const DEFAULT_MAX_SKEW_SECONDS: i64 = 300;

/// The three documented signature schemes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignatureScheme {
    Svix,
    Stripe,
    DiditHmac,
}

/// Raised on any signature-verification failure. `code` is one of
/// `missing_header` | `malformed_header` | `replay_window` |
/// `signature_mismatch`. `Display` renders `"{code}: {message}"` (matching the
/// Python `ValueError` string).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WebhookSignatureError {
    pub code: String,
    pub message: String,
}

impl WebhookSignatureError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for WebhookSignatureError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for WebhookSignatureError {}

type Result0 = Result<(), WebhookSignatureError>;

/// Verify the signature on an inbound webhook request. `secret` is the
/// provider-shared signing secret (svix accepts the `whsec_<base64>` form).
/// `body` is the **raw** request bytes (re-serializing breaks the HMAC).
/// `headers` are looked up case-insensitively. `now` is a test seam for the
/// current unix timestamp (seconds).
pub fn verify_signature(
    secret: &str,
    body: &[u8],
    headers: &HashMap<String, String>,
    scheme: SignatureScheme,
    max_skew_seconds: i64,
    now: Option<f64>,
) -> Result0 {
    let headers_lower: HashMap<String, String> = headers
        .iter()
        .map(|(k, v)| (k.to_lowercase(), v.clone()))
        .collect();
    match scheme {
        SignatureScheme::Svix => verify_svix(secret, body, &headers_lower, max_skew_seconds, now),
        SignatureScheme::Stripe => {
            verify_stripe(secret, body, &headers_lower, max_skew_seconds, now)
        }
        SignatureScheme::DiditHmac => {
            verify_didit_hmac(secret, body, &headers_lower, max_skew_seconds, now)
        }
    }
}

/// Convenience: verify with the default 300s skew and wall-clock `now`.
pub fn verify_signature_default(
    secret: &str,
    body: &[u8],
    headers: &HashMap<String, String>,
    scheme: SignatureScheme,
) -> Result0 {
    verify_signature(
        secret,
        body,
        headers,
        scheme,
        DEFAULT_MAX_SKEW_SECONDS,
        None,
    )
}

fn hmac_sha256(key: &[u8], msg: &[u8]) -> Vec<u8> {
    #[allow(clippy::expect_used)]
    let mut mac = <Hmac<Sha256>>::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(msg);
    mac.finalize().into_bytes().to_vec()
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

fn ct_eq_str(a: &str, b: &str) -> bool {
    a.as_bytes().ct_eq(b.as_bytes()).into()
}

// ── svix (Resend) ────────────────────────────────────────────────────────────

fn verify_svix(
    secret: &str,
    body: &[u8],
    headers: &HashMap<String, String>,
    max_skew_seconds: i64,
    now: Option<f64>,
) -> Result0 {
    let msg_id = headers.get("svix-id").filter(|s| !s.is_empty());
    let timestamp = headers.get("svix-timestamp").filter(|s| !s.is_empty());
    let signature_header = headers.get("svix-signature").filter(|s| !s.is_empty());

    let (msg_id, timestamp, signature_header) = match (msg_id, timestamp, signature_header) {
        (Some(a), Some(b), Some(c)) => (a, b, c),
        _ => {
            return Err(WebhookSignatureError::new(
                "missing_header",
                "svix-id, svix-timestamp, and svix-signature headers required",
            ))
        }
    };

    check_timestamp(timestamp, max_skew_seconds, now)?;

    let key = decode_svix_secret(secret)?;
    let mut signed = format!("{msg_id}.{timestamp}.").into_bytes();
    signed.extend_from_slice(body);
    let expected_sig = hmac_sha256(&key, &signed);
    let expected_b64 = base64::engine::general_purpose::STANDARD.encode(expected_sig);

    // Any matching `v1,<base64>` entry validates; iterate all (timing-uniform).
    let mut matched = false;
    for entry in signature_header.split_whitespace() {
        if let Some(candidate) = entry.strip_prefix("v1,") {
            matched |= ct_eq_str(candidate, &expected_b64);
        }
    }
    if !matched {
        return Err(WebhookSignatureError::new(
            "signature_mismatch",
            "no v1 signature entry matched",
        ));
    }
    Ok(())
}

/// Accept `whsec_<base64>` and return raw key bytes; else the UTF-8 bytes.
fn decode_svix_secret(secret: &str) -> Result<Vec<u8>, WebhookSignatureError> {
    if let Some(b64) = secret.strip_prefix("whsec_") {
        base64::engine::general_purpose::STANDARD
            .decode(b64)
            .map_err(|e| {
                WebhookSignatureError::new(
                    "malformed_header",
                    format!("svix secret after 'whsec_' is not valid base64: {e}"),
                )
            })
    } else {
        Ok(secret.as_bytes().to_vec())
    }
}

// ── stripe ───────────────────────────────────────────────────────────────────

fn verify_stripe(
    secret: &str,
    body: &[u8],
    headers: &HashMap<String, String>,
    max_skew_seconds: i64,
    now: Option<f64>,
) -> Result0 {
    let sig_header = headers
        .get("stripe-signature")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            WebhookSignatureError::new("missing_header", "Stripe-Signature header required")
        })?;

    let mut timestamp: Option<String> = None;
    let mut candidates: Vec<String> = Vec::new();
    for part in sig_header.split(',') {
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        let (k, v) = (k.trim(), v.trim());
        if k == "t" {
            timestamp = Some(v.to_string());
        } else if k == "v1" {
            candidates.push(v.to_string());
        }
    }

    let timestamp = timestamp.ok_or_else(|| {
        WebhookSignatureError::new("malformed_header", "Stripe-Signature missing 't=' field")
    })?;
    if candidates.is_empty() {
        return Err(WebhookSignatureError::new(
            "malformed_header",
            "Stripe-Signature missing v1 entries",
        ));
    }

    check_timestamp(&timestamp, max_skew_seconds, now)?;

    let mut signed = format!("{timestamp}.").into_bytes();
    signed.extend_from_slice(body);
    let expected_hex = hex_lower(&hmac_sha256(secret.as_bytes(), &signed));

    let mut matched = false;
    for cand in &candidates {
        matched |= ct_eq_str(cand, &expected_hex);
    }
    if !matched {
        return Err(WebhookSignatureError::new(
            "signature_mismatch",
            "no v1 signature matched",
        ));
    }
    Ok(())
}

// ── didit_hmac ───────────────────────────────────────────────────────────────

fn verify_didit_hmac(
    secret: &str,
    body: &[u8],
    headers: &HashMap<String, String>,
    max_skew_seconds: i64,
    now: Option<f64>,
) -> Result0 {
    let sig_hex = headers
        .get("x-signature-v2")
        .or_else(|| headers.get("x-signature"))
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            WebhookSignatureError::new(
                "missing_header",
                "X-Signature-V2 (or X-Signature) header required",
            )
        })?;

    let timestamp = headers
        .get("x-timestamp")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            WebhookSignatureError::new("missing_header", "X-Timestamp header required")
        })?;

    if !sig_hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(WebhookSignatureError::new(
            "malformed_header",
            "X-Signature-V2 must be hex",
        ));
    }

    check_timestamp(timestamp, max_skew_seconds, now)?;

    let expected_hex = hex_lower(&hmac_sha256(secret.as_bytes(), body));
    if !ct_eq_str(&sig_hex.to_lowercase(), &expected_hex) {
        return Err(WebhookSignatureError::new(
            "signature_mismatch",
            "didit_hmac signature did not match",
        ));
    }
    Ok(())
}

// ── shared helpers ───────────────────────────────────────────────────────────

/// Reject replay-window violations. Timestamp may be unix-seconds (Stripe,
/// Didit) or unix-millis (Svix): any value > 1e12 is treated as millis.
fn check_timestamp(timestamp: &str, max_skew_seconds: i64, now: Option<f64>) -> Result0 {
    let ts: i64 = timestamp.trim().parse().map_err(|_| {
        WebhookSignatureError::new(
            "malformed_header",
            format!("timestamp not an integer: {timestamp:?}"),
        )
    })?;

    let ts_seconds = if ts > 1_000_000_000_000 {
        ts as f64 / 1000.0
    } else {
        ts as f64
    };

    let current = now.unwrap_or_else(|| {
        #[allow(clippy::expect_used)]
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock after epoch")
            .as_secs_f64()
    });
    let skew = (current - ts_seconds).abs();
    if skew > max_skew_seconds as f64 {
        return Err(WebhookSignatureError::new(
            "replay_window",
            format!("timestamp skew {skew:.1}s exceeds {max_skew_seconds}s"),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn h(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn stripe_missing_and_malformed() {
        let body = b"{}";
        assert_eq!(
            verify_signature(
                "sec",
                body,
                &h(&[]),
                SignatureScheme::Stripe,
                300,
                Some(1.0)
            )
            .unwrap_err()
            .code,
            "missing_header"
        );
        assert_eq!(
            verify_signature(
                "sec",
                body,
                &h(&[("Stripe-Signature", "v1=abc")]),
                SignatureScheme::Stripe,
                300,
                Some(1.0),
            )
            .unwrap_err()
            .code,
            "malformed_header"
        );
    }

    #[test]
    fn replay_window_trips() {
        let body = b"{}";
        let err = verify_signature(
            "sec",
            body,
            &h(&[("X-Signature-V2", "aa"), ("X-Timestamp", "1000")]),
            SignatureScheme::DiditHmac,
            300,
            Some(1_000_000.0),
        )
        .unwrap_err();
        assert_eq!(err.code, "replay_window");
    }

    #[test]
    fn didit_non_hex_is_malformed() {
        let body = b"{}";
        let err = verify_signature(
            "sec",
            body,
            &h(&[("X-Signature-V2", "zzzz"), ("X-Timestamp", "1000")]),
            SignatureScheme::DiditHmac,
            300,
            Some(1000.0),
        )
        .unwrap_err();
        assert_eq!(err.code, "malformed_header");
    }
}
