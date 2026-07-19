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

#[test]
fn customer_360_renders_byte_identical_to_the_oracle() {
    use bss_cockpit::renderers::customer::{render_customer_360, Customer360Ctx};
    use std::collections::HashMap;

    let want = golden("customer");
    let chk = |got: String, name: &str| assert_ascii_eq(&got, want[name].as_str().unwrap(), name);
    let arr = |v: Value| v.as_array().cloned().unwrap_or_default();

    chk(
        render_customer_360(
            &serde_json::json!({"id": "CUST-001", "name": "Ada Lovelace"}),
            &Customer360Ctx::default(),
        ),
        "min",
    );

    // ── the works: kyc badge, both contact shapes, blocked marker, bundle %,
    //    a case with a child ticket, a collapsed closed case, interactions ────
    let subs = arr(serde_json::json!([
        {"id": "SUB-001", "offeringId": "PLAN_M", "state": "active", "msisdn": "91234567",
         "balances": [{"type": "data", "used": 5127, "total": 10240}]},
        {"id": "SUB-002", "offeringId": "PLAN_S", "state": "blocked", "msisdn": "98765432"},
    ]));
    let cases = arr(serde_json::json!([
        {"id": "CASE-1", "subject": "Billing dispute over roaming charges last month",
         "state": "open", "priority": "P1"},
        {"id": "CASE-2", "subject": "Closed one", "state": "closed", "priority": "P3"},
    ]));
    let interactions = arr(serde_json::json!([
        {"createdAt": "2026-07-01T10:00:00+00:00", "channel": "portal-chat",
         "summary": "Asked about balance"},
        {"createdAt": "2026-07-02T10:00:00+00:00", "channel": "cockpit", "action": "Agent note"},
    ]));
    let mut tickets_by_case = HashMap::new();
    tickets_by_case.insert(
        "CASE-1".to_string(),
        arr(serde_json::json!([
            {"id": "TKT-1", "ticketType": "billing", "priority": "P1", "state": "open"}
        ])),
    );
    chk(
        render_customer_360(
            &serde_json::json!({
                "id": "CUST-002", "name": "Grace Hopper", "status": "active",
                "createdAt": "2026-01-15T09:00:00+00:00", "kycStatus": "verified",
                "contactMedium": [
                    {"mediumType": "email", "characteristic": {"emailAddress": "g@navy.mil"}},
                    {"mediumType": "mobile", "value": "91234567"},
                ],
            }),
            &Customer360Ctx {
                subscriptions: &subs,
                cases: &cases,
                tickets_by_case,
                interactions: &interactions,
                interactions_limit: None,
            },
        ),
        "full",
    );

    // ── the kyc badge's three branches ──────────────────────────────────────
    chk(
        render_customer_360(
            &serde_json::json!({"id": "CUST-003", "name": "X", "kyc_status": "pending"}),
            &Customer360Ctx::default(),
        ),
        "kyc_pending",
    );
    // An unrecognised status renders NO badge at all (not a fallback string).
    chk(
        render_customer_360(
            &serde_json::json!({"id": "CUST-004", "name": "X", "kycStatus": "weird"}),
            &Customer360Ctx::default(),
        ),
        "kyc_unknown",
    );

    // ── > limit interactions → the "(+ N more)" tail ─────────────────────────
    let many: Vec<Value> = (1..8)
        .map(|i| {
            serde_json::json!({
                "createdAt": format!("2026-07-0{i}T10:00:00+00:00"),
                "channel": "c", "summary": format!("s{i}")
            })
        })
        .collect();
    chk(
        render_customer_360(
            &serde_json::json!({"id": "CUST-005", "name": "X"}),
            &Customer360Ctx {
                interactions: &many,
                ..Default::default()
            },
        ),
        "interactions_overflow",
    );
}

