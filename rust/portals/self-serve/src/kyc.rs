//! KYC verification adapter + value types. Port of `bss_self_serve.kyc`.
//!
//! **This slice (P6b s6): the prebaked adapter only** — the dev/scenario default
//! (`BSS_PORTAL_KYC_PROVIDER=prebaked`), which returns a deterministic
//! per-customer attestation with no external call. The Didit hosted-UI adapter
//! (cross-device handoff + corroborating webhook) is deferred with its
//! handoff/poll routes.
//!
//! `KycAttestation` is the only shape that crosses the BSS boundary: `last4` +
//! `hash` for the document number, no names / addresses / biometrics (v0.15 PII
//! doctrine). The document number is a deterministic stub derived from the email,
//! hashed in the same shape a real adapter produces, so the downstream
//! `document_hash_unique_per_tenant` policy still sees distinct values per email.

use sha2::{Digest, Sha256};

const PREBAKED_PROVIDER: &str = "prebaked";
const PREBAKED_ATTESTATION_ID: &str = "KYC-PREBAKED-001";

/// Result of `initiate()`. The portal redirects the customer to `redirect_url`;
/// later `session_id` is used to fetch the attestation.
#[derive(Debug, Clone)]
pub struct KycSession {
    pub session_id: String,
    pub redirect_url: String,
}

/// Verification receipt — what BSS sees. Verification-only: no names, addresses,
/// biometric URLs, MRZ, liveness scores, or the raw document number.
#[derive(Debug, Clone)]
pub struct KycAttestation {
    pub provider: String,
    pub provider_reference: String,
    pub document_type: String,
    pub document_country: String,
    pub document_number_last4: String,
    pub document_number_hash: String,
    /// ISO date string (`YYYY-MM-DD`) — the Python `date.isoformat()`.
    pub date_of_birth: String,
    pub corroboration_id: Option<String>,
}

/// The prebaked (dev/scenario) KYC adapter. `initiate` loops the `return_url`
/// straight back to the portal callback; `fetch_attestation` returns a stable
/// per-email attestation.
#[derive(Debug, Clone, Default)]
pub struct PrebakedKycAdapter;

impl PrebakedKycAdapter {
    pub fn initiate(&self, email: &str, return_url: &str) -> KycSession {
        KycSession {
            session_id: format!("prebaked-{}", email_session_token(email)),
            redirect_url: return_url.to_string(),
        }
    }

    pub fn fetch_attestation(&self, session_id: &str) -> KycAttestation {
        // session_id is "prebaked-<email_token>" — re-derive the per-customer
        // document_number for hash stability.
        let email_token = session_id.strip_prefix("prebaked-").unwrap_or("unknown");
        let email_token = if email_token.is_empty() {
            "unknown"
        } else {
            email_token
        };
        let document_number = stub_document_number(email_token);
        let digest = sha256_hex(&format!("{document_number}|SGP|{PREBAKED_PROVIDER}"));
        let last4: String = {
            let chars: Vec<char> = document_number.chars().collect();
            chars[chars.len().saturating_sub(4)..].iter().collect()
        };
        KycAttestation {
            provider: PREBAKED_PROVIDER.to_string(),
            provider_reference: PREBAKED_ATTESTATION_ID.to_string(),
            document_type: "nric".to_string(),
            document_country: "SGP".to_string(),
            document_number_last4: last4,
            document_number_hash: digest,
            date_of_birth: "1990-01-01".to_string(),
            corroboration_id: None, // prebaked has no webhook to corroborate
        }
    }
}

/// The configured KYC adapter. Only the prebaked variant is ported in this slice;
/// `Didit` lands with its handoff/poll routes.
#[derive(Debug, Clone)]
pub enum KycAdapter {
    Prebaked(PrebakedKycAdapter),
}

