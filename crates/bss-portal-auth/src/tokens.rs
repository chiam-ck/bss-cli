//! Token primitives — generation, hashing, timing-safe comparison. Port of
//! `bss_portal_auth.tokens`.
//!
//! Doctrine (V0_8_0.md §1.5):
//! * OTP — 6 numeric digits via a CSPRNG (`rand::rngs::OsRng`, the analogue of
//!   Python's `secrets.choice`).
//! * Magic link / session id / step-up grant — 32-char URL-safe (Python's
//!   `secrets.token_urlsafe(24)`: 24 random bytes → base64url, no padding → 32
//!   chars).
//! * Stored as **hex** HMAC-SHA-256 of `token` keyed by the pepper (Python's
//!   `.hexdigest()`), pepper from `BSS_PORTAL_TOKEN_PEPPER` (≥32 chars, validated
//!   at startup via [`crate::startup::validate_pepper_present`]).
//! * Comparison is timing-safe (`subtle::ConstantTimeEq`, the analogue of
//!   `hmac.compare_digest`) — never `==`, never short-circuits.
//!
//! Pure — no DB, no ORM. The session-binding logic lives in the service layer.

use hmac::{Hmac, Mac};
use rand::rngs::OsRng;
use rand::RngCore;
use sha2::Sha256;
use subtle::ConstantTimeEq;

use crate::config::Settings;

pub const OTP_LENGTH: usize = 6;
const MAGIC_LINK_BYTES: usize = 24; // token_urlsafe(24) -> 32 chars URL-safe

/// Raised when the pepper is empty at hash time. Mirrors the Python
/// `RuntimeError` — a regression from the startup validator must not silently
/// downgrade to "all tokens hash to the same value".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PepperMissing(pub String);