#[test]
fn order_tree_renders_byte_identical_to_the_oracle() {
    use bss_cockpit::renderers::order::{render_order, OrderCtx};
    use std::collections::HashMap;

    let want = golden("order");
    let chk = |got: String, name: &str| assert_ascii_eq(&got, want[name].as_str().unwrap(), name);
    let arr = |v: Value| v.as_array().cloned().unwrap_or_default();

    // Header + summary, no decomposition.
    chk(
        render_order(
            &serde_json::json!({
                "id": "ORD-014", "state": "completed", "customerId": "CUST-001",
                "items": [{"offeringId": "PLAN_M"}],
                "orderDate": "2026-07-01T10:00:00+00:00",
                "completedDate": "2026-07-01T10:00:04+00:00",
            }),
            &OrderCtx::default(),
        ),
        "bare",
    );

    // ── the full SOM tree: CFS → 2 RFS → tasks, a ⚠ failed task with an
    //    attempts suffix, a task duration, and the → subscription tail ────────
    let sos = arr(serde_json::json!([{"id": "SO-022", "state": "completed"}]));
    let mut services_by_so = HashMap::new();
    services_by_so.insert(
        "SO-022".to_string(),
        arr(serde_json::json!([
            {"id": "SVC-101", "serviceType": "CFS", "name": "MobileBroadband", "state": "completed"},
            {"id": "SVC-102", "serviceType": "RFS", "name": "Data", "state": "completed"},
            {"id": "SVC-103", "serviceType": "RFS", "name": "Voice", "state": "completed"},
        ])),
    );
    let mut tasks_by_service = HashMap::new();
    tasks_by_service.insert(
        "SVC-102".to_string(),
        arr(serde_json::json!([
            {"id": "PTK-001", "taskType": "hlr.activate", "state": "completed",
             "startedAt": "2026-07-01T10:00:00+00:00",
             "completedAt": "2026-07-01T10:00:01.300000+00:00"},
            {"id": "PTK-003", "taskType": "ocs.allocate_quota", "state": "failed", "attempts": 2},
        ])),
    );
    tasks_by_service.insert(
        "SVC-103".to_string(),
        arr(serde_json::json!([
            {"id": "PTK-004", "taskType": "hlr.subscribe", "state": "completed"}
        ])),
    );
    chk(
        render_order(
            &serde_json::json!({
                "id": "ORD-014", "state": "completed", "customerId": "CUST-001",
                "items": [{"offeringId": "PLAN_M"}],
                "orderDate": "2026-07-01T10:00:00+00:00",
                "completedDate": "2026-07-01T10:00:04+00:00",
            }),
            &OrderCtx {
                service_orders: &sos,
                services_by_so,
                tasks_by_service,
                subscription_id: Some("SUB-007".to_string()),
            },
        ),
        "full_tree",
    );

    // A service order with nothing attached gets the placeholder row.
    chk(
        render_order(
            &serde_json::json!({"id": "ORD-015", "state": "in_progress"}),
            &OrderCtx {
                service_orders: &arr(serde_json::json!([{"id": "SO-023", "state": "pending"}])),
                ..Default::default()
            },
        ),
        "no_services",
    );

    // Two SOs → the first uses ├─ / │, the last └─ / spaces.
    chk(
        render_order(
            &serde_json::json!({"id": "ORD-016", "state": "in_progress"}),
            &OrderCtx {
                service_orders: &arr(serde_json::json!([
                    {"id": "SO-1", "state": "completed"},
                    {"id": "SO-2", "state": "failed"},
                ])),
                ..Default::default()
            },
        ),
        "two_so",
    );
}

