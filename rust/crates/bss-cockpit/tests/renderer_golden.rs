//! Renderer goldens — byte-for-byte against the Python oracle.
//!
//! The fixtures in `tests/golden/*.json` were captured by running the *live*
//! Python renderers (`scratchpad/capture.py`). The ASCII output is the cockpit's
//! visualization language (motto #4) and is fed to the LLM as well as the
//! operator, so a single shifted column is a real regression — hence byte
//! equality, not a fuzzy match.
//!
//! Pure (no clock, no DB, no HTTP): `now` is pinned so `days_to` is deterministic.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use chrono::{TimeZone, Utc};
use serde_json::Value;

use bss_cockpit::renderers::subscription::{render_subscription, SubscriptionCtx};
use bss_cockpit::renderers::tables::{
    render_case, render_msisdn_count, render_msisdn_list, render_port_request_get,
    render_port_request_list, render_prov_tasks, render_ticket,
};

fn golden(name: &str) -> Value {
    let raw = std::fs::read_to_string(format!(
        "{}/tests/golden/{name}.json",
        env!("CARGO_MANIFEST_DIR")
    ))
    .expect("golden fixture is present");
    serde_json::from_str(&raw).expect("golden fixture parses")
}

/// Show the first differing line — a 78-column diff is unreadable otherwise.
fn assert_ascii_eq(got: &str, want: &str, case: &str) {
    if got == want {
        return;
    }
    let g: Vec<&str> = got.lines().collect();
    let w: Vec<&str> = want.lines().collect();
    for (i, (gl, wl)) in g.iter().zip(w.iter()).enumerate() {
        assert_eq!(
            gl, wl,
            "\ncase {case}: first divergence at line {i}\n  rust  : {gl:?}\n  oracle: {wl:?}\n"
        );
    }
    assert_eq!(
        g.len(),
        w.len(),
        "case {case}: line count differs\n--- rust ---\n{got}\n--- oracle ---\n{want}"
    );
    assert_eq!(got, want, "case {case}");
}

#[test]
fn subscription_renders_byte_identical_to_the_oracle() {
    let want = golden("subscription");
    let now = Utc.with_ymd_and_hms(2026, 7, 15, 12, 0, 0).unwrap();

    let case = |sub: Value, ctx: SubscriptionCtx<'_>, name: &str| {
        let got = render_subscription(&sub, &ctx);
        assert_ascii_eq(&got, want[name].as_str().unwrap(), name);
    };

    // ── active, both balance shapes column-aligned ──────────────────────────
    case(
        serde_json::json!({
            "id": "SUB-001", "customerId": "CUST-001", "msisdn": "91234567",
            "offeringId": "PLAN_M", "state": "active",
            "activatedAt": "2026-01-01T00:00:00+00:00",
            "nextRenewalAt": "2026-08-01T00:00:00+00:00",
            "balances": [
                {"type": "data", "used": 5120, "total": 10240, "unit": "mb"},
                {"type": "voice", "used": 30, "total": 200, "unit": "min"},
            ],
        }),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "active_basic",
    );

    // ── blocked → the DOUBLE frame (v0.6: weight before the label) ──────────
    case(
        serde_json::json!({
            "id": "SUB-002", "customerId": "CUST-002", "msisdn": "98765432",
            "offeringId": "PLAN_S", "state": "blocked",
            "balances": [{"type": "data", "used": 1024, "total": 1024, "unit": "mb"}],
        }),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "blocked_double_box",
    );

    // ── the LIVE payload shape: allowanceType / consumed / remaining / -1 ───
    // Also pins `str.title()` on an underscored label → "Data_Roaming".
    case(
        serde_json::json!({
            "id": "SUB-003", "customerId": "CUST-003", "msisdn": "90001111",
            "offeringId": "PLAN_L", "state": "active",
            "balances": [
                {"allowanceType": "data_roaming", "consumed": 256, "total": 1024, "unit": "mb"},
                {"allowanceType": "sms", "remaining": 40, "total": 100, "unit": "sms"},
                {"allowanceType": "voice_minutes", "total": -1, "unit": "min"},
            ],
        }),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "live_payload_shape",
    );

    case(
        serde_json::json!({"id": "SUB-004", "state": "pending"}),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "no_balances",
    );

    // ── full context: customer + offering price + VAS history + eSIM ────────
    let customer = serde_json::json!({"name": "Ada Lovelace"});
    let offering = serde_json::json!({"name": "Medium", "price": 22});
    let esim = serde_json::json!({
        "iccid": "8965000000000000001",
        "imsi": "525001234567890",
        "activationCode": "LPA:1$smdp.example.com$ABC-123-XYZ",
    });
    case(
        serde_json::json!({
            "id": "SUB-005", "customerId": "CUST-005", "msisdn": "91112222",
            "offeringId": "PLAN_M", "state": "active",
            "nextRenewalAt": "2026-07-20T00:00:00+00:00",
            "balances": [{"type": "data", "used": 333, "total": 1000, "unit": "mb"}],
            "vasHistory": [
                {"purchasedAt": "2026-07-01T10:00:00Z", "vasOfferingId": "VAS_DATA_1GB", "amount": 6},
                {"purchasedAt": "2026-06-01T10:00:00Z", "vasOfferingId": "VAS_DATA_5GB", "amount": 20},
            ],
        }),
        SubscriptionCtx {
            customer: Some(&customer),
            offering: Some(&offering),
            esim: Some(&esim),
            now: Some(now),
        },
        "with_ctx_and_esim",
    );

    // ── 5/200 = 2.5% exactly → banker's rounding gives 2, not 3 ─────────────
    case(
        serde_json::json!({
            "id": "SUB-006", "state": "active",
            "balances": [{"type": "data", "used": 5, "total": 200, "unit": "mb"}],
        }),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "bankers_rounding_edge",
    );

    // ── an unparseable date passes through raw rather than erroring ─────────
    case(
        serde_json::json!({"id": "SUB-007", "state": "active", "nextRenewalAt": "not-a-date"}),
        SubscriptionCtx {
            now: Some(now),
            ..Default::default()
        },
        "unparseable_renewal",
    );
}

