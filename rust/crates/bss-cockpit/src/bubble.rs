//! The cockpit's assistant-bubble override chain. Port of the `FinalMessage`
//! branch of `cockpit_events` in `bss_csr.routes.cockpit`.
//!
//! Every rule here exists because a model misled an operator in a specific way,
//! and **the order matters** — the overrides are layered deliberately, with the
//! destructive-outcome override last-but-one because *the staged pending row is
//! the truth and the bubble must match it*, whatever the model wrote.
//!
//! Extracted from the SSE plumbing because this decides what an operator reads
//! and therefore what they authorise. It is pure; the route keeps only the wiring.

use serde_json::Value;

use crate::guards::{claims_handbook, suppress_tool_recap, KNOWLEDGE_HALLUCINATION_FALLBACK};
use crate::{knowledge_called, strip_fake_propose};

/// A destructive tool call — its name and args — captured this turn.
#[derive(Debug, Clone, PartialEq)]
pub struct DestructiveCall {
    pub name: String,
    pub args: Value,
}

/// Everything the override chain needs to decide the final bubble.
pub struct BubbleCtx<'a> {
    /// Tool calls captured this turn (`{name, args, call_id}` objects).
    pub captured_tool_calls: &'a [Value],
    /// A destructive tool whose result was `DESTRUCTIVE_OPERATION_BLOCKED` this
    /// turn — i.e. we just staged a pending_destructive row.
    pub last_proposal: Option<&'a DestructiveCall>,
    /// Destructive tools that ACTUALLY ran this turn (the wrapper let them
    /// through).
    pub executed_destructive: &'a [DestructiveCall],
}

/// Why the bubble was overridden — surfaced so the route can log the same
/// warnings the oracle does.
#[derive(Debug, Default, PartialEq)]
pub struct BubbleOutcome {
    pub text: String,
    pub empty_final_after_tool_calls: bool,
    pub anti_mimicry_stall: bool,
    pub knowledge_hallucination: bool,
}

