//! Tokenizer seam — port of `app.domain.{tokenizer,mock_tokenizer,stripe_tokenizer}`.
//!
//! The oracle's `TokenizerAdapter` Protocol is realized here as a [`Tokenizer`]
//! enum (mock | stripe) rather than a `dyn` trait — the set is closed and the
//! enum avoids an `async-trait` dep. The Stripe adapter talks to Stripe's REST
//! API via **direct reqwest** (Decision D4; the Python SDK does not port), form-
//! encoded with a `Bearer` key and an `Idempotency-Key` header, and records every
//! call to `integrations.external_call` (redacted) — the forensic trail
//! `bss external-calls` reads. `tokenize` is absent (no HTTP route calls it).

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde_json::{json, Value};
use sqlx::PgPool;
use std::time::Instant;

use crate::domain::{decide_mock_charge, ChargeResult};

const STRIPE_API_BASE: &str = "https://api.stripe.com";
const PROVIDER: &str = "stripe";

/// A tokenizer error that is *not* a normal decline. Declines are a `ChargeResult`
/// with `status="declined"`; these map to a 500 (the oracle lets `ValueError` /
/// non-`CardError` stripe errors bubble to FastAPI's default handler).
#[derive(Debug)]
pub enum TokenizerError {
    /// `ValueError` equivalent — bad argument (missing customer ref, non-positive
    /// amount).
    Value(String),
    /// Transport / non-card provider error.
    Transport(String),
    /// The operation isn't supported by this adapter (mock `retrieve_card`).
    NotSupported,
}

impl std::fmt::Display for TokenizerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TokenizerError::Value(m) => write!(f, "{m}"),
            TokenizerError::Transport(m) => write!(f, "{m}"),
            TokenizerError::NotSupported => write!(f, "operation not supported by this adapter"),
        }
    }
}

/// Card metadata fetched from Stripe (`retrieve_payment_method_card`).
#[derive(Debug, Clone)]
pub struct CardDetails {
    pub last4: String,
    pub brand: String,
    pub exp_month: Option<i32>,
    pub exp_year: Option<i32>,
}

#[derive(Clone)]
pub struct StripeConfig {
    pub api_key: String,
    pub publishable_key: String,
    pub webhook_secret: String,
    pub allow_test_card_reuse: bool,
}

/// The closed set of tokenizers. `class_name()` returns the exact Python class
/// name so `check_token_provider_matches_active` compares against the same map.
#[derive(Clone)]
pub enum Tokenizer {
    Mock,
    Stripe(StripeConfig, reqwest::Client, PgPool),
}

impl Tokenizer {
    pub fn class_name(&self) -> &'static str {
        match self {
            Tokenizer::Mock => "MockTokenizerAdapter",
            Tokenizer::Stripe(..) => "StripeTokenizerAdapter",
        }
    }

    // ── charge ───────────────────────────────────────────────────────

    pub async fn charge(
        &self,
        token: &str,
        amount: &Decimal,
        currency: &str,
        idempotency_key: &str,
        purpose: &str,
        customer_external_ref: Option<&str>,
    ) -> Result<ChargeResult, TokenizerError> {
        match self {
            Tokenizer::Mock => {
                if *amount <= Decimal::ZERO {
                    return Err(TokenizerError::Value("amount must be positive".into()));
                }
                // Simulated gateway latency (the oracle sleeps 50ms).
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                let gateway_ref = format!("mock_{}", uuid::Uuid::new_v4());
                let (status, reason, decline_code) = decide_mock_charge(token);
                Ok(ChargeResult {
                    status,
                    gateway_ref: gateway_ref.clone(),
                    reason,
                    provider_call_id: gateway_ref,
                    decline_code,
                })
            }
            Tokenizer::Stripe(cfg, http, pool) => {
                stripe_charge(
                    cfg,
                    http,
                    pool,
                    token,
                    amount,
                    currency,
                    idempotency_key,
                    purpose,
                    customer_external_ref,
                )
                .await
            }
        }
    }

    // ── ensure_customer ──────────────────────────────────────────────

    pub async fn ensure_customer(
        &self,
        bss_customer_id: &str,
        email: &str,
    ) -> Result<String, TokenizerError> {
        match self {
            Tokenizer::Mock => Ok(format!("cus_mock_{bss_customer_id}")),
            Tokenizer::Stripe(cfg, http, pool) => {
                stripe_ensure_customer(cfg, http, pool, bss_customer_id, email).await
            }
        }
    }

    // ── attach ───────────────────────────────────────────────────────

    pub async fn attach_payment_method_to_customer(
        &self,
        payment_method_id: &str,
        customer_id: &str,
    ) -> Result<(), TokenizerError> {
        match self {
            Tokenizer::Mock => Ok(()),
            Tokenizer::Stripe(cfg, http, pool) => {
                stripe_attach(cfg, http, pool, payment_method_id, customer_id).await
            }
        }
    }

    // ── retrieve card ────────────────────────────────────────────────

    pub async fn retrieve_payment_method_card(
        &self,
        payment_method_id: &str,
    ) -> Result<CardDetails, TokenizerError> {
        match self {
            // The oracle's MockTokenizerAdapter has no such method; the caller's
            // try/except falls back to placeholders on the resulting error.
            Tokenizer::Mock => Err(TokenizerError::NotSupported),
            Tokenizer::Stripe(cfg, http, _pool) => {
                stripe_retrieve_card(cfg, http, payment_method_id).await
            }
        }
    }
}

