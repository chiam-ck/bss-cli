//! Chrome filter for cockpit-message history rehydration. Port of the
//! `is_cockpit_chrome` slice of `packages/bss-cockpit/bss_cockpit/chrome_filter.py`.
//!
//! When [`crate::conversation::Conversation::transcript_text`] rehydrates prior
//! turns into the LLM's context, the LLM should see only genuine user/assistant/
//! tool turns — never the cockpit's own emit chrome (the `(no reply)` fallback,
//! the empty-final recovery bubble, the citation-guard fallback, the route error
//! bubble). Leaving chrome in causes mimicry / state-confusion / citation-thrash
//! (the three v1.5 failure modes).
//!
//! [`strip_fake_propose`] (the propose-banner scrubber) is consumed by the
//! portals/CLI post-processing, not by the store. It landed in P6c with its
//! cockpit consumer; its narration-lead regex needs `fancy-regex`'s lookbehind.

use std::sync::LazyLock;

use fancy_regex::Regex;

/// Exact prefixes of every chrome-shaped assistant string the cockpit emits.
/// Any addition to the cockpit's emit set MUST also land here — the inventory
/// test pins the set so an omission shows up in CI (re-opens the mimicry/
/// state-confusion/citation-thrash failure modes).
pub const ASSISTANT_CHROME_PREFIXES: &[&str] = &[
    // Route-level error fallback (portals/csr cockpit.py).
    "Sorry — something went wrong",
    // Gemma empty-final-after-tool-calls recovery (portals/csr + cli REPL).
    "(The model called ",
    // Total empty-AIMessage fallback (same two sites).
    "(no reply)",
    // Citation guard fallback (_KNOWLEDGE_HALLUCINATION_FALLBACK).
    "I don't have a citation for that",
];

/// True when a persisted assistant message is cockpit-rendered chrome rather
/// than something the LLM actually said. Empty / whitespace-only content is
/// chrome (the cockpit never persists empty real replies — they become the
/// `(no reply)` fallback before persistence).
pub fn is_cockpit_chrome(content: &str) -> bool {
    if content.trim().is_empty() {
        return true;
    }
    ASSISTANT_CHROME_PREFIXES
        .iter()
        .any(|p| content.starts_with(p))
}

// ── strip_fake_propose (v1.5 anti-mimicry runtime backstop) ──────────
//
// LLMs sometimes emit text that LOOKS LIKE the cockpit's propose banner. The
// anti-mimicry rule in the system prompt is the first line of defence; this is
// the runtime backstop. Two shapes observed in the wild:
//
//   A. Banner mimicry — a leading `⚠ PROPOSE:` / `PROPOSE [step 2]:` line.
//   B. Narrated-call mimicry — a `tool.name(args)` shape in prose, often paired
//      with "Please type /confirm".
//
// Both mislead the operator into typing /confirm for an action that will never
// fire, because no real tool_call was made.

/// Shape A — the banner line (deleted whole).
static FAKE_PROPOSE_LINE_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?mi)^[ \t]*(?:⚠\s*)?PROPOSE\s*(?:\[\s*step\s*\d+\s*\])?\s*:.*?(?:\n|$)")
        .expect("compile-time constant")
});

/// Shape B — `lower.lower(...)`, optionally backtick-wrapped. At least one dot
/// is required so arbitrary parenthesised prose isn't matched.
static NARRATED_CALL_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(
        r"`{1,3}[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\([^)\n]*\)`{1,3}|\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\([^)\n]*\)",
    )
    .expect("compile-time constant")
});

/// Mimicry narration adjacent to a stripped call. Conservative: the verb must be
/// in a small canon — the same canon that triggers the destructive contract.
/// **Needs the lookbehind** (`(?<=[.!?]\s)`) that plain `regex` can't express.
static NARRATION_LEAD_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(
        r"(?i)(?:^|(?<=[.!?]\s))\s*I(?:'ll|\s+will|\s+would\s+like|\s+intend|\s+propose|'m\s+going)\s+to\s+(?:propose|call|invoke|terminate|cancel|close|remove|refund|revoke)\b[^.!?\n]*[.!?]?",
    )
    .expect("compile-time constant")
});

/// Leftover empty inline-code fragments once a backtick-wrapped call is removed.
static EMPTY_BACKTICK_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"`{2,3}\s*`*").expect("compile-time constant")
});

/// The "Please type /confirm" boilerplate. Matched as standalone sentences so
/// prose that LEGITIMATELY mentions /confirm isn't sliced.
static PLEASE_CONFIRM_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?i)(?:^|\n)\s*(?:please\s+)?type\s+`?/confirm`?[^\n.]*\.?\s*")
        .expect("compile-time constant")
});

static WS_RUN_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"[ \t]+").expect("compile-time constant")
});

static BLANK_RUN_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"\n\s*\n\s*\n+").expect("compile-time constant")
});

/// Count matches then replace — `fancy_regex` has no `subn`.
fn subn(re: &Regex, hay: &str, rep: &str) -> (String, usize) {
    let n = re.find_iter(hay).filter(|m| m.is_ok()).count();
    (re.replace_all(hay, rep).into_owned(), n)
}

/// Strip cockpit-banner-shaped lines AND narrated function-call shapes from an
/// LLM text reply. Returns `(cleaned_text, was_modified)`.
///
/// Legitimate prose (the ask, the explanation, the wrap-up) is preserved; only
/// the chrome-shaped fragments go. Operators reading the cleaned output don't see
/// a misleading PROPOSE-shape or a fake "type /confirm" prompt that won't fire.
///
/// Pass 2 is deliberately conservative-but-eager: a single `tool.name(arg)` in
/// legitimate prose is rare, and the cost of a false positive (explaining a
/// missing fragment) is far lower than a false negative (a stalled /confirm loop
/// where the operator authorises nothing). The trade-off favours stripping.
///
/// **`was_modified` only reflects the banner + call strips**, never the
/// /confirm-sentence strip: that regex alone can match legitimate carve-outs
/// ("type /confirm to authorise" inside a knowledge-grounded answer), and the
/// caller uses this flag to decide whether to show a stall warning.
pub fn strip_fake_propose(text: &str) -> (String, bool) {
    let (cleaned, n_banner) = subn(&FAKE_PROPOSE_LINE_RE, text, "");
    let (mut cleaned, n_calls) = subn(&NARRATED_CALL_RE, &cleaned, "");
    if n_calls > 0 {
        cleaned = NARRATION_LEAD_RE.replace_all(&cleaned, "").into_owned();
        cleaned = EMPTY_BACKTICK_RE.replace_all(&cleaned, "").into_owned();
    }
    cleaned = PLEASE_CONFIRM_RE.replace_all(&cleaned, " ").into_owned();
    // Collapse runs left by the inline strips, but preserve paragraph breaks.
    cleaned = WS_RUN_RE.replace_all(&cleaned, " ").into_owned();
    cleaned = BLANK_RUN_RE.replace_all(&cleaned, "\n\n").into_owned();
    (cleaned.trim().to_string(), (n_banner + n_calls) > 0)
}
