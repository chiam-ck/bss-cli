//! Order renderer — order header + SOM decomposition tree. Port of
//! `bss_cockpit.renderers.order`.

use std::collections::HashMap;

use chrono::DateTime;
use serde_json::Value;

use super::boxes::r#box;
use super::fmt::{rjust, scalar_str};

const FRAME_WIDTH: usize = 86;
/// State-column width — every node line right-aligns its state in this many
/// characters so the column lines up regardless of tree depth.
const STATE_COL: usize = 14;
/// Width budget for the title text inside the tree (label + id) before the
/// state column.
const TREE_LABEL_WIDTH: usize = 40;

const STUCK_OR_FAILED: &[&str] = &["failed", "stuck", "errored", "canceled", "cancelled"];

fn is_stuck_or_failed(state: &str) -> bool {
    STUCK_OR_FAILED.contains(&state.to_lowercase().as_str())
}

fn get_or(v: &Value, key: &str, default: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) => default.to_string(),
        Some(x) => scalar_str(x),
    }
}

/// `⚠ ` prefix for failed/stuck states, two spaces otherwise.
fn state_marker(state: &str) -> &'static str {
    if is_stuck_or_failed(state) {
        "⚠ "
    } else {
        "  "
    }
}

/// Right-aligned state column with optional warning marker.
/// Python: `f"{marker}{state.lower():>{_STATE_COL - 2}}"`.
fn state_col(state: &str) -> String {
    format!(
        "{}{}",
        state_marker(state),
        rjust(&state.to_lowercase(), STATE_COL - 2)
    )
}

/// One tree row: `prefix` is the ASCII indent + branch glyph, `label` the node
/// text, `state` the right column. The pad floors at 1 space.
fn line(prefix: &str, label: &str, state: &str) -> String {
    let full = format!("{prefix}{label}");
    // Python: `max(1, _TREE_LABEL_WIDTH + 4 - len(full))` — len() is char-wise,
    // and the tree glyphs (└─ │ ├─) are multi-byte.
    let pad = (TREE_LABEL_WIDTH + 4)
        .saturating_sub(full.chars().count())
        .max(1);
    format!("{full}{}{}", " ".repeat(pad), state_col(state))
}

/// Everything the SOM decomposition tree hangs off.
#[derive(Default)]
pub struct OrderCtx<'a> {
    pub service_orders: &'a [Value],
    pub services_by_so: HashMap<String, Vec<Value>>,
    pub tasks_by_service: HashMap<String, Vec<Value>>,
    pub subscription_id: Option<String>,
}

