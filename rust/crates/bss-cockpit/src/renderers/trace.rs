//! ASCII swimlane renderer for a Jaeger trace — the v0.2 visual artifact. Port of
//! `bss_cockpit.renderers.trace`.
//!
//! Takes a Jaeger v1 trace (per `JaegerClient::get_trace`) and renders one row per
//! span, indented by parent-child depth, with a bar showing relative duration.
//!
//! Not in the renderer dispatch — `bss trace` (P7) is its consumer.

use serde_json::Value;

use super::fmt::{ljust, rjust_f, truncate};

/// Manual-span operation names (V0_2_0.md §2a) — these get the `*` marker.
pub const MANUAL_SPAN_NAMES: &[&str] = &[
    "com.order.complete_to_subscription",
    "som.decompose",
    "subscription.purchase_vas",
    "bss.ask",
];

/// The `shutil.get_terminal_size((140, 24))` fallback.
pub const DEFAULT_WIDTH: usize = 140;

#[derive(Debug, Clone)]
struct RenderSpan {
    span_id: String,
    parent_id: Option<String>,
    service: String,
    operation: String,
    start_micros: i64,
    duration_micros: i64,
    is_error: bool,
    is_sql: bool,
    is_manual: bool,
    /// v0.9 — perimeter-resolved identity from the `bss.service.identity` span
    /// tag. Empty when the span pre-dates v0.9 or the request never reached
    /// `RequestIdMiddleware` (rare; auth-401 paths).
    service_identity: String,
    depth: usize,
}

fn tags(s: &Value) -> &[Value] {
    s.get("tags").and_then(Value::as_array).map_or(&[], |v| v)
}

/// Flatten a Jaeger trace into rows + the trace bounds.
fn normalize(trace: &Value) -> (Vec<RenderSpan>, i64, i64) {
    let processes = trace.get("processes");
    let Some(spans) = trace.get("spans").and_then(Value::as_array) else {
        return (Vec::new(), 0, 0);
    };
    if spans.is_empty() {
        return (Vec::new(), 0, 0);
    }

    let mut rows: Vec<RenderSpan> = Vec::new();
    for s in spans {
        let process_id = s.get("processID").and_then(Value::as_str).unwrap_or("");
        let service = processes
            .and_then(|p| p.get(process_id))
            .and_then(|p| p.get("serviceName"))
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        let operation = s
            .get("operationName")
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        let upper = operation.to_uppercase();
        let is_sql = matches!(upper.as_str(), "BEGIN;" | "COMMIT;" | "ROLLBACK;")
            || ["SELECT", "INSERT", "UPDATE", "DELETE"]
                .iter()
                .any(|p| operation.starts_with(p));
        // `tag["value"] is True` — an identity check, so only a real JSON `true`
        // counts (a truthy 1 or "true" does not).
        let is_error = tags(s).iter().any(|t| {
            t.get("key").and_then(Value::as_str) == Some("error")
                && t.get("value") == Some(&Value::Bool(true))
        });
        let is_manual = MANUAL_SPAN_NAMES.contains(&operation.as_str());
        let service_identity = tags(s)
            .iter()
            .find(|t| t.get("key").and_then(Value::as_str) == Some("bss.service.identity"))
            .and_then(|t| t.get("value"))
            .map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            })
            .unwrap_or_default();
        // The FIRST CHILD_OF reference is the parent.
        let parent_id = s
            .get("references")
            .and_then(Value::as_array)
            .and_then(|refs| {
                refs.iter()
                    .find(|r| r.get("refType").and_then(Value::as_str) == Some("CHILD_OF"))
            })
            .and_then(|r| r.get("spanID"))
            .and_then(Value::as_str)
            .map(str::to_string);

        rows.push(RenderSpan {
            span_id: s
                .get("spanID")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            parent_id,
            service,
            operation,
            start_micros: s.get("startTime").and_then(Value::as_i64).unwrap_or(0),
            duration_micros: s.get("duration").and_then(Value::as_i64).unwrap_or(0),
            is_error,
            is_sql,
            is_manual,
            service_identity,
            depth: 0,
        });
    }

    let trace_start = rows.iter().map(|r| r.start_micros).min().unwrap_or(0);
    let trace_end = rows
        .iter()
        .map(|r| r.start_micros + r.duration_micros)
        .max()
        .unwrap_or(0);
    (rows, trace_start, trace_end)
}

