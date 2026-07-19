//! Shared post-processing of LLM final-message text before display. Port of
//! `bss_cockpit.postprocess`.
//!
//! Model output is hostile-by-default and leaks two artefacts the renderers
//! don't handle: Harmony/channel-format control tokens (`<channel|>`,
//! `assistantfinal`) and reasoning-step leakage (`<think>…</think>`, leading
//! `thought`). Both helpers are surface-agnostic — the REPL, the browser
//! cockpit, and `bss_portal_ui::chat_html` all call them so the surfaces can't
//! drift. `knowledge_called` is the seam consumers use to open the pipe-table
//! grammar gate for renderer-less `knowledge.*` prose (v0.20 carve-out).
//!
//! Uses `fancy-regex` for the one lookahead (`_RE_INLINE_THOUGHT_PREFIX`); the
//! others have no lookaround but share the engine for uniformity.

use std::sync::LazyLock;

use fancy_regex::Regex;
use serde_json::Value;

#[allow(clippy::expect_used)]
fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static postprocess regex compiles")
}

// Harmony / channel-format leakage: `<channel|>`, `<|channel|>`, `</channel>`,
// `<channel>`, and a bare `assistantfinal` marker. Case-insensitive, multiline
// so `^assistantfinal` matches per line.
static RE_CHANNEL_MARKUP: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?im)(?:<\|?channel\|?>|</\s*channel\s*>|^\s*assistantfinal\s*\n?)"));

// `<think>…</think>` / `<thinking>…</thinking>` — case-insensitive, dot-matches-
// newline, lazy.
static RE_THINK_BLOCK: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>"));
// Leading `thought`/`thinking` header line (start-of-text only, NOT multiline).
static RE_LEADING_THOUGHT: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)^\s*(?:thought|thinking)\s*[:\-]?\s*\n+"));
// Same-line `thought <content>` prefix — the trailing `\s+(?=\S)` requires a
// following non-space so it never eats "thoughtful".
static RE_INLINE_THOUGHT_PREFIX: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)^\s*(?:thought|thinking)\s+(?=\S)"));

/// Remove Harmony / channel-format control tokens. Idempotent; leading
/// whitespace introduced by the strip is trimmed (trailing preserved).
pub fn strip_channel_markup(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let cleaned = replace_all(&RE_CHANNEL_MARKUP, text);
    cleaned.trim_start().to_string()
}

/// Remove gemma-style reasoning leakage (`<think>…</think>` blocks, leading
/// `thought\n\n`, same-line `thought …` prefix). Idempotent; safe to chain with
/// [`strip_channel_markup`].
pub fn strip_reasoning_leakage(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let cleaned = replace_all(&RE_THINK_BLOCK, text);
    let cleaned = replace_first(&RE_LEADING_THOUGHT, &cleaned);
    let cleaned = replace_first(&RE_INLINE_THOUGHT_PREFIX, &cleaned);
    cleaned.trim_start().to_string()
}

/// True iff any `knowledge.*` tool fired this turn. Accepts the cockpit's
/// `captured_tool_calls` shape — a list of `{"name": "…"}` objects — but
/// tolerates plain string entries too.
pub fn knowledge_called(captured_tool_calls: &[Value]) -> bool {
    captured_tool_calls.iter().any(|entry| {
        let name = match entry {
            Value::String(s) => s.as_str(),
            Value::Object(_) => entry.get("name").and_then(Value::as_str).unwrap_or(""),
            _ => "",
        };
        name.starts_with("knowledge.")
    })
}

/// Replace every match with the empty string. `fancy-regex` matching is
/// fallible (backtracking); a match error leaves the text unchanged (the safe
/// no-op — these are display sanitizers, never a hard gate).
fn replace_all(re: &Regex, text: &str) -> String {
    match re.replace_all(text, "") {
        std::borrow::Cow::Borrowed(s) => s.to_string(),
        std::borrow::Cow::Owned(s) => s,
    }
}

/// Replace the first match only (Python `sub(..., count=1)`).
fn replace_first(re: &Regex, text: &str) -> String {
    match re.replacen(text, 1, "") {
        std::borrow::Cow::Borrowed(s) => s.to_string(),
        std::borrow::Cow::Owned(s) => s,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn knowledge_called_shapes() {
        assert!(!knowledge_called(&[]));
        assert!(knowledge_called(&[
            serde_json::json!({"name": "knowledge.search"})
        ]));
        assert!(knowledge_called(&[serde_json::json!("knowledge.get")]));
        assert!(!knowledge_called(&[
            serde_json::json!({"name": "catalog.list"})
        ]));
        assert!(!knowledge_called(&[serde_json::json!(42)]));
    }
}
