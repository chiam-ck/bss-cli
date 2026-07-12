//! Pure mediation domain — the block-at-edge policies and the event payload
//! builders, factored out so the whole decision is CI-testable without infra.
//!
//! Ports `app.policies.usage` (the policies enforced *before* any `usage_event`
//! row is persisted) and `app.repositories.usage_repo.to_payload` +
//! `MediationService._record_rejection`'s payload. The I/O — Subscription
//! enrichment and the DB writes — stays in [`crate::service`]; the branching and
//! shapes live here.
//!
//! Roaming purity (doctrine guard 11): `roaming_indicator` is a per-event
//! attribute passed through to the `usage.recorded` payload. It is **not** a new
//! event type — [`VALID_EVENT_TYPES`] stays `{data, voice, voice_minutes, sms}`.

use bss_db::PolicyViolation;
use serde_json::{json, Value};

/// The four accepted usage event types. Frozen — roaming is an attribute, not a
/// type (v0.17 doctrine guard 11).
pub const VALID_EVENT_TYPES: [&str; 4] = ["data", "voice", "voice_minutes", "sms"];

/// `usage.record.positive_quantity` — quantity must be strictly positive.
pub fn check_positive_quantity(quantity: i64) -> Result<(), PolicyViolation> {
    if quantity <= 0 {
        return Err(PolicyViolation::with_context(
            "usage.record.positive_quantity",
            format!("Quantity must be positive, got {quantity}"),
            json!({ "quantity": quantity }),
        ));
    }
    Ok(())
}

/// `usage.record.valid_event_type` — event type must be one of [`VALID_EVENT_TYPES`].
pub fn check_valid_event_type(event_type: &str) -> Result<(), PolicyViolation> {
    if !VALID_EVENT_TYPES.contains(&event_type) {
        // `sorted(VALID_EVENT_TYPES)` — the frozenset is rendered sorted in the
        // Python context; keep the same order.
        let mut valid: Vec<&str> = VALID_EVENT_TYPES.to_vec();
        valid.sort_unstable();
        return Err(PolicyViolation::with_context(
            "usage.record.valid_event_type",
            format!("Invalid event type '{event_type}'"),
            json!({ "event_type": event_type, "valid_types": valid }),
        ));
    }
    Ok(())
}

/// The `usage.record.subscription_must_exist` violation — built when the
/// Subscription lookup 404s (the I/O lives in [`crate::service`]).
pub fn subscription_not_found(msisdn: &str) -> PolicyViolation {
    PolicyViolation::with_context(
        "usage.record.subscription_must_exist",
        format!("No subscription for MSISDN {msisdn}"),
        json!({ "msisdn": msisdn }),
    )
}

/// `usage.record.msisdn_belongs_to_subscription` — the enriched subscription's
/// MSISDN must equal the ingress MSISDN (defensive).
pub fn check_msisdn_matches(subscription: &Value, msisdn: &str) -> Result<(), PolicyViolation> {
    let sub_msisdn = subscription.get("msisdn").and_then(Value::as_str);
    if sub_msisdn != Some(msisdn) {
        let shown = sub_msisdn.unwrap_or("None");
        return Err(PolicyViolation::with_context(
            "usage.record.msisdn_belongs_to_subscription",
            format!(
                "Enriched subscription MSISDN '{shown}' does not match request MSISDN '{msisdn}'"
            ),
            json!({
                "request_msisdn": msisdn,
                "subscription_msisdn": subscription.get("msisdn").cloned().unwrap_or(Value::Null),
                "subscription_id": subscription.get("id").cloned().unwrap_or(Value::Null),
            }),
        ));
    }
    Ok(())
}

