//! The table-shaped renderers: ticket, prov, inventory, case, port_request.
//! Ports of the same-named modules under `bss_cockpit.renderers`.
//!
//! Grouped in one module because they share a shape (header rule → column
//! header → dash rule → rows) and no state. Each is byte-golden-tested against
//! the oracle in `tests/renderer_golden.rs`.

use serde_json::Value;

use super::boxes::{r#box, BOX_WIDTH};
use super::fmt::{ljust, py_or, py_repr_str, rjust, scalar_str, truncate};

/// `payload.get(k)` as a string with `default` when missing/null (but **not**
/// when falsy — Python's `.get(k, default)` only defaults on absence).
fn get_or(v: &Value, key: &str, default: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) => default.to_string(),
        Some(x) => scalar_str(x),
    }
}

// ── ticket ───────────────────────────────────────────────────────────

/// Render a single ticket view (header + fields). Port of `renderers.ticket`.
pub fn render_ticket(ticket: &Value) -> String {
    let tid = get_or(ticket, "id", "TKT-???");
    let ttype = get_or(ticket, "ticketType", "?");
    let state = get_or(ticket, "state", "?");
    let priority = get_or(ticket, "priority", "?");
    let subject = get_or(ticket, "subject", "");
    let agent = py_or(ticket, &["assignedAgent"], "—");
    // The LAST related entity of type `case` wins (Python's loop doesn't break).
    let mut case: Option<String> = None;
    if let Some(rels) = ticket.get("relatedEntity").and_then(Value::as_array) {
        for r in rels {
            if r.get("entityType").and_then(Value::as_str) == Some("case") {
                case = r.get("id").map(scalar_str);
            }
        }
    }
    let case = case
        .filter(|c| !c.is_empty())
        .unwrap_or_else(|| "—".to_string());
    let lines = vec![
        format!("Subject:   {subject}"),
        format!("Type:      {ttype}"),
        format!("State:     {state}"),
        format!("Priority:  {priority}"),
        format!("Assigned:  {agent}"),
        format!("Case:      {case}"),
    ];
    r#box(&lines, &tid, 64)
}

// ── prov ─────────────────────────────────────────────────────────────

/// Render a provisioning task list as an aligned text table. Port of
/// `renderers.prov`.
pub fn render_prov_tasks(tasks: &[Value]) -> String {
    if tasks.is_empty() {
        return "(no tasks)".to_string();
    }
    let header = format!(
        "{} {} {} {} ATTEMPTS",
        ljust("ID", 8),
        ljust("SERVICE", 10),
        ljust("TASK_TYPE", 22),
        ljust("STATE", 12)
    );
    // Python: `"-" * len(header)` — char-wise, and the header is pure ASCII.
    let rule = "-".repeat(header.chars().count());
    let mut lines = vec![header, rule];
    for t in tasks {
        lines.push(format!(
            "{} {} {} {} {}/{}",
            ljust(&get_or(t, "id", ""), 8),
            ljust(&get_or(t, "serviceId", ""), 10),
            ljust(&get_or(t, "taskType", ""), 22),
            ljust(&get_or(t, "state", ""), 12),
            get_or(t, "attempts", "0"),
            get_or(t, "maxAttempts", "0"),
        ));
    }
    lines.join("\n")
}

// ── inventory ────────────────────────────────────────────────────────