#[test]
fn catalog_renders_byte_identical_to_the_oracle() {
    use bss_cockpit::renderers::catalog::{render_catalog, render_catalog_show, render_vas_list};

    let want = golden("catalog");
    let chk = |got: String, name: &str| assert_ascii_eq(&got, want[name].as_str().unwrap(), name);

    fn plan(
        pid: &str,
        name: &str,
        price: f64,
        data: i64,
        voice: i64,
        sms: i64,
        roam: Option<i64>,
    ) -> Value {
        let mut a = vec![
            serde_json::json!({"allowanceType": "data", "quantity": data, "unit": "mb"}),
            serde_json::json!({"allowanceType": "voice_minutes", "quantity": voice, "unit": "min"}),
            serde_json::json!({"allowanceType": "sms", "quantity": sms, "unit": "sms"}),
        ];
        if let Some(r) = roam {
            a.push(
                serde_json::json!({"allowanceType": "data_roaming", "quantity": r, "unit": "mb"}),
            );
        }
        serde_json::json!({
            "id": pid, "name": name, "bundleAllowance": a,
            "productOfferingPrice": [
                {"price": {"taxIncludedAmount": {"value": price, "unit": "SGD"}}}
            ],
        })
    }

    // Deliberately out of price order — the renderer sorts cheapest-first.
    let plans = vec![
        plan("PLAN_L", "Large", 38.0, 51200, -1, -1, Some(2048)),
        plan("PLAN_S", "Small", 12.0, 5120, 100, 100, Some(0)),
        plan("PLAN_M", "Medium", 22.0, 20480, 500, -1, Some(1024)),
    ];

    chk(render_catalog(&[]), "empty");
    chk(render_catalog(&plans), "three_plans");

    // PLAN_M retired → the ★ falls back to the median by price.
    let no_m: Vec<Value> = plans
        .iter()
        .filter(|p| p["id"] != "PLAN_M")
        .cloned()
        .collect();
    chk(render_catalog(&no_m), "no_plan_m");

    // isSellable=false and lifecycleStatus!=active are filtered out.
    let mut with_junk = plans.clone();
    let mut hidden = plan("PLAN_X", "Hidden", 5.0, 1, 1, 1, None);
    hidden["isSellable"] = Value::Bool(false);
    let mut retired = plan("PLAN_Y", "Retired", 6.0, 1, 1, 1, None);
    retired["lifecycleStatus"] = Value::String("retired".to_string());
    with_junk.push(hidden);
    with_junk.push(retired);
    chk(render_catalog(&with_junk), "filters_unsellable");

    // ── single-plan card ────────────────────────────────────────────────────
    chk(render_catalog_show(&plans[2]), "show_m");
    // PLAN_S has 0 roaming → the row is suppressed... actually 0 mb renders
    // "0 mb" (not "—"), so the row SHOWS. The golden is the authority.
    chk(render_catalog_show(&plans[1]), "show_s_no_roaming");
    chk(
        render_catalog_show(&serde_json::json!({"id": "PLAN_Z"})),
        "show_min",
    );

    // ── VAS table: columns size to content ──────────────────────────────────
    chk(render_vas_list(&[]), "vas_empty");
    chk(
        render_vas_list(&[
            serde_json::json!({"id": "VAS_DATA_1GB", "name": "1GB Top-up", "currency": "SGD",
                               "priceAmount": 6, "allowanceQuantity": 1024,
                               "allowanceUnit": "mb", "expiryHours": 720}),
            // No expiryHours → the dash.
            serde_json::json!({"id": "VAS_DATA_5GB", "name": "5GB Top-up", "currency": "SGD",
                               "priceAmount": 20, "allowanceQuantity": 5120,
                               "allowanceUnit": "mb"}),
            // -1 quantity → "unlimited min".
            serde_json::json!({"id": "VAS_VOICE", "name": "Voice", "currency": "SGD",
                               "priceAmount": 3, "allowanceQuantity": -1,
                               "allowanceUnit": "min", "expiryHours": 24}),
        ]),
        "vas_list",
    );
}

/// The eSIM card, split by what we can honestly claim.
///
/// **Everything outside the QR block is byte-golden.** The QR block itself is a
/// documented parity seam: python-qrcode and Rust's `qrcode` crate encode the same
/// LPA payload into different matrices (different mode segmentation AND a
/// different mask), so the dark cells differ. Both are valid QR codes scanning to
/// the identical LPA string, and both pick the same *version*, so the block's
/// dimensions — and therefore the card's shape — are unchanged.
///
/// So: assert byte equality on every non-QR line, and assert the QR block's
/// FUNCTIONAL contract (same line count, same width, only block glyphs). No
/// assertion here claims parity the port does not have.
#[test]
fn esim_card_matches_the_oracle_outside_the_qr_block() {
    use bss_cockpit::renderers::esim::render_esim_activation;

    let want = golden("esim");
    let activation = serde_json::json!({
        "iccid": "8965000000000000001", "imsi": "525001234567890",
        "msisdn": "91234567", "activationCode": "LPA:1$smdp.example.com$ABC-123-XYZ",
        "status": "prepared",
    });

    // A QR line is one whose content is only block glyphs / spaces / frame.
    fn is_qr_line(line: &str) -> bool {
        let body: String = line.chars().filter(|c| !matches!(c, '│' | ' ')).collect();
        !body.is_empty() && body.chars().all(|c| matches!(c, '█' | '▀' | '▄'))
    }

    let check = |got: &str, name: &str| {
        let want_s = want[name].as_str().unwrap();
        let g: Vec<&str> = got.lines().collect();
        let w: Vec<&str> = want_s.lines().collect();
        assert_eq!(
            g.len(),
            w.len(),
            "case {name}: card line count must match the oracle\n--- rust ---\n{got}\n--- oracle ---\n{want_s}"
        );
        for (i, (gl, wl)) in g.iter().zip(w.iter()).enumerate() {
            if is_qr_line(wl) {
                // Functional contract only — the dark cells legitimately differ.
                assert!(
                    is_qr_line(gl),
                    "case {name} line {i}: expected a QR row, got {gl:?}"
                );
                assert_eq!(
                    gl.chars().count(),
                    wl.chars().count(),
                    "case {name} line {i}: QR row width must match (same QR version)"
                );
            } else {
                assert_eq!(
                    gl, wl,
                    "\ncase {name}: non-QR line {i} must be byte-identical\n  rust  : {gl:?}\n  oracle: {wl:?}\n"
                );
            }
        }
    };

    // Redaction is the security-relevant bit: never past last-4 by default.
    check(&render_esim_activation(&activation, false), "redacted");
    check(&render_esim_activation(&activation, true), "show_full");

    let mut activated = activation.clone();
    activated["status"] = Value::String("activated".to_string());
    check(&render_esim_activation(&activated, false), "activated");

    // An unrecognised status falls back to `● <UPPER>`.
    let mut weird = activation.clone();
    weird["status"] = Value::String("weird".to_string());
    check(&render_esim_activation(&weird, false), "odd_status");

    // A bare (non-"LPA:") code gets the `LPA:1$` prefix.
    let mut bare = activation.clone();
    bare["activationCode"] = Value::String("smdp.x$Y".to_string());
    check(&render_esim_activation(&bare, false), "bare_code");

    // Empty payload → the placeholder QR + dashes, not a panic.
    check(
        &render_esim_activation(&serde_json::json!({}), false),
        "empty",
    );
}

