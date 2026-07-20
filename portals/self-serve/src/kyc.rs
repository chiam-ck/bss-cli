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

use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use sqlx::PgPool;

const PREBAKED_PROVIDER: &str = "prebaked";
const PREBAKED_ATTESTATION_ID: &str = "KYC-PREBAKED-001";

// ── Didit (v0.15 real hosted-UI adapter) ─────────────────────────────────────
const DIDIT_PROVIDER: &str = "didit";
const DIDIT_BASE_URL: &str = "https://verification.didit.me";
const FREE_TIER_MONTHLY_CAP: i64 = 500;
const FREE_TIER_WARN_THRESHOLD: i64 = 450;
const CORROBORATION_POLL_INTERVAL: Duration = Duration::from_millis(200);
const CORROBORATION_POLL_TIMEOUT: Duration = Duration::from_secs(10);

/// Errors the Didit flow can surface. `CapExhausted` (monthly free-tier hit) and
/// `CorroborationTimeout` (no HMAC-verified webhook within the window) map 1:1 to
/// the Python `KycCapExhausted` / `KycCorroborationTimeout`; `Http` covers the
/// network/decode failures. Doctrine: never silently downgrade to prebaked.
#[derive(Debug)]
pub enum KycError {
    CapExhausted(String),
    CorroborationTimeout(String),
    Http(String),
}

impl std::fmt::Display for KycError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KycError::CapExhausted(m) => write!(f, "kyc.cap_exhausted: {m}"),
            KycError::CorroborationTimeout(m) => write!(f, "kyc.corroboration_timeout: {m}"),
            KycError::Http(m) => write!(f, "kyc.http_error: {m}"),
        }
    }
}
impl std::error::Error for KycError {}

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

/// The configured KYC adapter — `prebaked` (dev/scenario, synchronous) or
/// `didit` (real hosted-UI, async + corroborating webhook).
#[derive(Debug, Clone)]
pub enum KycAdapter {
    Prebaked(PrebakedKycAdapter),
    Didit(DiditKycAdapter),
}

impl KycAdapter {
    /// Select the no-DB adapter from `BSS_PORTAL_KYC_PROVIDER`. `prebaked` always
    /// resolves here; `didit` needs a DB pool (corroboration lookup) so it is
    /// built in `build_state_with_db` via [`KycAdapter::didit`] — asked for here
    /// without a pool it falls back to prebaked with a **loud** warning (never
    /// silent). Unknown names also warn + prebaked.
    pub fn from_provider(provider: &str) -> Self {
        match provider.to_lowercase().as_str() {
            "prebaked" | "myinfo" | "" => KycAdapter::Prebaked(PrebakedKycAdapter),
            "didit" => {
                tracing::warn!(
                    "portal.kyc.didit_needs_db — prebaked until the DB pool is attached \
                     (build_state_with_db); this path is the no-DB fallback only"
                );
                KycAdapter::Prebaked(PrebakedKycAdapter)
            }
            other => {
                tracing::warn!(
                    provider = other,
                    "portal.kyc.unknown_provider — falling back to prebaked"
                );
                KycAdapter::Prebaked(PrebakedKycAdapter)
            }
        }
    }

    /// Construct the Didit adapter with a DB pool (fail-fast on missing creds —
    /// mirrors the Python `select_kyc_adapter`). Called from `build_state_with_db`.
    pub fn didit(api_key: &str, workflow_id: &str, pool: PgPool) -> Result<Self, String> {
        if api_key.is_empty() {
            return Err("BSS_PORTAL_KYC_PROVIDER=didit requires BSS_PORTAL_KYC_DIDIT_API_KEY".into());
        }
        if workflow_id.is_empty() {
            return Err(
                "BSS_PORTAL_KYC_PROVIDER=didit requires BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID".into(),
            );
        }
        Ok(KycAdapter::Didit(DiditKycAdapter::new(
            api_key.to_string(),
            workflow_id.to_string(),
            pool,
        )))
    }