/// Port of `renderers.inventory.render_msisdn_list`.
pub fn render_msisdn_list(payload: &[Value]) -> String {
    if payload.is_empty() {
        return "(no MSISDNs match)".to_string();
    }
    let mut rows = vec![
        format!("── MSISDNs {}", "─".repeat(50)),
        String::new(),
        format!(
            "  {}  {}  {}  Reserved at",
            ljust("MSISDN", 14),
            ljust("Status", 12),
            ljust("Subscription", 14)
        ),
        format!(
            "  {}  {}  {}  {}",
            "─".repeat(14),
            "─".repeat(12),
            "─".repeat(14),
            "─".repeat(19)
        ),
    ];
    for m in payload.iter().take(50) {
        let reserved = truncate(&py_or(m, &["reserved_at", "reservedAt"], ""), 19);
        rows.push(format!(
            "  {}  {}  {}  {reserved}",
            ljust(&get_or(m, "msisdn", "?"), 14),
            ljust(&get_or(m, "status", "?"), 12),
            ljust(
                &py_or(
                    m,
                    &["assigned_to_subscription_id", "assignedToSubscriptionId"],
                    "—"
                ),
                14
            ),
        ));
    }
    if payload.len() > 50 {
        rows.push(format!("  (+ {} more)", payload.len() - 50));
    }
    rows.push(String::new());
    rows.push(format!(
        "  ({} rows shown — call `inventory.msisdn.count` for the full pool \
         total, or pass a higher `limit` to widen.)",
        payload.len()
    ));
    rows.join("\n")
}

/// Port of `renderers.inventory.render_msisdn_count`.
pub fn render_msisdn_count(payload: &Value) -> String {
    let pfx = py_or(payload, &["prefix"], "");
    let title = if pfx.is_empty() {
        "── MSISDN pool ".to_string()
    } else {
        format!("── MSISDN pool — prefix={pfx} ")
    };
    let fill = 60usize.saturating_sub(title.chars().count());
    let mut rows = vec![format!("{title}{}", "─".repeat(fill)), String::new()];
    for key in ["available", "reserved", "assigned", "ported_out"] {
        rows.push(format!(
            "  {}  {}",
            ljust(key, 12),
            rjust(&get_or(payload, key, "0"), 6)
        ));
    }
    rows.push(format!("  {}  {}", "─".repeat(12), "─".repeat(6)));
    rows.push(format!(
        "  {}  {}",
        ljust("total", 12),
        rjust(&get_or(payload, "total", "0"), 6)
    ));
    rows.join("\n")
}

// ── case ─────────────────────────────────────────────────────────────

/// Render a case with its child tickets and notes. Port of `renderers.case`.
pub fn render_case(case: &Value, tickets: &[Value], notes: &[Value]) -> String {
    let cid = get_or(case, "id", "CASE-???");
    let subject = get_or(case, "subject", "");
    let state = get_or(case, "state", "?");
    let priority = get_or(case, "priority", "?");
    let cust_id = get_or(case, "customerId", "—");
    let opened_at = get_or(case, "createdAt", "—");
    let opened_by = py_or(case, &["openedBy", "createdBy"], "—");

    let mut lines = vec![
        format!("Customer: {cust_id}"),
        format!("Opened:   {opened_at} by {opened_by}"),
        String::new(),
        format!("── Tickets ({}) {}", tickets.len(), "─".repeat(38)),
    ];
    if tickets.is_empty() {
        lines.push("(none)".to_string());
    }
    for t in tickets {
        // Python: `t.get("assignedAgent") or t.get("agentId", "—")` — note the
        // default rides on the SECOND get, so an explicitly-null agentId yields
        // "None" in Python. Absence yields the dash.
        let agent = match t.get("assignedAgent").map(scalar_str) {
            Some(a) if !a.is_empty() => a,
            _ => get_or(t, "agentId", "—"),
        };
        lines.push(format!(
            "{} {} {} {} {agent}",
            ljust(&get_or(t, "id", ""), 8),
            ljust(&get_or(t, "ticketType", ""), 18),
            ljust(&get_or(t, "state", ""), 14),
            ljust(&get_or(t, "priority", ""), 6),
        ));
    }
    lines.push(String::new());
    lines.push(format!("── Notes ({}) {}", notes.len(), "─".repeat(40)));
    if notes.is_empty() {
        lines.push("(none)".to_string());
    }
    for n in notes {
        lines.push(format!(
            "[{} {}] {}",
            get_or(n, "authorId", "—"),
            truncate(&get_or(n, "createdAt", ""), 16),
            truncate(&get_or(n, "body", ""), 60),
        ));
    }
    // `{subject!r:<40}` — a Python repr (single quotes), left-justified to 40.
    let title = format!(
        "{cid}  {}  [{state}]  {priority}",
        ljust(&py_repr_str(&subject), 40)
    );
    r#box(&lines, &title, 72)
}