// ── Stripe REST calls (direct reqwest, D4) ───────────────────────────

#[allow(clippy::too_many_arguments)]
async fn stripe_charge(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    pool: &PgPool,
    token: &str,
    amount: &Decimal,
    currency: &str,
    idempotency_key: &str,
    purpose: &str,
    customer_external_ref: Option<&str>,
) -> Result<ChargeResult, TokenizerError> {
    if *amount <= Decimal::ZERO {
        return Err(TokenizerError::Value("amount must be positive".into()));
    }
    let customer = customer_external_ref
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            TokenizerError::Value(
                "StripeTokenizerAdapter.charge requires customer_external_ref; off-session \
             confirm=True needs an attached Stripe customer"
                    .into(),
            )
        })?;
    let amount_minor = (*amount * Decimal::from(100))
        .round()
        .to_i64()
        .ok_or_else(|| TokenizerError::Value("amount overflow".into()))?;

    let form = [
        ("amount", amount_minor.to_string()),
        ("currency", currency.to_lowercase()),
        ("customer", customer.to_string()),
        ("payment_method", token.to_string()),
        ("off_session", "true".to_string()),
        ("confirm", "true".to_string()),
        ("metadata[bss_purpose]", purpose.to_string()),
    ];

    let started = Instant::now();
    let resp = http
        .post(format!("{STRIPE_API_BASE}/v1/payment_intents"))
        .bearer_auth(&cfg.api_key)
        .header("Idempotency-Key", idempotency_key)
        .form(&form)
        .send()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?;
    let body: Value = resp
        .json()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?;
    let latency_ms = started.elapsed().as_millis() as i64;

    // Card decline → HTTP 402 with an `error` of type `card_error`.
    if let Some(err) = body.get("error") {
        let err_type = err.get("type").and_then(Value::as_str).unwrap_or("");
        if err_type == "card_error" {
            let pi_id = err
                .get("payment_intent")
                .and_then(|pi| pi.get("id"))
                .and_then(Value::as_str)
                .unwrap_or("pi_unknown")
                .to_string();
            let decline_code = err
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("card_declined")
                .to_string();
            let reason = err
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("card declined")
                .to_string();
            record_external_call(
                pool,
                "charge",
                Some(idempotency_key),
                false,
                latency_ms,
                Some(&pi_id),
                Some(&decline_code),
                Some(&reason),
                Some(json!({ "declined": true, "decline_code": decline_code })),
            )
            .await;
            return Ok(ChargeResult {
                status: "declined".into(),
                gateway_ref: pi_id.clone(),
                reason: Some(reason),
                provider_call_id: pi_id,
                decline_code: Some(decline_code),
            });
        }
        // Any non-card error is not a domain outcome — bubble to 500.
        return Err(TokenizerError::Transport(format!(
            "stripe error: {}",
            err.get("message")
                .and_then(Value::as_str)
                .unwrap_or(err_type)
        )));
    }

    let pi_id = body
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let status = body.get("status").and_then(Value::as_str).unwrap_or("");
    record_external_call(
        pool,
        "charge",
        Some(idempotency_key),
        true,
        latency_ms,
        Some(&pi_id),
        None,
        None,
        Some(redact_stripe(&body)),
    )
    .await;

    if status == "succeeded" {
        Ok(ChargeResult {
            status: "approved".into(),
            gateway_ref: pi_id.clone(),
            reason: None,
            provider_call_id: pi_id,
            decline_code: None,
        })
    } else {
        Ok(ChargeResult {
            status: "errored".into(),
            gateway_ref: pi_id.clone(),
            reason: Some(format!(
                "unexpected sync status '{status}' on off-session confirm"
            )),
            provider_call_id: pi_id,
            decline_code: None,
        })
    }
}

