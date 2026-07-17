//! bss-orchestrator — the LLM agent brain. Rust port of
//! `orchestrator/bss_orchestrator` (linked in-process by the P6/P7 portals + CLI,
//! never over the network — the in-process doctrine, D3).
//!
//! This crate lands over several P5c slices. **Slice 1 (here)** is the hardest
//! architectural core, proven on the smallest real tool surface:
//! * [`agent::astream_once`] — the hand-rolled ReAct loop (replacing LangGraph's
//!   `create_react_agent`) + the guard stack (3-strike failure bail, identical-call
//!   stuck bail, destructive gating).
//! * [`chat_model`] — the `ChatModel` seam + the `MockChatModel` fixture player
//!   (the R2 acceptance harness).
//! * [`safety`] — `DESTRUCTIVE_TOOLS` + autonomy gating.
//! * [`tools`] — the registry + profile machinery + `clock.*` (the pilot family).
//!
//! **Following slices:** the OpenRouter client; the remaining ~106 tools
//! (schemars arg schemas per D5, profile by profile, `customer_self_serve` first);
//! the ownership trip-wire + chat caps; `SYSTEM_PROMPT`/customer-chat prompt. The
//! full fixture-corpus transcript-parity gate (R2) closes when the tools land.
#![forbid(unsafe_code)]

pub mod agent;
pub mod autonomy;
pub mod chat_caps;
pub mod chat_model;
pub mod config;
pub mod events;
pub mod llm;
pub mod ownership;
pub mod prompts;
pub mod safety;
pub mod tools;

pub use agent::{astream_once, astream_once_to, AgentConfig};
pub use autonomy::{read_autonomy_mode, AutonomyMisconfigured};
pub use chat_caps::{CapLimits, CapStatus, ChatCaps};
pub use chat_model::{AiTurn, ChatMessage, ChatModel, MockChatModel, Role, ToolCall, Usage};
pub use config::Settings;
pub use events::AgentEvent;
pub use llm::OpenRouterChatModel;
pub use ownership::{assert_owned_output, AgentOwnershipViolation, OWNERSHIP_PATHS};
pub use safety::{is_destructive, AutonomyMode, DESTRUCTIVE_TOOLS};
pub use tools::{ToolCtx, ToolError, ToolRegistry, ToolSpec};

/// A registry populated with the tool families ported so far (slice 1:
/// `clock.*`). Consumers that want a bespoke set build a [`ToolRegistry`]
/// directly and register the families they need.
pub fn default_registry() -> ToolRegistry {
    let mut registry = ToolRegistry::new();
    tools::clock::register_clock_tools(&mut registry);
    registry
}
