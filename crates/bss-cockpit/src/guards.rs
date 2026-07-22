//! The cockpit's assistant-bubble guards. Port of the module-level guard logic in
//! `bss_csr.routes.cockpit` (the destructive-prefix set, the tool-recap
//! suppressor, and the v0.20 citation guard).
//!
//! Lifted out of the route module because it is pure and heavily rule-bound —
//! every rule here exists because a real model did the wrong thing on a real
//! turn, so each gets a test rather than a comment.

use std::sync::LazyLock;

use crate::renderers::dispatch::RENDERED_TOOLS;
use fancy_regex::Regex;
use serde_json::Value;

/// Destructive-tool prefixes — **the cockpit's own list**, mirroring
/// `cli/bss_cli/repl.py`.
///
/// **This is deliberately NOT `bss_orchestrator::safety::DESTRUCTIVE_TOOLS`.**
/// The two lists have different jobs and different contents (11 vs 33 entries,
/// partially overlapping):
///
/// * `safety::DESTRUCTIVE_TOOLS` decides what the agent loop **blocks**
///   (`DESTRUCTIVE_OPERATION_BLOCKED`).
/// * This list decides what the cockpit **stages as a `/confirm` proposal** or
///   records as an executed destructive for the operator's awareness.
///
/// The broader list means more tools are surfaced to the operator than are
/// blocked — intentional. The dangerous direction (blocked by safety but not
/// stageable here, i.e. an operator hitting a wall with no way to confirm) is
/// empty because the only such tools (`admin.*`) aren't in the
/// `operator_cockpit` profile. That invariant is pinned by a test.
pub const DESTRUCTIVE_PREFIXES: &[&str] = &[
    "subscription.terminate",
    "subscription.migrate_to_new_price",
    "subscription.purchase_vas",
    "subscription.schedule_plan_change",
    "subscription.cancel_pending_plan_change",
    "payment.add_card",
    "payment.remove_method",
    "payment.charge",
    "customer.create",
    "customer.update_contact",
    "customer.attest_kyc",
    "customer.close",
    "customer.add_contact_medium",
    "customer.remove_contact_medium",
    "case.open",
    "case.close",
    "case.add_note",
    "case.transition",
    "case.update_priority",
    "ticket.open",
    "ticket.assign",
    "ticket.transition",
    "ticket.resolve",
    "ticket.close",
    "ticket.cancel",
    "order.create",
    "order.cancel",
    "catalog.add_offering",
    "catalog.add_price",
    "catalog.window_offering",
    // v2.1 — the fourth catalog verb. The other three predate it here: the cockpit
    // could always *stage* them, they just never reached the model's surface.
    "catalog.retire_offering",
    "provisioning.resolve_stuck",
    "provisioning.retry_failed",
    "provisioning.set_fault_injection",
];

/// Prefix match — `name == p || name.starts_with(p)`.
///
/// Note the prefix semantics are load-bearing: `subscription.terminate` also
/// catches `subscription.terminate_mine`, and `case.open` also catches
/// `case.open_for_me`.
pub fn is_destructive(tool_name: &str) -> bool {
    DESTRUCTIVE_PREFIXES
        .iter()
        .any(|p| tool_name == *p || tool_name.starts_with(p))
}

// ── Tool-recap suppression ───────────────────────────────────────────

/// Bubble starts with a literal `<pre>` — the LLM is trying to format ASCII
/// inside HTML, a clear mimic signal.
static RE_RECAP_PRE_TAG: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?i)^\s*<pre[^>]*>").expect("compile-time constant")
});

/// A keyword from the canonical Customer 360 / Subscription vocabulary followed
/// by `:` or `|`, possibly wrapped in `**bold**`.
static RE_RECAP_HEADERED: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(
        r"(?i)\b(?:Customer|Status|KYC|Since|Contact|Subscriptions?|Open\s+Cases?|Recent\s+Interactions?|MSISDN|Plan|State|Activated|Renews|Bundle|Balance)\s*[:\|]",
    )
    .expect("compile-time constant")
});

/// v0.20+ — citation guard. Mirror of the REPL's `_RE_KNOWLEDGE_CLAIM`.
static RE_KNOWLEDGE_CLAIM: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(
        r"(?i)\b(?:(?:per|according\s+to|as\s+per)\s+(?:the\s+)?(?:handbook|runbook|doctrine|CLAUDE\.md|ARCHITECTURE\.md|DECISIONS\.md|TOOL_SURFACE\.md|HANDBOOK\.md)|(?:the|in\s+the)\s+(?:handbook|runbook|doctrine)\s+(?:says|states|specifies|mentions|requires|forbids|allows)|the\s+docs?\s+(?:say|state|specify|mention|require|forbid))\b",
    )
    .expect("compile-time constant")
});

pub const KNOWLEDGE_HALLUCINATION_FALLBACK: &str =
    "I don't have a citation for that. Run `bss admin knowledge search \
     \"<your query>\"` or open `docs/HANDBOOK.md` for the authoritative answer.";