/// `usage.record.subscription_must_be_active` — block-at-edge: no usage recorded
/// for a non-active subscription.
pub fn check_subscription_active(subscription: &Value) -> Result<(), PolicyViolation> {
    let state = subscription.get("state").and_then(Value::as_str);
    if state != Some("active") {
        let id = subscription
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("None");
        let state_shown = state.unwrap_or("None");
        return Err(PolicyViolation::with_context(
            "usage.record.subscription_must_be_active",
            format!("Subscription {id} is {state_shown}, not active"),
            json!({
                "subscription_id": subscription.get("id").cloned().unwrap_or(Value::Null),
                "state": subscription.get("state").cloned().unwrap_or(Value::Null),
            }),
        ));
    }
    Ok(())
}

/// The fields of an accepted usage event, used to build both the `usage_event`
/// row and the `usage.recorded` payload.
#[derive(Debug, Clone)]
pub struct UsageEvent {
    pub id: String,
    pub msisdn: String,
    pub subscription_id: Option<String>,
    pub event_type: String,
    pub event_time: chrono::DateTime<chrono::Utc>,
    pub quantity: i64,
    pub unit: String,
    pub source: Option<String>,
    pub raw_cdr_ref: Option<String>,
    pub roaming_indicator: bool,
}

/// Build the `usage.recorded` payload — port of `UsageEventRepository.to_payload`
/// with the `offeringId` extra the service passes from the enriched subscription.
pub fn usage_recorded_payload(evt: &UsageEvent, offering_id: Option<&str>) -> Value {
    json!({
        "usageEventId": evt.id,
        "subscriptionId": evt.subscription_id,
        "msisdn": evt.msisdn,
        "eventType": evt.event_type,
        "eventTime": bss_clock::isoformat(evt.event_time),
        "quantity": evt.quantity,
        "unit": evt.unit,
        "source": evt.source,
        "rawCdrRef": evt.raw_cdr_ref,
        // v0.17 — rating consumer reads this to decide whether to decrement
        // `data_roaming` instead of `data`.
        "roamingIndicator": evt.roaming_indicator,
        "offeringId": offering_id,
    })
}