// ── port_request ─────────────────────────────────────────────────────

/// Port of `renderers.port_request.render_port_request_list`.
pub fn render_port_request_list(payload: &[Value]) -> String {
    if payload.is_empty() {
        return "(no port requests)".to_string();
    }
    let mut rows = vec![
        format!("── Port Requests {}", "─".repeat(50)),
        String::new(),
        format!(
            "  {}  {}  {}  {}  {}  Requested",
            ljust("ID", 14),
            ljust("Direction", 10),
            ljust("Donor MSISDN", 14),
            ljust("Carrier", 16),
            ljust("State", 10)
        ),
        format!(
            "  {}  {}  {}  {}  {}  {}",
            "─".repeat(14),
            "─".repeat(10),
            "─".repeat(14),
            "─".repeat(16),
            "─".repeat(10),
            "─".repeat(10)
        ),
    ];
    for p in payload.iter().take(50) {
        rows.push(format!(
            "  {}  {}  {}  {}  {}  {}",
            ljust(&get_or(p, "id", "?"), 14),
            ljust(&get_or(p, "direction", "?"), 10),
            ljust(&py_or(p, &["donorMsisdn", "donor_msisdn"], "—"), 14),
            ljust(
                &truncate(&py_or(p, &["donorCarrier", "donor_carrier"], "—"), 16),
                16
            ),
            ljust(&get_or(p, "state", "?"), 10),
            truncate(
                &py_or(p, &["requestedPortDate", "requested_port_date"], ""),
                10
            ),
        ));
    }
    if payload.len() > 50 {
        rows.push(format!("  (+ {} more)", payload.len() - 50));
    }
    rows.push(String::new());
    rows.push(format!(
        "  ({} rows shown — pass `limit` to widen.)",
        payload.len()
    ));
    rows.join("\n")
}

/// Port of `renderers.port_request.render_port_request_get`.
pub fn render_port_request_get(payload: &Value) -> String {
    let title = format!("── Port Request {} ", get_or(payload, "id", "?"));
    let fill = 60usize.saturating_sub(title.chars().count());
    let mut rows = vec![format!("{title}{}", "─".repeat(fill)), String::new()];
    rows.push(format!(
        "  Direction       : {}",
        get_or(payload, "direction", "?")
    ));
    rows.push(format!(
        "  Donor MSISDN    : {}",
        py_or(payload, &["donorMsisdn", "donor_msisdn"], "—")
    ));
    rows.push(format!(
        "  Donor carrier   : {}",
        py_or(payload, &["donorCarrier", "donor_carrier"], "—")
    ));
    rows.push(format!(
        "  Target sub      : {}",
        py_or(
            payload,
            &["targetSubscriptionId", "target_subscription_id"],
            "—"
        )
    ));
    rows.push(format!(
        "  Requested date  : {}",
        py_or(payload, &["requestedPortDate", "requested_port_date"], "—")
    ));
    rows.push(format!(
        "  State           : {}",
        get_or(payload, "state", "?")
    ));
    let rejection = py_or(payload, &["rejectionReason", "rejection_reason"], "");
    if !rejection.is_empty() {
        rows.push(format!("  Rejection reason: {rejection}"));
    }
    rows.push(format!(
        "  Created         : {}",
        truncate(&py_or(payload, &["createdAt", "created_at"], ""), 19)
    ));
    rows.push(format!(
        "  Updated         : {}",
        truncate(&py_or(payload, &["updatedAt", "updated_at"], ""), 19)
    ));
    rows.join("\n")
}

/// Re-exported for the dispatcher's default box width.
pub const DEFAULT_BOX_WIDTH: usize = BOX_WIDTH;