#[test]
fn trace_swimlane_renders_byte_identical_to_the_oracle() {
    use bss_cockpit::renderers::trace::{render_swimlane, SwimlaneOpts};

    let want = golden("trace");
    let chk = |got: String, name: &str| assert_ascii_eq(&got, want[name].as_str().unwrap(), name);

    let trace = serde_json::json!({
        "traceID": "abcdef0123456789aaaa",
        "processes": {
            "p1": {"serviceName": "com"},
            "p2": {"serviceName": "som"},
            "p3": {"serviceName": "postgres"},
        },
        "spans": [
            // A manual span (gets the *), carrying a v0.9 identity tag.
            {"spanID": "s1", "processID": "p1",
             "operationName": "com.order.complete_to_subscription",
             "startTime": 1000, "duration": 4000,
             "tags": [{"key": "bss.service.identity", "value": "operator_cockpit"}]},
            {"spanID": "s2", "processID": "p2", "operationName": "som.decompose",
             "startTime": 1500, "duration": 2000,
             "references": [{"refType": "CHILD_OF", "spanID": "s1"}], "tags": []},
            // A SQL span — hidden unless show_sql.
            {"spanID": "s3", "processID": "p3", "operationName": "SELECT service",
             "startTime": 1600, "duration": 200,
             "references": [{"refType": "CHILD_OF", "spanID": "s2"}], "tags": []},
            // An error span — the whole line wraps in red ANSI.
            {"spanID": "s4", "processID": "p2", "operationName": "boom",
             "startTime": 3000, "duration": 500,
             "references": [{"refType": "CHILD_OF", "spanID": "s1"}],
             "tags": [{"key": "error", "value": true}]},
        ],
    });

    // Default: SQL hidden (with the "N SQL spans hidden" tail), depth indent,
    // the identity column present because one span carries the tag.
    chk(
        render_swimlane(
            &trace,
            &SwimlaneOpts {
                width: Some(120),
                ..Default::default()
            },
        ),
        "default",
    );
    chk(
        render_swimlane(
            &trace,
            &SwimlaneOpts {
                width: Some(120),
                show_sql: true,
                ..Default::default()
            },
        ),
        "show_sql",
    );
    chk(
        render_swimlane(
            &trace,
            &SwimlaneOpts {
                width: Some(120),
                only_service: Some("som"),
                ..Default::default()
            },
        ),
        "only_som",
    );
    chk(
        render_swimlane(&serde_json::json!({}), &SwimlaneOpts::default()),
        "empty",
    );

    // No span carries an identity tag → the whole column is hidden, so pre-v0.9
    // traces stay clean.
    let mut no_ident = trace.clone();
    for s in no_ident["spans"].as_array_mut().unwrap() {
        let kept: Vec<Value> = s["tags"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|t| t.get("key").and_then(Value::as_str) != Some("bss.service.identity"))
            .cloned()
            .collect();
        s["tags"] = Value::Array(kept);
    }
    chk(
        render_swimlane(
            &no_ident,
            &SwimlaneOpts {
                width: Some(120),
                ..Default::default()
            },
        ),
        "no_ident",
    );
}
