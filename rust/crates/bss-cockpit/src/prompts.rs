//! Cockpit system-prompt builder. Port of
//! `packages/bss-cockpit/bss_cockpit/prompts.py`.
//!
//! Composes the per-turn system prompt passed to `astream_once(system_prompt=…)`:
//! 1. the operator's persona + house rules (`OPERATOR.md`, verbatim);
//! 2. the cockpit's invariant guidance ([`COCKPIT_INVARIANTS`], code-defined —
//!    a behavioural contract with the model, so it is embedded byte-for-byte via
//!    `include_str!` and pinned by a golden test);
//! 3. per-turn blocks: `customer_focus` and the just-consumed pending-destructive
//!    proposal.
//!
//! The split is doctrine: house rules are operator-customisable; the safety
//! contract is code-defined. Weakening it means editing code, not markdown.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::conversation::PendingDestructive;

/// The cockpit's code-defined safety contract, prepended verbatim to every
/// system prompt. Extracted byte-for-byte from `prompts._COCKPIT_INVARIANTS`.
pub const COCKPIT_INVARIANTS: &str = include_str!("cockpit_invariants.txt");

/// Compose the system prompt for one cockpit turn.
///
/// `operator_md` is the verbatim `OPERATOR.md`; `customer_focus` is the pinned
/// `CUST-NNN` (or `None`); `pending_destructive` is the just-consumed propose
/// row when the turn runs with `allow_destructive=true`; `extra_context` renders
/// as a `## Context` block of sorted `key: value` lines.
pub fn build_cockpit_prompt(
    operator_md: &str,
    customer_focus: Option<&str>,
    pending_destructive: Option<&PendingDestructive>,
    extra_context: Option<&BTreeMap<String, String>>,
) -> String {
    let mut parts: Vec<String> = Vec::new();

    let md = operator_md.trim_end();
    if !md.is_empty() {
        parts.push(md.to_string());
    }

    parts.push(COCKPIT_INVARIANTS.trim_end().to_string());

    if let Some(cf) = customer_focus.filter(|s| !s.is_empty()) {
        parts.push(format!(
            "## Customer focus\n\nThe operator has pinned `{cf}` for this session. \
             Default to that customer when a question is ambiguous; ask the operator \
             if you need to act on a different customer."
        ));
    }

    if let Some(pd) = pending_destructive {
        // Render args as a compact `k=v` list (Python `f"{k}={v!r}"`) so the
        // prompt stays prose-shaped. IndexMap preserves the stored JSON order.
        let args_pairs: Vec<String> = pd
            .tool_args
            .iter()
            .map(|(k, v)| format!("{k}={}", py_repr(v)))
            .collect();
        let args = if args_pairs.is_empty() {
            "(no args)".to_string()
        } else {
            args_pairs.join(", ")
        };
        parts.push(format!(
            "## Confirmed destructive action\n\n\
             The operator typed `/confirm` for the prior propose. \
             You are now authorised to call exactly:\n\n\
             - tool: `{}`\n\
             - args: {}\n\n\
             Run it. Surface the result. Do not call any other \
             destructive tool on this turn.",
            pd.tool_name, args
        ));
    }

    if let Some(ctx) = extra_context.filter(|c| !c.is_empty()) {
        // BTreeMap iterates sorted by key (Python `sorted(items())`).
        let ctx_lines: Vec<String> = ctx.iter().map(|(k, v)| format!("- {k}: {v}")).collect();
        parts.push(format!("## Context\n\n{}", ctx_lines.join("\n")));
    }

    format!("{}\n", parts.join("\n\n"))
}

/// Python `repr()` for a JSON value, faithful for the tool-arg shapes that reach
/// the pending-destructive block (strings, bools, null, numbers). Strings get
/// Python's quote selection: single quotes unless the string contains `'` and no
/// `"`. Nested containers fall back to a Python-shaped repr.
fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr_str(s),
        Value::Bool(b) => {
            if *b {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, val)| format!("{}: {}", py_repr_str(k), py_repr(val)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// Python `repr()` of a string: pick the quote (single, unless the string has a
/// `'` and no `"`), escape backslash, the quote, and `\n`/`\r`/`\t`.
fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}