    /// `true` for the synchronous prebaked adapter — the signup flow completes
    /// the attest in-request; `false` (Didit) drives the cross-device handoff.
    pub fn is_prebaked(&self) -> bool {
        matches!(self, KycAdapter::Prebaked(_))
    }

    /// Uniform async dispatch — start a verification session.
    pub async fn initiate(&self, email: &str, return_url: &str) -> Result<KycSession, KycError> {
        match self {
            KycAdapter::Prebaked(a) => Ok(a.initiate(email, return_url)),
            KycAdapter::Didit(a) => a.initiate(email, return_url).await,
        }
    }

    /// Uniform async dispatch — read back the verified attestation.
    pub async fn fetch_attestation(&self, session_id: &str) -> Result<KycAttestation, KycError> {
        match self {
            KycAdapter::Prebaked(a) => Ok(a.fetch_attestation(session_id)),
            KycAdapter::Didit(a) => a.fetch_attestation(session_id).await,
        }
    }
}

/// The real Didit hosted-UI KYC adapter. Port of `bss_self_serve.kyc.didit`.
///
/// Trust model: Didit's decision endpoint returns unsigned JSON, so the trust
/// anchor is the HMAC-signed webhook recorded in
/// `integrations.kyc_webhook_corroboration`. `fetch_attestation` blocks on that
/// row before returning. Privacy: the decision carries raw NRIC / name / address
/// / biometrics; `build_attestation` reduces the document number to `last4 +
/// hash` and drops everything else — nothing else crosses the BSS boundary.
#[derive(Clone)]
pub struct DiditKycAdapter {
    api_key: String,
    workflow_id: String,
    http: reqwest::Client,
    pool: PgPool,
    poll_interval: Duration,
    poll_timeout: Duration,
}

impl std::fmt::Debug for DiditKycAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never render the api_key.
        f.debug_struct("DiditKycAdapter")
            .field("workflow_id", &self.workflow_id)
            .finish_non_exhaustive()
    }
}

impl DiditKycAdapter {
    pub fn new(api_key: String, workflow_id: String, pool: PgPool) -> Self {
        Self {
            api_key,
            workflow_id,
            http: reqwest::Client::new(),
            pool,
            poll_interval: CORROBORATION_POLL_INTERVAL,
            poll_timeout: CORROBORATION_POLL_TIMEOUT,
        }
    }

    async fn initiate(&self, email: &str, return_url: &str) -> Result<KycSession, KycError> {
        self.guard_free_tier_cap().await?;

        let body = serde_json::json!({
            "workflow_id": self.workflow_id,
            "vendor_data": format!("bss-cli-{email}"),
            "callback": return_url,
        });
        let start = Instant::now();
        let result = self
            .http
            .post(format!("{DIDIT_BASE_URL}/v2/session/"))
            .header("x-api-key", &self.api_key)
            .header("content-type", "application/json")
            .json(&body)
            .send()
            .await;

        let payload = match self.decode_json(result).await {
            Ok(v) => v,
            Err(e) => {
                self.record_external_call("initiate", None, false, start.elapsed(), None)
                    .await;
                return Err(e);
            }
        };
        let session_id = payload
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let url = payload
            .get("url")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        self.record_external_call(
            "initiate",
            Some(&session_id),
            true,
            start.elapsed(),
            Some(&session_id),
        )
        .await;
        Ok(KycSession {
            session_id,
            redirect_url: url,
        })
    }