async fn stripe_ensure_customer(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    pool: &PgPool,
    bss_customer_id: &str,
    email: &str,
) -> Result<String, TokenizerError> {
    // Cache check first (payment.customer keyed on id + provider).
    if let Ok(Some(cached)) =
        crate::repo::lookup_customer_external_ref_for_provider(pool, bss_customer_id, PROVIDER)
            .await
    {
        if !cached.is_empty() {
            return Ok(cached);
        }
    }

    let form = [
        ("email", email.to_string()),
        ("metadata[bss_customer_id]", bss_customer_id.to_string()),
    ];
    let started = Instant::now();
    let resp = http
        .post(format!("{STRIPE_API_BASE}/v1/customers"))
        .bearer_auth(&cfg.api_key)
        .header(
            "Idempotency-Key",
            format!("ensure_customer_{bss_customer_id}"),
        )
        .form(&form)
        .send()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?;
    let body: Value = resp
        .json()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?;
    let latency_ms = started.elapsed().as_millis() as i64;

    if let Some(err) = body.get("error") {
        return Err(TokenizerError::Transport(format!(
            "stripe customer.create error: {}",
            err.get("message").and_then(Value::as_str).unwrap_or("")
        )));
    }
    let cus_id = body
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| TokenizerError::Transport("stripe customer.create: no id".into()))?
        .to_string();

    record_external_call(
        pool,
        "ensure_customer",
        Some(bss_customer_id),
        true,
        latency_ms,
        Some(&cus_id),
        None,
        None,
        Some(redact_stripe(&body)),
    )
    .await;

    // Persist the cache row (best-effort; a duplicate insert is harmless).
    let _ = crate::repo::insert_payment_customer(pool, bss_customer_id, &cus_id, PROVIDER).await;
    Ok(cus_id)
}

async fn stripe_attach(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    pool: &PgPool,
    payment_method_id: &str,
    customer_id: &str,
) -> Result<(), TokenizerError> {
    let started = Instant::now();
    let body = stripe_attach_call(cfg, http, payment_method_id, customer_id).await?;
    let latency_ms = started.elapsed().as_millis() as i64;

    if let Some(err) = body.get("error") {
        let code = err.get("code").and_then(Value::as_str).unwrap_or("");
        // Sandbox test-card relink: detach from the prior customer, re-attach.
        if cfg.allow_test_card_reuse && code == "payment_method_already_attached" {
            tracing::warn!(
                payment_method = payment_method_id,
                target_customer = customer_id,
                "stripe.test_card_relink"
            );
            stripe_detach_call(cfg, http, payment_method_id).await?;
            let re = stripe_attach_call(cfg, http, payment_method_id, customer_id).await?;
            if let Some(e2) = re.get("error") {
                let msg = e2.get("message").and_then(Value::as_str).unwrap_or("");
                record_external_call(
                    pool,
                    "attach",
                    Some(payment_method_id),
                    false,
                    latency_ms,
                    Some(payment_method_id),
                    Some(code),
                    Some(msg),
                    None,
                )
                .await;
                return Err(TokenizerError::Transport(msg.to_string()));
            }
            record_external_call(
                pool,
                "attach_test_relink",
                Some(payment_method_id),
                true,
                latency_ms,
                Some(payment_method_id),
                None,
                None,
                None,
            )
            .await;
            return Ok(());
        }
        let msg = err.get("message").and_then(Value::as_str).unwrap_or("");
        record_external_call(
            pool,
            "attach",
            Some(payment_method_id),
            false,
            latency_ms,
            Some(payment_method_id),
            Some(code),
            Some(msg),
            None,
        )
        .await;
        return Err(TokenizerError::Transport(msg.to_string()));
    }

    record_external_call(
        pool,
        "attach",
        Some(payment_method_id),
        true,
        latency_ms,
        Some(payment_method_id),
        None,
        None,
        None,
    )
    .await;
    Ok(())
}

async fn stripe_attach_call(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    payment_method_id: &str,
    customer_id: &str,
) -> Result<Value, TokenizerError> {
    let form = [("customer", customer_id.to_string())];
    http.post(format!(
        "{STRIPE_API_BASE}/v1/payment_methods/{payment_method_id}/attach"
    ))
    .bearer_auth(&cfg.api_key)
    .form(&form)
    .send()
    .await
    .map_err(|e| TokenizerError::Transport(e.to_string()))?
    .json()
    .await
    .map_err(|e| TokenizerError::Transport(e.to_string()))
}