/// Render a product order with full SOM decomposition.
pub fn render_order(order: &Value, ctx: &OrderCtx<'_>) -> String {
    let oid = get_or(order, "id", "ORD-???");
    let state = get_or(order, "state", "?");
    let cust_id = get_or(order, "customerId", "—");
    let items = order.get("items").and_then(Value::as_array);
    let offering = match items.and_then(|i| i.first()) {
        Some(first) => get_or(first, "offeringId", "—"),
        None => "—".to_string(),
    };
    let placed = super::fmt::py_or(order, &["orderDate"], "—");
    let completed = super::fmt::py_or(order, &["completedDate"], "—");

    let mut lines = vec![
        format!("Customer:  {cust_id}"),
        format!("Placed:    {placed}"),
        format!("Completed: {completed}"),
        String::new(),
    ];

    // Root row: the order itself.
    lines.push(line("", &format!("Order {oid}"), &state));

    let total_so = ctx.service_orders.len();
    for (so_idx, so) in ctx.service_orders.iter().enumerate() {
        let so_id = get_or(so, "id", "SO-???");
        let so_state = get_or(so, "state", "?");
        let is_last_so = so_idx == total_so - 1;
        let so_branch = if is_last_so { "└─ " } else { "├─ " };
        let so_indent = if is_last_so { "   " } else { "│  " };
        lines.push(line(
            so_branch,
            &format!("Service Order {so_id}"),
            &so_state,
        ));

        let services = ctx.services_by_so.get(&so_id).cloned().unwrap_or_default();
        let cfs_list: Vec<&Value> = services
            .iter()
            .filter(|s| s.get("serviceType").and_then(Value::as_str) == Some("CFS"))
            .collect();
        let rfs_list: Vec<&Value> = services
            .iter()
            .filter(|s| s.get("serviceType").and_then(Value::as_str) == Some("RFS"))
            .collect();

        for (ci, cfs) in cfs_list.iter().enumerate() {
            let is_last_cfs = ci == cfs_list.len() - 1 && rfs_list.is_empty();
            let cfs_branch = if is_last_cfs { "└─ " } else { "├─ " };
            let cfs_indent = if is_last_cfs { "   " } else { "│  " };
            let label = format!("CFS {}  {}", get_or(cfs, "id", ""), get_or(cfs, "name", ""))
                .trim_end()
                .to_string();
            lines.push(line(
                &format!("{so_indent}{cfs_branch}"),
                &label,
                &get_or(cfs, "state", ""),
            ));

            // NOTE: the RFS loop is nested INSIDE the CFS loop in the oracle, so
            // two CFS nodes render the RFS list twice. Reproduced faithfully —
            // v0.1's decomposition is 1 CFS → 2 RFS, so it never bites in
            // practice, but "fixing" it here would be a behaviour change (R5).
            for (ri, rfs) in rfs_list.iter().enumerate() {
                let is_last_rfs = ri == rfs_list.len() - 1;
                let rfs_branch = if is_last_rfs { "└─ " } else { "├─ " };
                let rfs_indent = if is_last_rfs { "   " } else { "│  " };
                let rlabel = format!("RFS {}  {}", get_or(rfs, "id", ""), get_or(rfs, "name", ""))
                    .trim_end()
                    .to_string();
                lines.push(line(
                    &format!("{so_indent}{cfs_indent}{rfs_branch}"),
                    &rlabel,
                    &get_or(rfs, "state", ""),
                ));

                let rfs_id = get_or(rfs, "id", "");
                let tasks = ctx
                    .tasks_by_service
                    .get(&rfs_id)
                    .cloned()
                    .unwrap_or_default();
                for (ti, task) in tasks.iter().enumerate() {
                    let is_last_task = ti == tasks.len() - 1;
                    let task_branch = if is_last_task { "└─ " } else { "├─ " };
                    let tstate = get_or(task, "state", "");
                    let duration = fmt_duration(task);
                    // Python: `task.get("attemptCount") or task.get("attempts") or 1`
                    let attempts = super::fmt::py_or(task, &["attemptCount", "attempts"], "1")
                        .parse::<i64>()
                        .unwrap_or(1);
                    let mut suffix_bits: Vec<String> = Vec::new();
                    if !duration.is_empty() {
                        suffix_bits.push(duration);
                    }
                    if attempts > 1 {
                        suffix_bits.push(format!("{attempts} attempts"));
                    }
                    let suffix = if suffix_bits.is_empty() {
                        String::new()
                    } else {
                        format!("  {}", suffix_bits.join("  "))
                    };
                    let tlabel = format!(
                        "{}  {}",
                        get_or(task, "id", ""),
                        get_or(task, "taskType", "")
                    )
                    .trim_end()
                    .to_string();
                    lines.push(format!(
                        "{}{suffix}",
                        line(
                            &format!("{so_indent}{cfs_indent}{rfs_indent}{task_branch}"),
                            &tlabel,
                            &tstate,
                        )
                    ));
                }
            }
        }

        if cfs_list.is_empty() && rfs_list.is_empty() {
            lines.push(format!("{so_indent}(no services attached)"));
        }
    }

    if let Some(sub_id) = ctx.subscription_id.as_deref().filter(|s| !s.is_empty()) {
        lines.push(String::new());
        lines.push(format!("→ Subscription {sub_id} activated"));
    }

    lines.push(String::new());
    lines.push(summary_line(order, ctx));

    r#box(&lines, &format!("{oid}  {offering}"), FRAME_WIDTH)
}

/// Bottom row with elapsed time + per-stage breakdown.
fn summary_line(order: &Value, ctx: &OrderCtx<'_>) -> String {
    let placed = super::fmt::py_or(order, &["orderDate"], "");
    let completed = super::fmt::py_or(order, &["completedDate"], "");
    let mut elapsed = String::new();
    if !placed.is_empty() && !completed.is_empty() {
        if let (Some(p), Some(c)) = (parse_dt(&placed), parse_dt(&completed)) {
            let secs = (c - p).num_milliseconds() as f64 / 1000.0;
            elapsed = format!("  total {secs:.1}s");
        }
    }
    let n_so = ctx.service_orders.len();
    let n_tasks: usize = ctx.tasks_by_service.values().map(Vec::len).sum();
    let n_failed: usize = ctx
        .tasks_by_service
        .values()
        .flatten()
        .filter(|t| is_stuck_or_failed(&get_or(t, "state", "")))
        .count();
    let mut parts = vec![format!("{n_so} SO")];
    if n_tasks > 0 {
        parts.push(format!("{n_tasks} tasks"));
    }
    if n_failed > 0 {
        parts.push(format!("{n_failed} failed"));
    }
    format!("Summary: {}{elapsed}", parts.join(" · "))
}

fn fmt_duration(task: &Value) -> String {
    let started = super::fmt::py_or(task, &["startedAt"], "");
    let completed = super::fmt::py_or(task, &["completedAt"], "");
    if started.is_empty() || completed.is_empty() {
        return String::new();
    }
    match (parse_dt(&started), parse_dt(&completed)) {
        (Some(s), Some(c)) => {
            let sec = (c - s).num_milliseconds() as f64 / 1000.0;
            format!("({sec:.1}s)")
        }
        _ => String::new(),
    }
}

fn parse_dt(s: &str) -> Option<DateTime<chrono::FixedOffset>> {
    DateTime::parse_from_rfc3339(&s.replace('Z', "+00:00")).ok()
}