/// Build the `usage.rejected` payload — port of `MediationService._record_rejection`.
/// No `usage_event` row is written for a rejection; this audit-only payload is the
/// sole trace, so the attempt is observable without corrupting the CDR stream.
#[allow(clippy::too_many_arguments)]
pub fn rejection_payload(
    msisdn: &str,
    subscription_id: Option<&str>,
    state: Option<&str>,
    event_type: &str,
    event_time: chrono::DateTime<chrono::Utc>,
    quantity: i64,
    unit: &str,
    source: Option<&str>,
    raw_cdr_ref: Option<&str>,
    reason: &str,
) -> Value {
    json!({
        "msisdn": msisdn,
        "subscriptionId": subscription_id,
        "state": state,
        "eventType": event_type,
        "eventTime": bss_clock::isoformat(event_time),
        "quantity": quantity,
        "unit": unit,
        "source": source,
        "rawCdrRef": raw_cdr_ref,
        "reason": reason,
        "rejectedAt": bss_clock::isoformat(bss_clock::now()),
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    // ── check_positive_quantity — ports test_usage_policies.py ──────────────

    #[test]
    fn positive_quantity_passes() {
        check_positive_quantity(1).unwrap();
        check_positive_quantity(1_000_000).unwrap();
    }

    #[test]
    fn positive_quantity_rejects() {
        for q in [0, -1, -1_000_000] {
            let err = check_positive_quantity(q).unwrap_err();
            assert_eq!(err.rule, "usage.record.positive_quantity");
            assert_eq!(err.context["quantity"], q);
        }
    }

    // ── check_valid_event_type ──────────────────────────────────────────────

    #[test]
    fn valid_event_type_passes() {
        for t in ["data", "voice", "voice_minutes", "sms"] {
            check_valid_event_type(t).unwrap();
        }
    }

    #[test]
    fn valid_event_type_rejects() {
        for t in ["video", "", "DATA", "unknown"] {
            let err = check_valid_event_type(t).unwrap_err();
            assert_eq!(err.rule, "usage.record.valid_event_type");
        }
    }

    #[test]
    fn valid_types_context_is_sorted() {
        let err = check_valid_event_type("nope").unwrap_err();
        assert_eq!(
            err.context["valid_types"],
            json!(["data", "sms", "voice", "voice_minutes"])
        );
    }

    // ── subscription_not_found ──────────────────────────────────────────────

    #[test]
    fn subscription_not_found_rule() {
        let v = subscription_not_found("90000042");
        assert_eq!(v.rule, "usage.record.subscription_must_exist");
        assert_eq!(v.context["msisdn"], "90000042");
    }

    // ── check_msisdn_matches ────────────────────────────────────────────────

    #[test]
    fn msisdn_matches_passes() {
        let sub = json!({ "id": "SUB-0001", "msisdn": "90000042" });
        check_msisdn_matches(&sub, "90000042").unwrap();
    }

    #[test]
    fn msisdn_matches_rejects_mismatch() {
        let sub = json!({ "id": "SUB-0001", "msisdn": "90000043" });
        let err = check_msisdn_matches(&sub, "90000042").unwrap_err();
        assert_eq!(err.rule, "usage.record.msisdn_belongs_to_subscription");
    }

    // ── check_subscription_active ───────────────────────────────────────────

    #[test]
    fn subscription_active_passes() {
        let sub = json!({ "id": "SUB-0001", "state": "active" });
        check_subscription_active(&sub).unwrap();
    }

    #[test]
    fn subscription_active_rejects_non_active() {
        for state in ["blocked", "terminated", "suspended", "pending"] {
            let sub = json!({ "id": "SUB-0001", "state": state });
            let err = check_subscription_active(&sub).unwrap_err();
            assert_eq!(err.rule, "usage.record.subscription_must_be_active");
            assert_eq!(err.context["state"], state);
        }
    }

    // ── payload builders — port test_blocked_rejection.py's assertions ──────

    fn sample_event() -> UsageEvent {
        UsageEvent {
            id: "UE-000001".into(),
            msisdn: "90000042".into(),
            subscription_id: Some("SUB-0001".into()),
            event_type: "data".into(),
            event_time: "2026-07-12T05:00:00+00:00".parse().unwrap(),
            quantity: 100,
            unit: "mb".into(),
            source: Some("test".into()),
            raw_cdr_ref: Some("CDR-OK-1".into()),
            roaming_indicator: false,
        }
    }

    #[test]
    fn usage_recorded_payload_shape() {
        let p = usage_recorded_payload(&sample_event(), Some("PLAN_M"));
        assert_eq!(p["msisdn"], "90000042");
        assert_eq!(p["subscriptionId"], "SUB-0001");
        assert_eq!(p["eventType"], "data");
        assert_eq!(p["quantity"], 100);
        assert_eq!(p["offeringId"], "PLAN_M");
        assert_eq!(p["roamingIndicator"], false);
        assert_eq!(p["usageEventId"], "UE-000001");
    }

    #[test]
    fn rejection_payload_shape() {
        let evt = sample_event();
        let p = rejection_payload(
            &evt.msisdn,
            Some("SUB-0042"),
            Some("blocked"),
            &evt.event_type,
            evt.event_time,
            evt.quantity,
            &evt.unit,
            evt.source.as_deref(),
            Some("CDR-BLOCKED-1"),
            "usage.record.subscription_must_be_active",
        );
        assert_eq!(p["reason"], "usage.record.subscription_must_be_active");
        assert_eq!(p["state"], "blocked");
        assert_eq!(p["subscriptionId"], "SUB-0042");
        assert_eq!(p["rawCdrRef"], "CDR-BLOCKED-1");
    }

    #[test]
    fn roaming_indicator_passthrough_not_a_type() {
        // Guard 11: a roaming data event is still `event_type = "data"`.
        check_valid_event_type("data").unwrap();
        let mut evt = sample_event();
        evt.roaming_indicator = true;
        let p = usage_recorded_payload(&evt, Some("PLAN_M"));
        assert_eq!(p["eventType"], "data");
        assert_eq!(p["roamingIndicator"], true);
    }
}