impl KycAdapter {
    /// Select from `BSS_PORTAL_KYC_PROVIDER`. `didit` is not yet ported — it
    /// currently falls back to prebaked with a warning rather than failing boot,
    /// since the Didit routes it needs don't exist yet either.
    pub fn from_provider(provider: &str) -> Self {
        match provider.to_lowercase().as_str() {
            "prebaked" | "myinfo" | "" => KycAdapter::Prebaked(PrebakedKycAdapter),
            other => {
                tracing::warn!(
                    provider = other,
                    "portal.kyc.provider_not_ported — falling back to prebaked (Didit lands with its routes)"
                );
                KycAdapter::Prebaked(PrebakedKycAdapter)
            }
        }
    }
}

/// Stable per-email token: `sha256(lower(email))[:16 hex]`. Same email → same
/// session id → same document hash.
fn email_session_token(email: &str) -> String {
    let hex = sha256_hex(&email.to_lowercase());
    hex[..16].to_string()
}

/// Synthesize a Singapore-NRIC-shaped document number from a token:
/// `S<7 digits><checksum-letter>`. Digits are the first 7 digit chars of the
/// token, left-padded to 7; the checksum is the first token char uppercased
/// (or `Z` when it isn't a letter).
fn stub_document_number(token: &str) -> String {
    let digit_chars: String = token
        .chars()
        .filter(|c| c.is_ascii_digit())
        .take(7)
        .collect();
    let digits = if digit_chars.is_empty() {
        // Unreachable for a sha256-hex token (always contains digits). Python's
        // fallback uses the process-seeded `hash()` and is itself non-repeatable,
        // so exact parity here is impossible; use a deterministic placeholder.
        "0000000".to_string()
    } else {
        format!("{digit_chars:0>7}")
    };
    let first = token.chars().next();
    let checksum = match first {
        Some(c) if c.is_ascii_alphabetic() => c.to_ascii_uppercase(),
        _ => 'Z',
    };
    format!("S{digits}{checksum}")
}

/// Lowercase hex SHA-256 of `value` (64 chars).
fn sha256_hex(value: &str) -> String {
    let digest = Sha256::digest(value.as_bytes());
    let mut hex = String::with_capacity(64);
    for b in digest.iter() {
        hex.push_str(&format!("{b:02x}"));
    }
    hex
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    // Golden values from the Python prebaked adapter (see PROGRESS P6b s6).
    #[test]
    fn prebaked_attestation_matches_oracle() {
        let adapter = PrebakedKycAdapter;
        let cases = [
            (
                "ada@example.sg",
                "prebaked-e2451a9dbe438bf2",
                "943E",
                "46214a44de9e853364fdda7651b017d3c09d9dfe90c4a08c4d82653eaef0d8d7",
            ),
            (
                "grace.hopper@example.sg",
                "prebaked-8d0ca0ce3c50ef9c",
                "509Z",
                "4ee526fe2f52420ccebd9ea19a717052b6cb3d1fa734fde36e266baf3043d75a",
            ),
            (
                "rusttest@example.test",
                "prebaked-3d91db7739fc94ef",
                "739Z",
                "14a5953b21443d078b1afad594572a85ff8c2dbd7374d169a66ada996c6670bd",
            ),
        ];
        for (email, session_id, last4, hash) in cases {
            let sess = adapter.initiate(email, "https://x/return");
            assert_eq!(sess.session_id, session_id, "session id for {email}");
            let att = adapter.fetch_attestation(&sess.session_id);
            assert_eq!(att.document_number_last4, last4, "last4 for {email}");
            assert_eq!(att.document_number_hash, hash, "hash for {email}");
            assert_eq!(att.provider, "prebaked");
            assert_eq!(att.provider_reference, "KYC-PREBAKED-001");
            assert_eq!(att.document_country, "SGP");
            assert_eq!(att.date_of_birth, "1990-01-01");
            assert!(att.corroboration_id.is_none());
        }
    }

    #[test]
    fn stub_document_number_shape() {
        // token[0] is a letter → uppercased checksum; first 7 digits kept.
        assert_eq!(stub_document_number("e2451a9dbe438bf2"), "S2451943E");
        // token[0] is a digit → checksum 'Z'.
        assert_eq!(stub_document_number("3d91db7739fc94ef"), "S3917739Z");
    }
}
