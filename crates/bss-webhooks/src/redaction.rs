//! Per-provider payload redaction before logging or persistence. Port of
//! `bss_webhooks.redaction`.
//!
//! Every provider's response leaks something (Resend → recipient, Stripe →
//! customer email, Didit → raw document numbers). `redact_provider_payload` is
//! called at every persistence point (greppable doctrine); unknown providers
//! fall through to an identity transform, so a new provider must add a rule
//! rather than silently rely on the fallback.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const MASK: &str = "[redacted]";

/// Stable, greppable, non-reversible hash for PII strings:
/// `sha256:<first-16-hex>`.
fn hash_pii(value: &str) -> String {
    let digest = Sha256::digest(value.as_bytes());
    let mut hex = String::with_capacity(64);
    for b in digest.iter() {
        hex.push_str(&format!("{b:02x}"));
    }
    format!("sha256:{}", &hex[..16])
}

/// Return a redacted copy of `body` keyed on `provider`. Pure, recursive across
/// objects + arrays; scalars pass through unless the field name matches a rule.
/// Unknown providers return the value unchanged.
pub fn redact_provider_payload(provider: &str, body: &Value) -> Value {
    match provider {
        "resend" => redact_resend(body),
        "stripe" => redact_stripe(body),
        "didit" => redact_didit(body),
        _ => body.clone(),
    }
}

fn masked() -> Value {
    Value::String(MASK.to_string())
}

fn redact_resend(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                let lk = k.to_lowercase();
                if matches!(lk.as_str(), "to" | "from" | "reply_to" | "cc" | "bcc") {
                    out.insert(k.clone(), masked());
                } else {
                    out.insert(k.clone(), redact_resend(v));
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact_resend).collect()),
        other => other.clone(),
    }
}

fn redact_stripe(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                let lk = k.to_lowercase();
                if matches!(
                    lk.as_str(),
                    "email" | "name" | "phone" | "address" | "billing_details"
                ) || matches!(lk.as_str(), "number" | "cvc" | "cvv")
                {
                    out.insert(k.clone(), masked());
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

fn redact_didit(value: &Value) -> Value {
    const DOC_FIELDS: &[&str] = &[
        "document_number",
        "id_number",
        "national_id",
        "nric",
        "passport_number",
    ];
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                let lk = k.to_lowercase();
                // Doc numbers + DOB: hash the string (else recurse). Same rule
                // for both field groups, so they share a branch.
                if DOC_FIELDS.contains(&lk.as_str())
                    || matches!(lk.as_str(), "date_of_birth" | "dob" | "birth_date")
                {
                    match v {
                        Value::String(s) => out.insert(k.clone(), Value::String(hash_pii(s))),
                        _ => out.insert(k.clone(), redact_didit(v)),
                    };
                } else if matches!(
                    lk.as_str(),
                    "first_name" | "last_name" | "full_name" | "name"
                ) {
                    out.insert(k.clone(), masked());
                } else {
                    out.insert(k.clone(), redact_didit(v));
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact_didit).collect()),
        other => other.clone(),
    }
}
