//! Cockpit turn planning — the transcript-block parser and the decision of what
//! a `GET /cockpit/{id}/events` request should actually do. Port of the pure
//! logic inside `cockpit_events` in `bss_csr.routes.cockpit`.
//!
//! Extracted from the route because this is where the turn's *correctness* lives
//! — whether the LLM runs at all, on which prompt, and with destructive writes
//! authorised or not. All of it is decidable from the transcript + the pending
//! row, so it is pure and tested. The route keeps only the SSE plumbing.

use serde::Serialize;

/// One `role:\ncontent` block of the cockpit transcript.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Block {
    pub role: String,
    pub tool_name: String,
    pub body: String,
}

/// Parse `role:\ncontent` blocks. Mirrors the role mapping the orchestrator uses
/// (`user` / `assistant` / `tool[NAME]`); tool blocks carry the bracketed name so
/// the template can treat them differently from assistant bubbles.
///
/// Unknown roles are **dropped** (Python's if/elif has no else) — a block that is
/// neither user/assistant nor tool-prefixed never reaches the template.
pub fn split_transcript_blocks(transcript: &str) -> Vec<Block> {
    let mut out = Vec::new();
    if transcript.trim().is_empty() {
        return out;
    }
    for block in transcript.split("\n\n") {
        let block = block.trim_matches('\n');
        if block.is_empty() {
            continue;
        }
        // Python's `partition("\n")` → ("head", "", "") when there's no newline.
        let (head, body) = match block.split_once('\n') {
            Some((h, b)) => (h, b),
            None => (block, ""),
        };
        let head = head.trim().trim_end_matches(':');
        let body = body.trim();
        if head.is_empty() {
            continue;
        }
        if head.starts_with("tool") {
            let tool_name = head
                .strip_prefix("tool[")
                .and_then(|s| s.strip_suffix(']'))
                .unwrap_or("");
            out.push(Block {
                role: "tool".to_string(),
                tool_name: tool_name.to_string(),
                body: body.to_string(),
            });
        } else if head == "user" || head == "assistant" {
            out.push(Block {
                role: head.to_string(),
                tool_name: String::new(),
                body: body.to_string(),
            });
        }
        // else: dropped, matching the oracle.
    }
    out
}

