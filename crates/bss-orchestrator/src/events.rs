//! Streaming event types. Port of the `AgentEvent*` dataclasses in
//! `orchestrator/bss_orchestrator/session.py`.
//!
//! The agent loop yields these as it runs so portals can render tool-call logs
//! live over SSE and the chat route can record per-turn cost.

use std::collections::BTreeMap;

use serde_json::Value;

/// One event emitted by the agent loop.
#[derive(Debug, Clone, PartialEq)]
pub enum AgentEvent {
    /// Emitted once at the start with the caller's raw prompt.
    PromptReceived { prompt: String },
    /// The LLM decided to invoke a tool. Emitted before the tool runs.
    ToolCallStarted {
        name: String,
        args: Value,
        call_id: String,
    },
    /// The tool's result came back. `result` is the truncated string repr;
    /// `result_full` is untruncated (consumers that parse the JSON read it).
    ToolCallCompleted {
        name: String,
        call_id: String,
        result: String,
        is_error: bool,
        result_full: String,
    },
    /// Last AI message with no further tool calls — the end of the turn.
    FinalMessage { text: String },
    /// The loop or a tool raised past all handlers, or a guard bailed the turn.
    Error { message: String },
    /// Per-turn token counts, emitted once before `FinalMessage` so the chat
    /// route can record cost. `model` is the identifier used this turn.
    TurnUsage {
        prompt_tok: i64,
        completion_tok: i64,
        model: String,
    },
}

impl AgentEvent {
    /// A stable JSON projection used by the golden transcript tests. IDs that
    /// vary run-to-run (mock call ids) are the caller's concern to normalize.
    pub fn to_value(&self) -> Value {
        match self {
            AgentEvent::PromptReceived { prompt } => {
                json_event("prompt_received", [("prompt", Value::from(prompt.clone()))])
            }
            AgentEvent::ToolCallStarted {
                name,
                args,
                call_id,
            } => json_event(
                "tool_call_started",
                [
                    ("name", Value::from(name.clone())),
                    ("args", args.clone()),
                    ("call_id", Value::from(call_id.clone())),
                ],
            ),
            AgentEvent::ToolCallCompleted {
                name,
                call_id,
                result,
                is_error,
                result_full,
            } => json_event(
                "tool_call_completed",
                [
                    ("name", Value::from(name.clone())),
                    ("call_id", Value::from(call_id.clone())),
                    ("result", Value::from(result.clone())),
                    ("is_error", Value::from(*is_error)),
                    ("result_full", Value::from(result_full.clone())),
                ],
            ),
            AgentEvent::FinalMessage { text } => {
                json_event("final_message", [("text", Value::from(text.clone()))])
            }
            AgentEvent::Error { message } => {
                json_event("error", [("message", Value::from(message.clone()))])
            }
            AgentEvent::TurnUsage {
                prompt_tok,
                completion_tok,
                model,
            } => json_event(
                "turn_usage",
                [
                    ("prompt_tok", Value::from(*prompt_tok)),
                    ("completion_tok", Value::from(*completion_tok)),
                    ("model", Value::from(model.clone())),
                ],
            ),
        }
    }
}

fn json_event<const N: usize>(kind: &str, fields: [(&str, Value); N]) -> Value {
    let mut map = BTreeMap::new();
    map.insert("event".to_string(), Value::from(kind));
    for (k, v) in fields {
        map.insert(k.to_string(), v);
    }
    Value::Object(map.into_iter().collect())
}