#[test]
fn table_renderers_render_byte_identical_to_the_oracle() {
    let want = golden("tables");
    let chk = |got: String, name: &str| assert_ascii_eq(&got, want[name].as_str().unwrap(), name);
    let arr = |v: Value| v.as_array().cloned().unwrap_or_default();

    // ── ticket: the case id comes off relatedEntity; empties hit the defaults ──
    chk(
        render_ticket(&serde_json::json!({
            "id": "TKT-101", "ticketType": "fault", "state": "open", "priority": "P2",
            "subject": "No data", "assignedAgent": "AG-1",
            "relatedEntity": [{"entityType": "case", "id": "CASE-042"}],
        })),
        "ticket_basic",
    );
    chk(render_ticket(&serde_json::json!({})), "ticket_empty");

    // ── prov ────────────────────────────────────────────────────────────────
    chk(render_prov_tasks(&[]), "prov_empty");
    chk(
        render_prov_tasks(&arr(serde_json::json!([
            {"id": "PTK-1", "serviceId": "SVC-1", "taskType": "hlr_create_subscriber",
             "state": "completed", "attempts": 1, "maxAttempts": 3},
            {"id": "PTK-2", "serviceId": "SVC-1", "taskType": "esim_download",
             "state": "stuck", "attempts": 3, "maxAttempts": 3},
        ]))),
        "prov_tasks",
    );

    // ── inventory: both key families for the reserved/assigned columns ───────
    chk(render_msisdn_list(&[]), "msisdn_empty");
    chk(
        render_msisdn_list(&arr(serde_json::json!([
            {"msisdn": "91234567", "status": "available"},
            {"msisdn": "91234568", "status": "assigned",
             "assigned_to_subscription_id": "SUB-001",
             "reserved_at": "2026-07-01T10:00:00+00:00"},
        ]))),
        "msisdn_list",
    );
    chk(
        render_msisdn_count(&serde_json::json!({
            "available": 940, "reserved": 5, "assigned": 50, "ported_out": 5, "total": 1000
        })),
        "msisdn_count",
    );
    // The prefix widens the title, shortening the rule.
    chk(
        render_msisdn_count(&serde_json::json!({"prefix": "9123", "available": 10, "total": 10})),
        "msisdn_count_prefix",
    );

    // ── case ────────────────────────────────────────────────────────────────
    chk(
        render_case(
            &serde_json::json!({
                "id": "CASE-042", "subject": "Billing dispute", "state": "open",
                "priority": "P1", "customerId": "CUST-001",
                "createdAt": "2026-07-01T10:00:00+00:00", "openedBy": "agent-1",
            }),
            &arr(serde_json::json!([
                {"id": "TKT-101", "ticketType": "billing", "state": "open",
                 "priority": "P1", "assignedAgent": "AG-1"},
                {"id": "TKT-102", "ticketType": "fault", "state": "resolved", "priority": "P3"},
            ])),
            &arr(serde_json::json!([
                {"authorId": "agent-1", "createdAt": "2026-07-01T11:00:00+00:00",
                 "body": "Called customer, awaiting docs."},
            ])),
        ),
        "case_full",
    );
    chk(
        render_case(
            &serde_json::json!({"id": "CASE-043", "subject": "", "state": "open", "priority": "P3"}),
            &[],
            &[],
        ),
        "case_empty",
    );
    // `{subject!r}` — an apostrophe flips the repr to DOUBLE quotes.
    chk(
        render_case(
            &serde_json::json!({"id": "CASE-044", "subject": "Customer's line dead",
                                "state": "open", "priority": "P1"}),
            &[],
            &[],
        ),
        "case_apostrophe",
    );

    // ── port_request ────────────────────────────────────────────────────────
    chk(render_port_request_list(&[]), "pr_empty");
    chk(
        render_port_request_list(&arr(serde_json::json!([
            {"id": "PR-1", "direction": "in", "donorMsisdn": "91234567",
             "donorCarrier": "SuperLongCarrierNameHere", "state": "requested",
             "requestedPortDate": "2026-08-01"},
            {"id": "PR-2", "direction": "out", "donor_msisdn": "98765432",
             "donor_carrier": "M1", "state": "completed"},
        ]))),
        "pr_list",
    );
    chk(
        render_port_request_get(&serde_json::json!({
            "id": "PR-1", "direction": "in", "donorMsisdn": "91234567",
            "donorCarrier": "Singtel", "targetSubscriptionId": "SUB-001",
            "requestedPortDate": "2026-08-01", "state": "rejected",
            "rejectionReason": "NRIC mismatch",
            "createdAt": "2026-07-01T10:00:00+00:00", "updatedAt": "2026-07-02T10:00:00+00:00",
        })),
        "pr_get",
    );
    // No rejection reason → the row is omitted entirely.
    chk(
        render_port_request_get(&serde_json::json!({"id": "PR-9"})),
        "pr_get_min",
    );
}