/// Walk the parent chain to assign each row a depth. The `seen` set is a cycle
/// guard — a malformed trace must not hang the renderer.
fn assign_depths(rows: &mut [RenderSpan]) {
    let by_id: std::collections::HashMap<String, (Option<String>, usize)> = rows
        .iter()
        .enumerate()
        .map(|(i, r)| (r.span_id.clone(), (r.parent_id.clone(), i)))
        .collect();
    let depths: Vec<usize> = rows
        .iter()
        .map(|r| {
            let mut depth = 0;
            let mut cur_id = r.span_id.clone();
            let mut cur_parent = r.parent_id.clone();
            let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
            while let Some(pid) = cur_parent.clone() {
                if !by_id.contains_key(&pid) || seen.contains(&cur_id) {
                    break;
                }
                seen.insert(cur_id.clone());
                let (pp, _) = &by_id[&pid];
                cur_id = pid;
                cur_parent = pp.clone();
                depth += 1;
            }
            depth
        })
        .collect();
    for (r, d) in rows.iter_mut().zip(depths) {
        r.depth = d;
    }
}

/// Sort spans into parent-then-children order (DFS by `start_micros`).
fn sort_by_tree_order(rows: Vec<RenderSpan>) -> Vec<RenderSpan> {
    let mut by_parent: std::collections::HashMap<Option<String>, Vec<usize>> =
        std::collections::HashMap::new();
    for (i, r) in rows.iter().enumerate() {
        by_parent.entry(r.parent_id.clone()).or_default().push(i);
    }
    for sibs in by_parent.values_mut() {
        sibs.sort_by_key(|i| rows[*i].start_micros);
    }

    let mut out: Vec<usize> = Vec::new();
    let mut stack: Vec<Option<String>> = vec![None];
    // Iterative DFS (Python recurses; a malformed deep trace shouldn't blow the
    // stack in a server process).
    fn walk(
        key: Option<String>,
        by_parent: &std::collections::HashMap<Option<String>, Vec<usize>>,
        rows: &[RenderSpan],
        out: &mut Vec<usize>,
        guard: &mut std::collections::HashSet<String>,
    ) {
        let Some(children) = by_parent.get(&key) else {
            return;
        };
        for &i in children {
            if !guard.insert(rows[i].span_id.clone()) {
                continue;
            }
            out.push(i);
            walk(Some(rows[i].span_id.clone()), by_parent, rows, out, guard);
        }
    }
    let mut guard = std::collections::HashSet::new();
    walk(
        stack.pop().unwrap_or(None),
        &by_parent,
        &rows,
        &mut out,
        &mut guard,
    );

    // Orphans (parent missing from the batch — shouldn't normally happen).
    let mut result: Vec<RenderSpan> = out.iter().map(|i| rows[*i].clone()).collect();
    let placed: std::collections::HashSet<usize> = out.into_iter().collect();
    for (i, r) in rows.iter().enumerate() {
        if !placed.contains(&i) {
            result.push(r.clone());
        }
    }
    result
}

/// Options for [`render_swimlane`].
#[derive(Debug, Default)]
pub struct SwimlaneOpts<'a> {
    /// `None` → [`DEFAULT_WIDTH`] (Python reads the terminal size).
    pub width: Option<usize>,
    pub show_sql: bool,
    pub only_service: Option<&'a str>,
}

