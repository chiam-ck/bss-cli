//! bss-cockpit — operator-cockpit Conversation store + config hot-reload +
//! system-prompt builder. Rust port of the `packages/bss-cockpit` **core**.
//!
//! v0.13 introduces a unified cockpit owned by the operator. The CLI REPL is the
//! canonical surface; the browser is a thin veneer over the same Postgres-backed
//! Conversation store. Either surface can write a turn; the other resumes
//! seamlessly. That single-store invariant is the entire point.
//!
//! Ported here (the pieces the orchestrator + both P6/P7 consumers need):
//! * [`conversation`] — the Conversation store (session/message/pending_destructive).
//! * [`config`] — `OPERATOR.md` + `settings.toml` loader with mtime hot-reload.
//! * [`prompts`] — `build_cockpit_prompt` + the verbatim `COCKPIT_INVARIANTS`.
//! * [`chrome_filter`] — `is_cockpit_chrome` (the transcript filter).
//!
//! **Deferred to P6/P7** (land with their browser/CLI consumers): the ASCII
//! renderers, `chrome_filter::strip_fake_propose`, `postprocess::*` (all three
//! use lookbehind/lookahead regexes → `fancy-regex` there), and the `settings.toml`
//! + branding writers (land with `bss-branding`).
#![forbid(unsafe_code)]

pub mod chrome_filter;
pub mod config;
pub mod conversation;
pub mod postprocess;
pub mod prompts;

pub use chrome_filter::{is_cockpit_chrome, ASSISTANT_CHROME_PREFIXES};
pub use config::{
    current, reset_cache, CockpitConfig, CockpitSettings, ConfigError, OPERATOR_ACTOR,
};
pub use conversation::{
    Conversation, ConversationError, ConversationMessage, ConversationStore, ConversationSummary,
    PendingDestructive,
};
pub use postprocess::{knowledge_called, strip_channel_markup, strip_reasoning_leakage};
pub use prompts::{build_cockpit_prompt, COCKPIT_INVARIANTS};