    async fn fetch_attestation(&self, session_id: &str) -> Result<KycAttestation, KycError> {
        // 1. Wait for the corroborating HMAC-verified webhook row.
        let corroboration = self.wait_for_corroboration(session_id).await;
        let Some((corroboration_id, _status)) = corroboration else {
            return Err(KycError::CorroborationTimeout(format!(
                "No verified webhook delivery for Didit session {session_id} within {}s",
                self.poll_timeout.as_secs()
            )));
        };

        // 2. Fetch the (unsigned) decision body for the supplementary fields.
        let start = Instant::now();
        let result = self
            .http
            .get(format!("{DIDIT_BASE_URL}/v2/session/{session_id}/decision/"))
            .header("x-api-key", &self.api_key)
            .send()
            .await;
        let decision = match self.decode_json(result).await {
            Ok(v) => v,
            Err(e) => {
                self.record_external_call(
                    "fetch_attestation",
                    Some(session_id),
                    false,
                    start.elapsed(),
                    None,
                )
                .await;
                return Err(e);
            }
        };
        self.record_external_call(
            "fetch_attestation",
            Some(session_id),
            true,
            start.elapsed(),
            None,
        )
        .await;

        // 3. PII reduction. After this, raw doc number / name / address are gone.
        Ok(build_attestation(&decision, Some(corroboration_id)))
    }

    /// Await success or map an HTTP/status/decode failure into `KycError::Http`.
    async fn decode_json(
        &self,
        result: Result<reqwest::Response, reqwest::Error>,
    ) -> Result<serde_json::Value, KycError> {
        let resp = result.map_err(|e| KycError::Http(e.to_string()))?;
        let status = resp.status();
        if !status.is_success() {
            let preview: String = resp
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(200)
                .collect();
            return Err(KycError::Http(format!("didit HTTP {status}: {preview}")));
        }
        resp.json::<serde_json::Value>()
            .await
            .map_err(|e| KycError::Http(e.to_string()))
    }

    /// Hard-block at the monthly free-tier cap. No silent fallback (Motto).
    async fn guard_free_tier_cap(&self) -> Result<(), KycError> {
        let count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM integrations.external_call \
             WHERE provider = $1 AND operation = 'initiate' \
             AND occurred_at >= date_trunc('month', now())",
        )
        .bind(DIDIT_PROVIDER)
        .fetch_one(&self.pool)
        .await
        .unwrap_or(0);

        if count >= FREE_TIER_MONTHLY_CAP {
            tracing::warn!(count, cap = FREE_TIER_MONTHLY_CAP, "didit.cap_exhausted");
            return Err(KycError::CapExhausted(format!(
                "Didit free-tier monthly cap ({FREE_TIER_MONTHLY_CAP}) reached"
            )));
        }
        if count >= FREE_TIER_WARN_THRESHOLD {
            tracing::warn!(count, cap = FREE_TIER_MONTHLY_CAP, "didit.cap_warning");
        }
        Ok(())
    }

    /// Poll `integrations.kyc_webhook_corroboration` for the row. Returns
    /// `(corroboration_id, decision_status)` once the HMAC-verified webhook lands.
    async fn wait_for_corroboration(&self, session_id: &str) -> Option<(String, String)> {
        let deadline = Instant::now() + self.poll_timeout;
        loop {
            let row: Option<(uuid::Uuid, String)> = sqlx::query_as(
                "SELECT id, decision_status FROM integrations.kyc_webhook_corroboration \
                 WHERE provider = $1 AND provider_session_id = $2",
            )
            .bind(DIDIT_PROVIDER)
            .bind(session_id)
            .fetch_optional(&self.pool)
            .await
            .ok()
            .flatten();
            if let Some((id, status)) = row {
                return Some((id.to_string(), status));
            }
            if Instant::now() >= deadline {
                return None;
            }
            tokio::time::sleep(self.poll_interval).await;
        }
    }

    async fn record_external_call(
        &self,
        operation: &str,
        aggregate_id: Option<&str>,
        success: bool,
        latency: Duration,
        provider_call_id: Option<&str>,
    ) {
        let _ = sqlx::query(
            "INSERT INTO integrations.external_call \
             (provider, operation, aggregate_type, aggregate_id, success, latency_ms, provider_call_id) \
             VALUES ($1,$2,$3,$4,$5,$6,$7)",
        )
        .bind(DIDIT_PROVIDER)
        .bind(operation)
        .bind(aggregate_id.map(|_| "kyc_session"))
        .bind(aggregate_id)
        .bind(success)
        .bind(latency.as_millis() as i32)
        .bind(provider_call_id)
        .execute(&self.pool)
        .await;
    }
}

