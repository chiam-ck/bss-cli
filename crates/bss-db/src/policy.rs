//! `PolicyViolation` — the single most load-bearing payload in the system.
//!
//! The LLM reads this as a tool observation and decides whether to retry or ask
//! the user, so the wire shape is a **frozen contract** (phases/2.0/00-STRATEGY.md
//! §3.2). Port of the services' `policies/base.PolicyViolation` (the raise side)
//! and the `RequestIdMiddleware` 422 serialization (the wire side).
//!
//! Internal field is `rule`; on the wire it serializes as `reason` (TMF-style)
//! plus a derived `referenceError`. Clients read `reason` back — see
//! `bss_clients.base._handle_response`, reproduced by [`PolicyViolation::from_wire`].

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};

/// The `code` discriminant every policy-violation body carries.
pub const POLICY_VIOLATION_CODE: &str = "POLICY_VIOLATION";

/// A domain-invariant violation raised by the policy layer before any write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyViolation {
    /// Stable rule id, e.g. `case.close.requires_all_tickets_resolved`. Serializes
    /// to the wire field `reason`.
    pub rule: String,
    /// Human-readable explanation (also the `Display` / error message).
    pub message: String,
    /// Structured context (defaults to an empty object, matching Python's
    /// `context or {}`). Always a JSON object on the wire.
    pub context: Value,
}

impl PolicyViolation {
    /// A violation with empty context.
    pub fn new(rule: impl Into<String>, message: impl Into<String>) -> Self {
        PolicyViolation {
            rule: rule.into(),
            message: message.into(),
            context: json!({}),
        }
    }

    /// A violation carrying structured context (e.g. the offending ids).
    pub fn with_context(
        rule: impl Into<String>,
        message: impl Into<String>,
        context: Value,
    ) -> Self {
        PolicyViolation {
            rule: rule.into(),
            message: message.into(),
            context,
        }
    }

    /// The `referenceError` URL derived from the rule id.
    pub fn reference_error(&self) -> String {
        format!("https://docs.bss-cli.dev/policies/{}", self.rule)
    }

    /// The exact 422 body, byte-shaped like `RequestIdMiddleware`'s serialization:
    /// `{code, reason, message, referenceError, context}`.
    pub fn to_wire(&self) -> Value {
        json!({
            "code": POLICY_VIOLATION_CODE,
            "reason": self.rule,
            "message": self.message,
            "referenceError": self.reference_error(),
            "context": self.context,
        })
    }

    /// Parse a downstream 422 body back into a `PolicyViolation`, mirroring
    /// `bss_clients.base._handle_response` (reads `reason`/`message`/`context`).
    /// Returns `None` when `code != "POLICY_VIOLATION"` or required fields are
    /// missing.
    pub fn from_wire(body: &Value) -> Option<Self> {
        if body.get("code").and_then(Value::as_str) != Some(POLICY_VIOLATION_CODE) {
            return None;
        }
        let rule = body.get("reason").and_then(Value::as_str)?.to_string();
        let message = body.get("message").and_then(Value::as_str)?.to_string();
        let context = body.get("context").cloned().unwrap_or_else(|| json!({}));
        Some(PolicyViolation {
            rule,
            message,
            context,
        })
    }
}

impl std::fmt::Display for PolicyViolation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for PolicyViolation {}

/// Renders as HTTP 422 with the policy-violation body. This is what makes the
/// structured contract compiler-enforced: any handler that returns a
/// `PolicyViolation` produces exactly the shape the LLM expects.
impl IntoResponse for PolicyViolation {
    fn into_response(self) -> Response {
        (StatusCode::UNPROCESSABLE_ENTITY, Json(self.to_wire())).into_response()
    }
}
