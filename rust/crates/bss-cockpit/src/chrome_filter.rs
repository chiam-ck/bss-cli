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
//! `strip_fake_propose` (the propose-banner scrubber) is consumed by the
//! portals/CLI post-processing, not by the store — it lands in P6/P7 with its
//! consumer (its lookbehind/lookahead regexes need `fancy-regex`).

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