async fn stripe_detach_call(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    payment_method_id: &str,
) -> Result<Value, TokenizerError> {
    http.post(format!(
        "{STRIPE_API_BASE}/v1/payment_methods/{payment_method_id}/detach"
    ))
    .bearer_auth(&cfg.api_key)
    .send()
    .await
    .map_err(|e| TokenizerError::Transport(e.to_string()))?
    .json()
    .await
    .map_err(|e| TokenizerError::Transport(e.to_string()))
}

async fn stripe_retrieve_card(
    cfg: &StripeConfig,
    http: &reqwest::Client,
    payment_method_id: &str,
) -> Result<CardDetails, TokenizerError> {
    let body: Value = http
        .get(format!(
            "{STRIPE_API_BASE}/v1/payment_methods/{payment_method_id}"
        ))
        .bearer_auth(&cfg.api_key)
        .send()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?
        .json()
        .await
        .map_err(|e| TokenizerError::Transport(e.to_string()))?;
    if let Some(err) = body.get("error") {
        return Err(TokenizerError::Transport(format!(
            "stripe payment_method.retrieve error: {}",
            err.get("message").and_then(Value::as_str).unwrap_or("")
        )));
    }
    let card = body.get("card").cloned().unwrap_or(Value::Null);
    Ok(CardDetails {
        last4: card
            .get("last4")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        brand: card
            .get("brand")
            .and_then(Value::as_str)
            .unwrap_or("card")
            .to_string(),
        exp_month: card
            .get("exp_month")
            .and_then(Value::as_i64)
            .map(|v| v as i32),
        exp_year: card
            .get("exp_year")
            .and_then(Value::as_i64)
            .map(|v| v as i32),
    })
}

// ── external_call forensics ──────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
async fn record_external_call(
    pool: &PgPool,
    operation: &str,
    aggregate_id: Option<&str>,
    success: bool,
    latency_ms: i64,
    provider_call_id: Option<&str>,
    error_code: Option<&str>,
    error_message: Option<&str>,
    redacted_payload: Option<Value>,
) {
    let aggregate_type = match operation {
        "charge" => Some("payment_attempt"),
        "attach" | "attach_test_relink" => Some("payment_method"),
        "ensure_customer" => Some("customer"),
        _ => None,
    };
    // Best-effort forensic write — a failure here must never fail the charge
    // (the row is diagnostic, not domain state). tenant_id/occurred_at default.
    let res = sqlx::query(
        "INSERT INTO integrations.external_call \
         (provider, operation, aggregate_type, aggregate_id, success, latency_ms, \
          provider_call_id, error_code, error_message, redacted_payload) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
    )
    .bind(PROVIDER)
    .bind(operation)
    .bind(aggregate_type)
    .bind(aggregate_id)
    .bind(success)
    .bind(latency_ms as i32)
    .bind(provider_call_id)
    .bind(error_code)
    .bind(error_message)
    .bind(redacted_payload.map(sqlx::types::Json))
    .execute(pool)
    .await;
    if let Err(e) = res {
        tracing::warn!(error = %e, operation, "external_call.record_failed");
    }
}

/// Port of `bss_webhooks.redaction._redact_stripe` — mask email/name/phone/
/// address/billing_details + card number/cvc/cvv; keep last4 + decline_code.
pub fn redact_stripe(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                let lk = k.to_lowercase();
                if matches!(
                    lk.as_str(),
                    "email"
                        | "name"
                        | "phone"
                        | "address"
                        | "billing_details"
                        | "number"
                        | "cvc"
                        | "cvv"
                ) {
                    out.insert(k.clone(), Value::String("[redacted]".into()));
                } else {
                    out.insert(k.clone(), redact_stripe(v));
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact_stripe).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn redact_masks_email_keeps_last4() {
        let body = json!({
            "id": "pi_1",
            "receipt_email": "x@y.com",
            "email": "secret@z.com",
            "charges": { "data": [{ "billing_details": { "name": "A" }, "payment_method_details": { "card": { "last4": "4242" } } }] }
        });
        let r = redact_stripe(&body);
        assert_eq!(r["email"], json!("[redacted]"));
        // billing_details is masked wholesale.
        assert_eq!(
            r["charges"]["data"][0]["billing_details"],
            json!("[redacted]")
        );
        // last4 survives (ops needs it).
        assert_eq!(
            r["charges"]["data"][0]["payment_method_details"]["card"]["last4"],
            json!("4242")
        );
    }
}