/// Render the Jaeger trace as an ASCII swimlane.
pub fn render_swimlane(trace: &Value, opts: &SwimlaneOpts<'_>) -> String {
    let (mut rows, trace_start, trace_end) = normalize(trace);
    if rows.is_empty() {
        return "(empty trace)\n".to_string();
    }
    assign_depths(&mut rows);
    let mut rows = sort_by_tree_order(rows);

    if let Some(svc) = opts.only_service {
        rows.retain(|r| r.service == svc);
    }

    let visible: Vec<&RenderSpan> = rows.iter().filter(|r| opts.show_sql || !r.is_sql).collect();
    let hidden_sql = rows.len() - visible.len();

    let mut services: Vec<&str> = rows.iter().map(|r| r.service.as_str()).collect();
    services.sort_unstable();
    services.dedup();
    let error_count = rows.iter().filter(|r| r.is_error).count();
    let total_micros = trace_end - trace_start;
    let total_ms = total_micros as f64 / 1000.0;

    // Layout.
    let term_width = opts.width.unwrap_or(DEFAULT_WIDTH);
    const INDENT_PER_LEVEL: usize = 2;
    let max_depth = visible.iter().map(|r| r.depth).max().unwrap_or(0);
    let label_col_w = 14 + INDENT_PER_LEVEL * max_depth.max(1);
    const DURATION_COL_W: usize = 8;
    // Wide enough for the longest manual-span name
    // (`com.order.complete_to_subscription` = 34) + the asterisk.
    const OP_COL_W: usize = 40;
    // v0.9 — the perimeter-identity column, wide enough for "portal_self_serve"
    // (17) plus a leading space. Hidden entirely when no span carries a tag, so
    // pre-v0.9 traces stay clean.
    let has_identity = visible.iter().any(|r| !r.service_identity.is_empty());
    let identity_col_w = if has_identity { 18 } else { 0 };
    let bar_w = 20.max(
        term_width.saturating_sub(label_col_w + identity_col_w + DURATION_COL_W + OP_COL_W + 4),
    );

    let trace_id = trace
        .get("traceID")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| rows.first().map(|r| r.span_id.clone()).unwrap_or_default());
    let trace_id_short = if trace_id.chars().count() > 16 {
        format!("{}…", truncate(&trace_id, 16))
    } else {
        trace_id
    };

    let mut lines: Vec<String> = Vec::new();
    lines.push(format!(
        "Trace {trace_id_short}  total {total_ms:.0}ms  ·  {} spans  ·  {} services  ·  {error_count} errors",
        rows.len(),
        services.len()
    ));
    lines.push(String::new());

    for r in &visible {
        let (offset_frac, width_frac) = if total_micros > 0 {
            (
                (r.start_micros - trace_start) as f64 / total_micros as f64,
                (r.duration_micros as f64 / total_micros as f64).max(0.001),
            )
        } else {
            (0.0, 1.0)
        };
        let offset = (offset_frac * bar_w as f64) as usize;
        let bar_chars = 1.max((width_frac * bar_w as f64) as usize);
        let mut bar = " ".repeat(offset);
        bar.push('┃');
        bar.push_str(&"━".repeat(bar_chars.saturating_sub(2)));
        if bar_chars >= 2 {
            bar.push('┃');
        }
        let bar = ljust(&bar, bar_w);

        let indent = " ".repeat(INDENT_PER_LEVEL * r.depth);
        let svc_label = ljust(&format!("{indent}{}", r.service), label_col_w);
        let ms = r.duration_micros as f64 / 1000.0;
        let dur_label = format!("{}ms", rjust_f(ms, 6, 0));
        let marker = if r.is_manual { " *" } else { "  " };
        let op_label = if r.operation.chars().count() > OP_COL_W {
            format!("{}…", truncate(&r.operation, OP_COL_W - 1))
        } else {
            r.operation.clone()
        };

        let mut line = if identity_col_w > 0 {
            let mut ident = if r.service_identity.is_empty() {
                "—".to_string()
            } else {
                r.service_identity.clone()
            };
            if ident.chars().count() > identity_col_w - 1 {
                ident = format!("{}…", truncate(&ident, identity_col_w - 2));
            }
            format!(
                "{svc_label}{}{bar}  {dur_label}  {op_label}{marker}",
                ljust(&ident, identity_col_w)
            )
        } else {
            format!("{svc_label}{bar}  {dur_label}  {op_label}{marker}")
        };
        if r.is_error {
            // Wrap the full line in red ANSI.
            line = format!("\u{1b}[31m{line} ERR\u{1b}[0m");
        }
        lines.push(line);
    }

    if hidden_sql > 0 {
        lines.push(String::new());
        lines.push(format!(
            "⋯ {hidden_sql} SQL spans hidden — rerun with --show-sql to expand"
        ));
    }
    lines.push(String::new());
    lines.push("* business span (manually instrumented)".to_string());
    format!("{}\n", lines.join("\n"))
}