/// Reduce a Didit decision payload to the BSS-bound shape. **THE PII REDUCTION
/// POINT** — after this returns, raw document_number / name / address / image
/// URLs are gone. Port of `_build_attestation`.
fn build_attestation(decision: &serde_json::Value, corroboration_id: Option<String>) -> KycAttestation {
    let idv = decision.get("id_verification");
    let get_str = |k: &str| -> String {
        idv.and_then(|v| v.get(k))
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string()
    };
    let raw_doc_number = get_str("document_number");
    let document_country = {
        let c = get_str("issuing_state");
        if c.is_empty() { "SGP".to_string() } else { c }
    };

    let normalized = raw_doc_number.to_uppercase();
    let normalized = normalized.trim();
    let digest = sha256_hex(&format!("{normalized}|{document_country}|{DIDIT_PROVIDER}"));
    let last4: String = if normalized.chars().count() >= 4 {
        normalized.chars().skip(normalized.chars().count() - 4).collect()
    } else {
        normalized.to_string()
    };

    let dob = {
        let d = get_str("date_of_birth");
        if d.is_empty() { "1900-01-01".to_string() } else { d }
    };

    let raw_type = {
        let t = get_str("document_type").to_lowercase();
        if t.is_empty() { "identity card".to_string() } else { t }
    };
    let document_type = match raw_type.as_str() {
        "identity card" => "nric",
        "passport" => "passport",
        "driver's license" => "drivers_license",
        "fin" => "fin",
        other => other,
    }
    .to_string();

    let provider_reference = decision
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();

    KycAttestation {
        provider: DIDIT_PROVIDER.to_string(),
        provider_reference,
        document_type,
        document_country,
        document_number_last4: last4,
        document_number_hash: digest,
        date_of_birth: dob,
        corroboration_id,
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
    fn didit_build_attestation_reduces_pii() {
        // A representative decision payload — raw NRIC + name + address present.
        let decision = serde_json::json!({
            "session_id": "sess-abc-123",
            "id_verification": {
                "document_number": "s1234567d",
                "issuing_state": "SGP",
                "date_of_birth": "1985-03-02",
                "document_type": "Identity Card",
                "first_name": "Ada",       // must NOT survive
                "last_name": "Lovelace",   // must NOT survive
                "address": "1 Raffles Pl"  // must NOT survive
            }
        });
        let att = build_attestation(&decision, Some("corr-1".to_string()));
        assert_eq!(att.provider, "didit");
        assert_eq!(att.provider_reference, "sess-abc-123");
        assert_eq!(att.document_type, "nric");
        assert_eq!(att.document_country, "SGP");
        assert_eq!(att.document_number_last4, "567D"); // normalized upper, last4
        assert_eq!(att.date_of_birth, "1985-03-02");
        assert_eq!(att.corroboration_id.as_deref(), Some("corr-1"));
        // Digest is domain-separated sha256(NORMALIZED|COUNTRY|didit).
        assert_eq!(att.document_number_hash, sha256_hex("S1234567D|SGP|didit"));
        // The reduced shape has no field that could carry name/address.
        assert_eq!(att.document_number_hash.len(), 64);
    }

    #[test]
    fn didit_build_attestation_defaults_on_empty() {
        let att = build_attestation(&serde_json::json!({}), None);
        assert_eq!(att.document_country, "SGP");
        assert_eq!(att.date_of_birth, "1900-01-01");
        assert_eq!(att.document_type, "nric"); // "identity card" default
        assert!(att.corroboration_id.is_none());
    }

    #[test]
    fn stub_document_number_shape() {
        // token[0] is a letter → uppercased checksum; first 7 digits kept.
        assert_eq!(stub_document_number("e2451a9dbe438bf2"), "S2451943E");
        // token[0] is a digit → checksum 'Z'.
        assert_eq!(stub_document_number("3d91db7739fc94ef"), "S3917739Z");
    }
}