/// True when the assistant claims handbook/runbook/doctrine.
pub fn claims_handbook(text: &str) -> bool {
    RE_KNOWLEDGE_CLAIM.is_match(text).unwrap_or(false)
}

/// True when the assistant bubble is mimicking a tool result.
///
/// `_COCKPIT_INVARIANTS` forbids re-rendering tool output, but small models
/// ignore the instruction. This heuristic catches the bubble before it reaches
/// the operator's eye. Two-or-more headered matches is the threshold, so a single
/// "Status: active" in legitimate commentary doesn't false-positive.
pub fn looks_like_tool_recap(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    if RE_RECAP_PRE_TAG.is_match(text).unwrap_or(false) {
        return true;
    }
    let matches = RE_RECAP_HEADERED
        .find_iter(text)
        .filter(|m| m.is_ok())
        .count();
    matches >= 2
}

/// Replace a tool-recap bubble with a short acknowledgement.
///
/// Only fires when (a) at least one tool with a **registered renderer** fired
/// this turn — so the operator already saw the canonical output above the bubble
/// — AND (b) the bubble matches the recap heuristics. Otherwise returns `text`
/// unchanged.
pub fn suppress_tool_recap(text: &str, captured_tool_calls: &[Value]) -> String {
    if captured_tool_calls.is_empty() {
        return text.to_string();
    }
    let rendered_tool_fired = captured_tool_calls.iter().any(|call| {
        call.get("name")
            .and_then(Value::as_str)
            .is_some_and(|n| RENDERED_TOOLS.contains(&n))
    });
    if !rendered_tool_fired {
        return text.to_string();
    }
    if !looks_like_tool_recap(text) {
        return text.to_string();
    }
    "(see above)".to_string()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use serde_json::json;

    #[test]
    fn is_destructive_matches_by_prefix() {
        assert!(is_destructive("subscription.terminate"));
        // Prefix semantics: the `_mine` wrapper is caught by the base name.
        assert!(is_destructive("subscription.terminate_mine"));
        assert!(is_destructive("case.open_for_me"));
        assert!(is_destructive("payment.charge"));
        // Reads are never destructive.
        assert!(!is_destructive("customer.get"));
        assert!(!is_destructive("subscription.list_for_customer"));
        assert!(!is_destructive(""));
    }

    // NOTE: the cross-crate invariant `no_cockpit_tool_is_blocked_without_being_
    // stageable` (cockpit `is_destructive` must cover every `operator_cockpit`
    // DESTRUCTIVE_TOOL) lives in `portals/csr/tests/cockpit_guards.rs` — it needs
    // `bss_orchestrator`, which this crate can't depend on (orchestrator → cockpit
    // is the dependency direction).

    #[test]
    fn recap_detects_a_pre_tag_bubble() {
        assert!(looks_like_tool_recap("<pre>┌─ Subscription SUB-1 ─┐</pre>"));
        assert!(looks_like_tool_recap("  <pre class=\"x\">stuff"));
    }

    #[test]
    fn recap_needs_two_headers_not_one() {
        // One header in commentary is legitimate — must NOT fire.
        assert!(!looks_like_tool_recap(
            "I've topped up your line — Status: active now."
        ));
        // Two or more is a recap.
        assert!(looks_like_tool_recap(
            "Customer: CUST-1\nStatus: active\nPlan: PLAN_M"
        ));
        // Bold-wrapped headers count too.
        assert!(looks_like_tool_recap(
            "**MSISDN:** 9123 4567\n**Plan:** PLAN_M"
        ));
        assert!(!looks_like_tool_recap(""));
    }

    #[test]
    fn suppress_only_when_a_rendered_tool_fired() {
        let recap = "Customer: CUST-1\nStatus: active\nPlan: PLAN_M";

        // No tools at all → unchanged.
        assert_eq!(suppress_tool_recap(recap, &[]), recap);

        // A tool fired, but one with NO registered renderer → the operator never
        // saw canonical output, so the bubble is all they have. Unchanged.
        let unrendered = vec![json!({"name": "customer.create"})];
        assert_eq!(suppress_tool_recap(recap, &unrendered), recap);

        // A RENDERED tool fired → the card is already above the bubble.
        let rendered = vec![json!({"name": "customer.get"})];
        assert_eq!(suppress_tool_recap(recap, &rendered), "(see above)");

        // Rendered tool fired but the bubble is legitimate commentary → kept.
        let commentary = "Done — I've topped up the line.";
        assert_eq!(suppress_tool_recap(commentary, &rendered), commentary);
    }

    #[test]
    fn knowledge_claim_detection() {
        for s in [
            "Per the handbook, block-on-exhaust is mandatory.",
            "According to the runbook you should retry.",
            "As per CLAUDE.md this is forbidden.",
            "The handbook says we never proration.",
            "In the doctrine states that...",
            "The docs say otherwise.",
        ] {
            assert!(claims_handbook(s), "should flag: {s}");
        }
        for s in [
            "Your balance is 2GB.",
            "I searched the knowledge base and found CASE-1.",
            "",
        ] {
            assert!(!claims_handbook(s), "should NOT flag: {s}");
        }
    }
}
