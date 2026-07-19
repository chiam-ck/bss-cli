//! Log-field redaction — port of the services' `app/logging.redact_sensitive`.
//!
//! Key-based: a field whose name is in [`REDACTED_KEYS`] (and does not end with a
//! [`SAFE_SUFFIXES`] entry) has its value replaced with [`REDACTED`]. This is the
//! substrate the doctrine relies on ("Don't log card numbers, tokens, full NRIC,
//! full Ki, or full ICCIDs beyond last-4"). The tracing `Layer` that applies this
//! to live log events lands with the OTel bootstrap (conformance-service step);
//! these rules are the reusable, tested core.

use serde_json::{Map, Value};

/// Replacement value for a redacted field.
pub const REDACTED: &str = "***REDACTED***";

/// Field names whose values must never be logged in full.
pub const REDACTED_KEYS: &[&str] = &[
    "document_number",
    "cvv",
    "card_number",
    "ki",
    "pan",
    "password",
    "token",
];

/// A field is safe (not redacted) if it ends with one of these — e.g. `ki_ref`,
/// `token_id`. Belt-and-suspenders: [`REDACTED_KEYS`] holds exact names, none of
/// which end this way, so this guards against a future exact-name collision.
pub const SAFE_SUFFIXES: &[&str] = &["_ref", "_id"];

/// Whether a field with this key should be redacted. Mirrors
/// `key in REDACTED_KEYS and not any(key.endswith(s) for s in SAFE_SUFFIXES)`.
pub fn should_redact(key: &str) -> bool {
    REDACTED_KEYS.contains(&key) && !SAFE_SUFFIXES.iter().any(|s| key.ends_with(s))
}

/// Redact the top-level keys of a structured log event in place (the structlog
/// `event_dict` equivalent). Only top-level keys are considered — nested objects
/// are not recursed, matching the Python processor.
pub fn redact_event(event: &mut Map<String, Value>) {
    let keys: Vec<String> = event.keys().cloned().collect();
    for key in keys {
        if should_redact(&key) {
            event.insert(key, Value::String(REDACTED.to_string()));
        }
    }
}