/// Inverse of [`split_transcript_blocks`] — back to the canonical
/// `role:\ncontent\n\nrole:\ncontent` form.
pub fn join_blocks(blocks: &[Block]) -> String {
    blocks
        .iter()
        .map(|b| {
            let head = if b.role == "tool" {
                if b.tool_name.is_empty() {
                    "tool".to_string()
                } else {
                    format!("tool[{}]", b.tool_name)
                }
            } else {
                b.role.clone()
            };
            format!("{head}:\n{}", b.body)
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// The prompt substituted when the operator types `/confirm` — a clear
/// authorisation cue so the LLM picks up the prior turn's propose context.
pub const CONFIRM_PROMPT: &str = "(operator typed /confirm — proceed with the prior \
                                  destructive proposal now; call the tool)";

/// What a `/events` request should do.
#[derive(Debug, PartialEq)]
pub enum TurnPlan {
    /// No user message at all — emit a `done` status and stop.
    Nothing,
    /// The latest user message already has an assistant answer after it. Emit the
    /// reload marker + `done`; **never re-run the LLM**. This is what makes page
    /// reloads free.
    Replay,
    /// Drive a turn.
    Drive(Box<DriveTurn>),
}

/// The resolved inputs for one agent turn.
#[derive(Debug, PartialEq)]
pub struct DriveTurn {
    /// The prompt handed to the agent (already `/confirm`-substituted).
    pub user_message: String,
    /// Everything BEFORE the new user message, re-joined as transcript context.
    pub prior_transcript: String,
    /// Whether destructive tools are authorised for this turn.
    pub allow_destructive: bool,
    /// True when the operator typed `/confirm` (as opposed to a stashed
    /// pending-destructive row authorising the turn).
    pub via_slash_confirm: bool,
}

/// Decide what to do, given the transcript and whether a pending-destructive row
/// was consumed.
///
/// `pending_consumed` is the result of `Conversation::consume_pending_destructive`
/// — consuming it IS the authorisation (the row is the contract).
pub fn plan_turn(transcript: &str, pending_consumed: bool) -> TurnPlan {
    let blocks = split_transcript_blocks(transcript);

    // The LAST user block is the prompt (not the first).
    let Some(last_user_index) = blocks.iter().rposition(|b| b.role == "user") else {
        return TurnPlan::Nothing;
    };

    // Answered already → don't re-run the LLM on a page reload.
    let answered_after = blocks[last_user_index + 1..]
        .iter()
        .any(|b| b.role == "assistant");
    if answered_after {
        return TurnPlan::Replay;
    }

    let raw = blocks[last_user_index].body.clone();
    let mut allow_destructive = pending_consumed;
    let mut via_slash_confirm = false;
    let mut user_message = raw.clone();

    // v0.13.1 — `/confirm` typed in the textarea. Some models leak tool-call
    // markup as plain text rather than structured tool_calls; when that happens
    // no ToolCallStarted fires and pending_destructive never gets stashed. The
    // operator typing /confirm rightly expects "now run the destructive thing" —
    // even without a stashed propose. We honour that intent.
    //
    // Doctrine note: this widens the trust beat by one turn — acceptable for a
    // single-operator-by-design cockpit behind a secure perimeter (DECISIONS
    // 2026-05-01). It is NOT a general escape hatch: it only sets
    // allow_destructive for THIS turn, and the policy layer is still the server
    // gate.
    if raw.trim_start().to_lowercase().starts_with("/confirm") {
        allow_destructive = true;
        via_slash_confirm = true;
        user_message = CONFIRM_PROMPT.to_string();
    }

    TurnPlan::Drive(Box::new(DriveTurn {
        user_message,
        prior_transcript: join_blocks(&blocks[..last_user_index]),
        allow_destructive,
        via_slash_confirm,
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn drive(plan: TurnPlan) -> DriveTurn {
        match plan {
            TurnPlan::Drive(d) => *d,
            other => panic!("expected Drive, got {other:?}"),
        }
    }

    // ── block parsing ───────────────────────────────────────────────────────

    #[test]
    fn splits_roles_and_tool_names() {
        let t = "user:\nhi\n\nassistant:\nhello\n\ntool[customer.get]:\n{\"id\":\"CUST-1\"}";
        let b = split_transcript_blocks(t);
        assert_eq!(b.len(), 3);
        assert_eq!(b[0].role, "user");
        assert_eq!(b[0].body, "hi");
        assert_eq!(b[2].role, "tool");
        assert_eq!(b[2].tool_name, "customer.get");
        assert_eq!(b[2].body, "{\"id\":\"CUST-1\"}");
    }

    #[test]
    fn bare_tool_block_has_no_name() {
        let b = split_transcript_blocks("tool:\nresult");
        assert_eq!(b[0].role, "tool");
        assert_eq!(b[0].tool_name, "");
    }

    #[test]
    fn unknown_roles_are_dropped() {
        // Python's if/elif has no else — a `system:` block never reaches the
        // template rather than rendering as an unstyled bubble.
        let b = split_transcript_blocks("system:\nyou are\n\nuser:\nhi");
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].role, "user");
    }

    #[test]
    fn empty_transcript_is_empty() {
        assert!(split_transcript_blocks("").is_empty());
        assert!(split_transcript_blocks("   \n\n  ").is_empty());
    }

    #[test]
    fn join_is_the_inverse_of_split() {
        let t = "user:\nhi\n\nassistant:\nhello\n\ntool[customer.get]:\n{}";
        assert_eq!(join_blocks(&split_transcript_blocks(t)), t);
        // A bare tool block round-trips to `tool:`.
        let t2 = "tool:\nx";
        assert_eq!(join_blocks(&split_transcript_blocks(t2)), t2);
    }

    // ── the plan ────────────────────────────────────────────────────────────

    #[test]
    fn no_user_message_does_nothing() {
        assert_eq!(plan_turn("", false), TurnPlan::Nothing);
        assert_eq!(plan_turn("assistant:\nhi", false), TurnPlan::Nothing);
    }

    /// The property that makes page reloads free: an answered user message must
    /// never re-run the LLM.
    #[test]
    fn answered_user_message_replays_instead_of_re_running() {
        let t = "user:\nwhat's my balance?\n\nassistant:\n2GB left.";
        assert_eq!(plan_turn(t, false), TurnPlan::Replay);
        // Tool rows between the two don't change that.
        let t = "user:\nq\n\ntool[customer.get]:\n{}\n\nassistant:\na";
        assert_eq!(plan_turn(t, false), TurnPlan::Replay);
    }

    #[test]
    fn unanswered_user_message_drives() {
        let d = drive(plan_turn("user:\nwhat's my balance?", false));
        assert_eq!(d.user_message, "what's my balance?");
        assert_eq!(d.prior_transcript, "");
        assert!(!d.allow_destructive);
        assert!(!d.via_slash_confirm);
    }

    /// The LAST user block is the prompt, and everything before it is context.
    #[test]
    fn drives_the_latest_user_message_with_prior_as_context() {
        let t = "user:\nfirst\n\nassistant:\nreply\n\nuser:\nsecond";
        let d = drive(plan_turn(t, false));
        assert_eq!(d.user_message, "second");
        assert_eq!(d.prior_transcript, "user:\nfirst\n\nassistant:\nreply");
    }

    /// A tool row after the last user message is NOT an answer — the turn was
    /// interrupted mid-flight, so it must still drive.
    #[test]
    fn a_trailing_tool_row_is_not_an_answer() {
        let t = "user:\nq\n\ntool[customer.get]:\n{}";
        let d = drive(plan_turn(t, false));
        assert_eq!(d.user_message, "q");
    }

    #[test]
    fn a_consumed_pending_row_authorises_the_turn() {
        let d = drive(plan_turn("user:\ngo ahead", true));
        assert!(
            d.allow_destructive,
            "consuming the row IS the authorisation"
        );
        assert!(!d.via_slash_confirm);
        // The prompt is untouched — only /confirm substitutes it.
        assert_eq!(d.user_message, "go ahead");
    }

    /// v0.13.1 — /confirm authorises even with NO stashed pending row (the model
    /// leaked tool-call markup as text, so nothing got stashed; the operator's
    /// intent still stands).
    #[test]
    fn slash_confirm_authorises_without_a_pending_row() {
        let d = drive(plan_turn("user:\n/confirm", false));
        assert!(d.allow_destructive);
        assert!(d.via_slash_confirm);
        assert_eq!(d.user_message, CONFIRM_PROMPT);
    }

    #[test]
    fn slash_confirm_is_case_and_whitespace_tolerant() {
        for raw in ["/confirm", "  /confirm", "/CONFIRM", "/Confirm do it"] {
            let d = drive(plan_turn(&format!("user:\n{raw}"), false));
            assert!(d.allow_destructive, "{raw:?} should authorise");
            assert_eq!(d.user_message, CONFIRM_PROMPT);
        }
    }

    /// The guard is `startswith` — a /confirm mentioned mid-sentence must NOT
    /// authorise, or an LLM echoing the word could self-authorise.
    #[test]
    fn confirm_mid_sentence_does_not_authorise() {
        let d = drive(plan_turn("user:\nshould I type /confirm now?", false));
        assert!(!d.allow_destructive);
        assert!(!d.via_slash_confirm);
        assert_eq!(d.user_message, "should I type /confirm now?");
    }
}