impl std::fmt::Display for PepperMissing {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for PepperMissing {}

/// Return a 6-digit numeric OTP, from a CSPRNG (not a PRNG).
pub fn generate_otp() -> String {
    let mut out = String::with_capacity(OTP_LENGTH);
    for _ in 0..OTP_LENGTH {
        // 0..=9, unbiased (10 divides 256 → rejection not needed for parity;
        // use rejection anyway to match `secrets.choice`'s uniform guarantee).
        let d = loop {
            let mut b = [0u8; 1];
            OsRng.fill_bytes(&mut b);
            if b[0] < 250 {
                break b[0] % 10;
            }
        };
        out.push((b'0' + d) as char);
    }
    out
}

/// 24 random bytes → URL-safe base64 without padding → 32 chars. Matches
/// Python's `secrets.token_urlsafe(24)`.
fn token_urlsafe_24() -> String {
    let mut bytes = [0u8; MAGIC_LINK_BYTES];
    OsRng.fill_bytes(&mut bytes);
    base64url_nopad(&bytes)
}

/// Return a 32-char URL-safe token for a magic link's `?token=`.
pub fn generate_magic_link_token() -> String {
    token_urlsafe_24()
}

/// Return a 32-char URL-safe opaque session id (cookie value).
pub fn generate_session_id() -> String {
    token_urlsafe_24()
}

/// One-shot token returned after a successful step-up verify (stored hashed;
/// forwarded as `X-BSS-StepUp-Token`, consumed once).
pub fn generate_step_up_grant() -> String {
    token_urlsafe_24()
}

/// HMAC-SHA-256 of `token` keyed by the server pepper, hex-encoded (predictable
/// column type). `pepper=None` reads the Settings value — explicit override is a
/// test convenience. Errors when the pepper is empty (defensive; the startup
/// validator should have caught it).
pub fn hash_token(token: &str, pepper: Option<&str>) -> Result<String, PepperMissing> {
    let resolved: String;
    let pepper = match pepper {
        Some(p) => p,
        None => {
            resolved = Settings::from_env().token_pepper;
            &resolved
        }
    };
    if pepper.is_empty() {
        return Err(PepperMissing(
            "BSS_PORTAL_TOKEN_PEPPER missing — call validate_pepper_present() in \
             lifespan startup before any auth flow runs."
                .to_string(),
        ));
    }
    // HMAC accepts a key of any length, so `new_from_slice` is infallible.
    #[allow(clippy::expect_used)]
    let mut mac = <Hmac<Sha256>>::new_from_slice(pepper.as_bytes())
        .expect("HMAC-SHA256 accepts any key length");
    mac.update(token.as_bytes());
    let digest = mac.finalize().into_bytes();
    Ok(hex_lower(&digest))
}

/// Timing-safe verify: hash `token` and compare to `expected_hash`. Always
/// constant-time — never short-circuits on length, never uses `==`. A pepper
/// error hashes to `Err`, which verifies as `false`.
pub fn verify_token(token: &str, expected_hash: &str, pepper: Option<&str>) -> bool {
    match hash_token(token, pepper) {
        Ok(actual) => actual.as_bytes().ct_eq(expected_hash.as_bytes()).into(),
        Err(_) => false,
    }
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

/// URL-safe base64 without padding (`-`/`_` alphabet), matching Python's
/// `base64.urlsafe_b64encode(...).rstrip(b"=")` as used by `secrets.token_urlsafe`.
fn base64url_nopad(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((n >> 18) & 0x3f) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((n >> 6) & 0x3f) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(n & 0x3f) as usize] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn otp_is_six_numeric_digits() {
        for _ in 0..50 {
            let otp = generate_otp();
            assert_eq!(otp.len(), OTP_LENGTH);
            assert!(otp.chars().all(|c| c.is_ascii_digit()));
        }
    }

    #[test]
    fn otps_are_random_enough() {
        let sample: std::collections::HashSet<_> = (0..200).map(|_| generate_otp()).collect();
        assert!(sample.len() > 100);
    }

    #[test]
    fn urlsafe_tokens_are_32_chars() {
        for _ in 0..20 {
            let tok = generate_magic_link_token();
            assert_eq!(tok.len(), 32);
            assert!(tok
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        }
        assert_eq!(generate_session_id().len(), 32);
        assert_eq!(generate_step_up_grant().len(), 32);
    }

    #[test]
    fn hash_is_hex_sha256_64_chars() {
        let h = hash_token("123456", Some("x".repeat(32).as_str())).unwrap();
        assert_eq!(h.len(), 64);
        assert!(h
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    /// Golden vectors captured from the Python oracle
    /// (`hmac.new(pepper, token, sha256).hexdigest()`). Byte-parity gate.
    #[test]
    fn hash_matches_oracle_golden_vectors() {
        let vectors = [
            (
                "123456",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "40ac13afdba7a3991909643e4afc10ff10a735a81e1a978c7c6305aab383c896",
            ),
            (
                "424242",
                "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
                "3c2588e46253525125c48e1ecce8a38de518b97e37f7e8d4626d9a6b137ba216",
            ),
            (
                "000000",
                "abcdefghijklmnopqrstuvwxyz012345",
                "b806040129a599674f86c3cb435d20f18daf17d83f23bde5b63584a84ad74834",
            ),
            (
                "aB-_xyz789TOKENurlsafe0123456789",
                "pepper-with-unicode-caf\u{e9}-0123456789",
                "251937956b4b8175709f29de6be5ddf3031800f55220228219b58b11567737f7",
            ),
            (
                "",
                "peppered0123456789012345678901234567",
                "306d04f65c2bf9597620e1009f0a1c1dd6f0a40671d7437c2720744cc7ebdec5",
            ),
        ];
        for (token, pepper, expected) in vectors {
            assert_eq!(
                hash_token(token, Some(pepper)).unwrap(),
                expected,
                "token={token:?}"
            );
        }
    }

    #[test]
    fn hash_deterministic_and_pepper_sensitive() {
        let a = hash_token("123456", Some("x".repeat(32).as_str())).unwrap();
        let b = hash_token("123456", Some("x".repeat(32).as_str())).unwrap();
        let c = hash_token("123456", Some("y".repeat(32).as_str())).unwrap();
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn verify_token_passes_correct_rejects_wrong() {
        let pepper = "z".repeat(32);
        let h = hash_token("424242", Some(&pepper)).unwrap();
        assert!(verify_token("424242", &h, Some(&pepper)));
        assert!(!verify_token("000000", &h, Some(&pepper)));
    }

    #[test]
    fn hash_errors_when_pepper_empty() {
        assert!(hash_token("123456", Some("")).is_err());
    }
}