/// `", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])` — the first three
/// args, Python-repr'd.
///
/// Insertion order is preserved (serde_json `preserve_order`, D9), so "first
/// three" means the same three the oracle picks.
fn args_preview(args: &Value) -> String {
    let Some(map) = args.as_object() else {
        return String::new();
    };
    map.iter()
        .take(3)
        .map(|(k, v)| format!("{k}={}", py_repr(v)))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Python `repr()` for the arg values interpolated with `!r`.
fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) => {
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
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn tool_names(calls: &[Value]) -> Vec<String> {
    calls
        .iter()
        .filter_map(|c| c.get("name").and_then(Value::as_str))
        .map(str::to_string)
        .collect()
}

/// Run the override chain over the model's raw final text.
///
/// `text` should already have had `strip_reasoning_leakage` applied (the route
/// does that as the events arrive).
pub fn finalize_bubble(text: &str, ctx: &BubbleCtx<'_>) -> BubbleOutcome {
    let mut out = BubbleOutcome::default();
    let mut text = text.to_string();

    // 1. v0.20.1 — an empty terminal AIMessage after tool calls is a known small-
    //    model failure. Tell the operator what fired and how to recover rather
    //    than dropping an opaque "(no reply)" that looks like a crashed turn.
    if text.is_empty() {
        if !ctx.captured_tool_calls.is_empty() {
            out.empty_final_after_tool_calls = true;
            text = format!(
                "(The model called `{}` but did not synthesise a final answer. \
                 Send the same question again or rephrase to retry.)",
                tool_names(ctx.captured_tool_calls).join(", ")
            );
        } else {
            text = "(no reply)".to_string();
        }
    }

    // 2. Defence-in-depth against the tool-recap habit: the deterministic ASCII
    //    card already showed; the operator doesn't need the LLM's prose copy.
    text = suppress_tool_recap(&text, ctx.captured_tool_calls);

    // 3. v1.5 — anti-mimicry. Strip the call shape + surrounding narration, then
    //    — regardless of what's left — warn when NO real tool_call fired but the
    //    bubble carries mimicry signals. A half-stripped propose ("I propose to
    //    terminate SUB-0005.") still misleads the operator into typing /confirm;
    //    the explicit replacement is the only honest UX.
    let (stripped, mimicry_stripped) = strip_fake_propose(&text);
    text = stripped;
    let lower = text.to_lowercase();
    let mentions_confirm = lower.contains("/confirm") || lower.contains("type confirm");
    if ctx.captured_tool_calls.is_empty() && (mimicry_stripped || mentions_confirm) {
        out.anti_mimicry_stall = true;
        text = "(The model wrote a propose banner as text instead of calling the \
                tool — no pending action. Rephrase the request or be more direct.)"
            .to_string();
    }

    // 4. v1.5 — the destructive-outcome override. THE STAGED ROW IS THE TRUTH and
    //    the bubble must match it, whatever the model wrote. Three observed
    //    wrap-ups after a BLOCKED destructive are all equally misleading: "Done."
    //    (implies it ran — it didn't), a mimicry-shaped propose paired with a real
    //    tool_call (so the stall warning above doesn't fire), and empty. Replaced
    //    unconditionally.
    if let Some(p) = ctx.last_proposal {
        text = format!(
            "Proposed {}({}). Type /confirm to authorise.",
            p.name,
            args_preview(&p.args)
        );
    } else if let Some(last) = ctx.executed_destructive.last() {
        // Symmetric post-execute override: "Done." is the same word the operator
        // just saw on the stalled-propose path, which teaches them not to trust
        // the cockpit. Name what actually happened.
        let preview = args_preview(&last.args);
        let n = ctx.executed_destructive.len();
        text = if n == 1 {
            format!("Executed {}({preview}).", last.name)
        } else {
            format!(
                "Executed {n} destructive actions, last was {}({preview}).",
                last.name
            )
        };
    }

    // 5. v0.20 — citation guard. Un-cited handbook claims get the safe fallback.
    if !text.is_empty() && claims_handbook(&text) && !knowledge_called(ctx.captured_tool_calls) {
        out.knowledge_hallucination = true;
        text = KNOWLEDGE_HALLUCINATION_FALLBACK.to_string();
    }

    out.text = text;
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use serde_json::json;

    fn ctx<'a>(
        calls: &'a [Value],
        proposal: Option<&'a DestructiveCall>,
        executed: &'a [DestructiveCall],
    ) -> BubbleCtx<'a> {
        BubbleCtx {
            captured_tool_calls: calls,
            last_proposal: proposal,
            executed_destructive: executed,
        }
    }

    #[test]
    fn plain_prose_passes_through() {
        let out = finalize_bubble("Your balance is 2GB.", &ctx(&[], None, &[]));
        assert_eq!(out.text, "Your balance is 2GB.");
        assert_eq!(
            out,
            BubbleOutcome {
                text: out.text.clone(),
                ..Default::default()
            }
        );
    }

    #[test]
    fn empty_with_no_tools_is_no_reply() {
        let out = finalize_bubble("", &ctx(&[], None, &[]));
        assert_eq!(out.text, "(no reply)");
        assert!(!out.empty_final_after_tool_calls);
    }

    #[test]
    fn empty_after_tool_calls_names_what_fired() {
        let calls = vec![
            json!({"name": "customer.get"}),
            json!({"name": "order.list"}),
        ];
        let out = finalize_bubble("", &ctx(&calls, None, &[]));
        assert!(out.empty_final_after_tool_calls);
        assert_eq!(
            out.text,
            "(The model called `customer.get, order.list` but did not synthesise \
             a final answer. Send the same question again or rephrase to retry.)"
        );
    }

    /// The stall warning is the honest UX: no real tool_call fired, so there is
    /// no pending action however confidently the model wrote one.
    #[test]
    fn narrated_propose_without_a_tool_call_becomes_the_stall_warning() {
        let out = finalize_bubble(
            "I propose to terminate the line. `subscription.terminate(subscription_id='SUB-1')` Please type /confirm.",
            &ctx(&[], None, &[]),
        );
        assert!(out.anti_mimicry_stall);
        assert!(out.text.contains("no pending action"));
    }

    /// A /confirm mention that SURVIVES the boilerplate strip stalls — a
    /// half-stripped propose still misleads. Note `mentions_confirm` is computed
    /// on the POST-strip text, so the trigger is a mention the strip didn't eat
    /// ("You must /confirm this" has no `type` prefix, so `_PLEASE_CONFIRM_RE`
    /// doesn't match it).
    #[test]
    fn a_surviving_confirm_mention_without_a_tool_call_stalls() {
        let out = finalize_bubble("You must /confirm this first.", &ctx(&[], None, &[]));
        assert!(out.anti_mimicry_stall);
        assert!(out.text.contains("no pending action"));
    }

    /// Faithful oracle quirk, pinned so nobody "fixes" it in the port: the
    /// empty-text check runs BEFORE `strip_fake_propose`, so a bubble that is
    /// *stripped* to nothing stays empty — it does not become "(no reply)" and it
    /// does not stall (the post-strip text no longer mentions /confirm, and the
    /// boilerplate strip alone never sets `mimicry_stripped`).
    ///
    /// Verified against the oracle. Reproduced under R5 rather than corrected;
    /// the fix belongs in the Python first.
    #[test]
    fn text_stripped_to_nothing_stays_empty() {
        let out = finalize_bubble("Please type /confirm to proceed.", &ctx(&[], None, &[]));
        assert_eq!(out.text, "");
        assert!(!out.anti_mimicry_stall);
    }

    /// THE key override: whatever the model wrote, a staged proposal wins.
    #[test]
    fn a_staged_proposal_overrides_the_bubble_unconditionally() {
        let p = DestructiveCall {
            name: "subscription.terminate".to_string(),
            args: json!({"subscription_id": "SUB-1", "reason": "customer_requested"}),
        };
        let calls = vec![json!({"name": "subscription.terminate"})];
        // "Done." implies the action ran. It didn't — it was BLOCKED and staged.
        for model_said in ["Done.", "", "I propose to terminate SUB-1. Type /confirm."] {
            let out = finalize_bubble(model_said, &ctx(&calls, Some(&p), &[]));
            assert_eq!(
                out.text,
                "Proposed subscription.terminate(subscription_id='SUB-1', \
                 reason='customer_requested'). Type /confirm to authorise.",
                "model said {model_said:?}"
            );
        }
    }

    #[test]
    fn args_preview_takes_the_first_three_only() {
        let p = DestructiveCall {
            name: "t".to_string(),
            args: json!({"a": 1, "b": 2, "c": 3, "d": 4}),
        };
        let calls = vec![json!({"name": "t"})];
        let out = finalize_bubble("x", &ctx(&calls, Some(&p), &[]));
        assert_eq!(
            out.text,
            "Proposed t(a=1, b=2, c=3). Type /confirm to authorise."
        );
    }

    /// The symmetric post-execute override — "Done." after a real action reads
    /// identically to "Done." after a stall, which teaches distrust.
    #[test]
    fn an_executed_destructive_names_what_happened() {
        let calls = vec![json!({"name": "subscription.terminate"})];
        let executed = vec![DestructiveCall {
            name: "subscription.terminate".to_string(),
            args: json!({"subscription_id": "SUB-1"}),
        }];
        let out = finalize_bubble("Done.", &ctx(&calls, None, &executed));
        assert_eq!(
            out.text,
            "Executed subscription.terminate(subscription_id='SUB-1')."
        );
    }

    #[test]
    fn multiple_executed_destructives_report_the_count_and_the_last() {
        let calls = vec![json!({"name": "case.close"})];
        let executed = vec![
            DestructiveCall {
                name: "case.close".to_string(),
                args: json!({"case_id": "CASE-1"}),
            },
            DestructiveCall {
                name: "case.close".to_string(),
                args: json!({"case_id": "CASE-2"}),
            },
        ];
        let out = finalize_bubble("Done.", &ctx(&calls, None, &executed));
        assert_eq!(
            out.text,
            "Executed 2 destructive actions, last was case.close(case_id='CASE-2')."
        );
    }

    /// A staged proposal takes precedence over an executed one (Python's
    /// if/elif) — the thing needing authorisation is the more urgent truth.
    #[test]
    fn a_proposal_wins_over_an_executed_destructive() {
        let p = DestructiveCall {
            name: "a.b".to_string(),
            args: json!({}),
        };
        let executed = vec![DestructiveCall {
            name: "c.d".to_string(),
            args: json!({}),
        }];
        let calls = vec![json!({"name": "a.b"})];
        let out = finalize_bubble("Done.", &ctx(&calls, Some(&p), &executed));
        assert!(out.text.starts_with("Proposed a.b()"));
    }

    #[test]
    fn uncited_handbook_claim_gets_the_fallback() {
        let calls = vec![json!({"name": "customer.get"})];
        let out = finalize_bubble(
            "Per the handbook, we never prorate.",
            &ctx(&calls, None, &[]),
        );
        assert!(out.knowledge_hallucination);
        assert_eq!(out.text, KNOWLEDGE_HALLUCINATION_FALLBACK);
    }

    /// A handbook claim IS allowed when a knowledge tool actually fired.
    #[test]
    fn a_cited_handbook_claim_survives() {
        let calls = vec![json!({"name": "knowledge.search"})];
        let text = "Per the handbook, we never prorate.";
        let out = finalize_bubble(text, &ctx(&calls, None, &[]));
        assert!(!out.knowledge_hallucination);
        assert_eq!(out.text, text);
    }
}
