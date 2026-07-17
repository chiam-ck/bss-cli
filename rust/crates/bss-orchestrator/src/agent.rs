//! The hand-rolled ReAct loop. Port of `astream_once` from
//! `orchestrator/bss_orchestrator/session.py` (LangGraph's `create_react_agent`
//! becomes an explicit loop: system prompt + messages → model → run tool_calls →
//! append tool results → repeat until the model stops calling tools).
//!
//! Yields the same [`AgentEvent`] sequence as the Python stream, including the
//! guard stack: the 3-strike failure bail, the identical-call stuck bail, the
//! destructive gating, and (P6b) the v0.12 output-ownership trip-wire. Chat caps
//! are the chat route's business — they gate *before* the turn and record after it,
//! so they never belong inside the loop.

use bss_clients::CrmClient;

use crate::chat_model::{ChatMessage, ChatModel, Role, ToolCall};
use crate::events::AgentEvent;
use crate::ownership::{assert_owned_output, record_violation};
use crate::safety::{gate_destructive, AutonomyMode, LoopState};
use crate::tools::{ToolCtx, ToolError, ToolRegistry};

/// v1.5 — bail when tool calls keep failing (the thrash pattern). Three
/// consecutive failures ends the turn; any success resets the counter.
pub const MAX_CONSECUTIVE_TOOL_FAILURES: u32 = 3;
/// v1.6.2 — bail when the same (tool, args, result) repeats: the agent is
/// replaying, not progressing.
pub const MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS: u32 = 3;

const RESULT_TRUNCATE: usize = 500;
const TRANSCRIPT_MAX_CHARS: usize = 32_000;

/// Compact `"error":"<CODE>"` fragments that flag a failure for the bail counter
/// (matches the compact JSON these observations serialize to).
const FAILURE_MARKERS: &[&str] = &[
    "\"error\":\"DESTRUCTIVE_OPERATION_BLOCKED\"",
    "\"error\":\"POLICY_VIOLATION\"",
    "\"error\":\"CLIENT_ERROR\"",
];

/// Configuration for one `astream_once` turn.
pub struct AgentConfig {
    pub allow_destructive: bool,
    pub autonomy: AutonomyMode,
    /// Profile name (`customer_self_serve` / `operator_cockpit`), or `None` for
    /// the full registered surface.
    pub tool_filter: Option<String>,
    pub system_prompt: String,
    /// Prior-turn transcript (cockpit `transcript_text()` format), or empty.
    pub transcript: String,
    pub ctx: ToolCtx,
    /// Model identifier reported in `TurnUsage` when the model surfaces no usage.
    pub model_name: String,
    /// CRM client used **only** to audit an ownership violation (`record_violation`
    /// logs an interaction on the actor's record). `None` skips the audit write; the
    /// violation still ends the turn. The chat route supplies it.
    pub crm_audit: Option<CrmClient>,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            allow_destructive: false,
            autonomy: AutonomyMode::Batched,
            tool_filter: None,
            system_prompt: String::new(),
            transcript: String::new(),
            ctx: ToolCtx::default(),
            model_name: String::new(),
            crm_audit: None,
        }
    }
}

/// Run one turn to completion, collecting the [`AgentEvent`] sequence.
///
/// A convenience wrapper over [`astream_once_to`] for callers that want the
/// whole turn at once (the CLI, tests, the fixture-parity harness). The SSE
/// portal route uses the streaming form.
pub async fn astream_once<M: ChatModel>(
    model: &mut M,
    registry: &ToolRegistry,
    prompt: &str,
    config: &AgentConfig,
) -> Vec<AgentEvent> {
    let mut events: Vec<AgentEvent> = Vec::new();
    {
        let mut sink = |e: AgentEvent| {
            events.push(e);
            true
        };
        astream_once_to(model, registry, prompt, config, &mut sink).await;
    }
    events
}

/// Run one turn, handing each [`AgentEvent`] to `sink` **as it happens** — the
/// streaming form the SSE chat route consumes (Python's `astream_once` is an
/// async generator; this is its Rust shape).
///
/// `sink` returns `false` when the consumer has gone away (e.g. the SSE receiver
/// was dropped because the browser disconnected), which ends the turn early.
/// That mirrors the `GeneratorExit` Python raises into the generator when the
/// `async for` body returns.
///
/// Note the three points where the Python *consumer* returns early — ownership
/// violation, other error, and final message — are all already terminal in the
/// loop itself, so the sink form emits exactly the same sequence as the
/// collecting form; no tool call can execute "past" a consumer's early return.
// `+ Send` so the whole turn can run in a spawned task — the SSE route drives it
// from `tokio::spawn` while the response streams.
pub async fn astream_once_to<M: ChatModel>(
    model: &mut M,
    registry: &ToolRegistry,
    prompt: &str,
    config: &AgentConfig,
    sink: &mut (dyn FnMut(AgentEvent) -> bool + Send),
) {
    /// Emit one event, bailing out of the turn if the consumer has gone away.
    macro_rules! emit {
        ($ev:expr) => {
            if !sink($ev) {
                return;
            }
        };
    }

    emit!(AgentEvent::PromptReceived {
        prompt: prompt.to_string(),
    });

    let specs = registry.surface(config.tool_filter.as_deref());

    let mut messages: Vec<ChatMessage> = Vec::new();
    messages.push(ChatMessage::system(config.system_prompt.clone()));
    messages.extend(messages_from_transcript(&config.transcript));
    messages.push(ChatMessage::user(prompt.to_string()));

    let mut loop_state = LoopState::default();
    let mut consecutive_failures: u32 = 0;
    let mut stuck = IdenticalCallTracker::default();
    let mut last_ai_text = String::new();
    let mut usage_in: i64 = 0;
    let mut usage_out: i64 = 0;
    let mut usage_model = String::new();

    loop {
        let turn = model.generate(&messages, &specs).await;

        if let Some(u) = &turn.usage {
            usage_in += u.input_tokens;
            usage_out += u.output_tokens;
            if !u.model.is_empty() {
                usage_model = u.model.clone();
            }
        }
        if !turn.content.is_empty() {
            last_ai_text = turn.content.clone();
        }

        messages.push(ChatMessage {
            role: Role::Assistant,
            content: turn.content.clone(),
            tool_calls: turn.tool_calls.clone(),
            tool_call_id: None,
            name: None,
        });

        if turn.tool_calls.is_empty() {
            break;
        }

        for tc in &turn.tool_calls {
            emit!(AgentEvent::ToolCallStarted {
                name: tc.name.clone(),
                args: tc.args.clone(),
                call_id: tc.id.clone(),
            });

            let (result_full, is_error) = execute_tool(registry, tc, config, &mut loop_state).await;

            messages.push(ChatMessage::tool(
                tc.name.clone(),
                tc.id.clone(),
                result_full.clone(),
            ));
            emit!(AgentEvent::ToolCallCompleted {
                name: tc.name.clone(),
                call_id: tc.id.clone(),
                result: truncate(&result_full),
                is_error,
                result_full: result_full.clone(),
            });

            // 3-strike failure bail.
            if is_failure_result(&result_full, is_error) {
                consecutive_failures += 1;
                if consecutive_failures >= MAX_CONSECUTIVE_TOOL_FAILURES {
                    emit!(AgentEvent::Error {
                        message: format!(
                            "agent_loop_bailout: {MAX_CONSECUTIVE_TOOL_FAILURES} \
                             consecutive tool failures (last tool: {:?}). The agent \
                             could not recover — send a fresh prompt or rephrase.",
                            tc.name
                        ),
                    });
                    return;
                }
            } else {
                consecutive_failures = 0;
            }

            // Identical-call stuck bail.
            let args_sig = tool_args_sig(&tc.args);
            if stuck.record(&tc.name, &args_sig, &result_full) {
                emit!(AgentEvent::Error {
                    message: format!(
                        "agent_loop_bailout: {MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS} \
                         identical calls to {:?} returned the identical result. The \
                         agent is stuck — rephrase, or give it a different identifier \
                         to work with.",
                        tc.name
                    ),
                });
                return;
            }

            // v0.12 — output ownership trip-wire. Skips error-status results (they
            // cannot carry customer-bound rows by definition) and runs only when an
            // actor is bound (the chat surface) so non-chat callers (CLI / scenario
            // / CSR) keep their full-surface behaviour.
            if !config.ctx.actor.is_empty() && !is_error && !tc.name.is_empty() {
                if let Err(v) = assert_owned_output(&tc.name, &result_full, &config.ctx.actor) {
                    // Best-effort audit trail — record_violation never raises. Then
                    // surface to the route handler, which renders the generic
                    // user-facing message.
                    if let Some(crm) = &config.crm_audit {
                        record_violation(
                            crm,
                            &config.ctx.actor,
                            &v.tool_name,
                            &v.path,
                            &v.found,
                            prompt,
                        )
                        .await;
                    }
                    emit!(AgentEvent::Error {
                        message: format!("AgentOwnershipViolation: {}", v.tool_name),
                    });
                    return;
                }
            }
        }
    }

    emit!(AgentEvent::TurnUsage {
        prompt_tok: usage_in,
        completion_tok: usage_out,
        model: if usage_model.is_empty() {
            config.model_name.clone()
        } else {
            usage_model
        },
    });
    emit!(AgentEvent::FinalMessage { text: last_ai_text });
}

/// Execute one tool call, applying destructive gating. Returns `(observation,
/// is_error)` — a blocked destructive is not an exception (`is_error=false`) but
/// its structured body still trips the failure counter, matching Python.
async fn execute_tool(
    registry: &ToolRegistry,
    tc: &ToolCall,
    config: &AgentConfig,
    loop_state: &mut LoopState,
) -> (String, bool) {
    if let Err(blocked) = gate_destructive(
        &tc.name,
        config.allow_destructive,
        config.autonomy,
        loop_state,
    ) {
        return (blocked.to_string(), false);
    }
    let Some(tool) = registry.get(&tc.name) else {
        let err = ToolError::Other {
            kind: "KeyError".to_string(),
            detail: format!("Unknown tool: {:?}", tc.name),
        };
        return (err.to_observation(), true);
    };
    // Thread the current transcript onto the ctx so `case.open_for_me` can hash +
    // store the conversation (Python reads `auth_context.current().transcript`).
    let mut ctx = config.ctx.clone();
    ctx.transcript = config.transcript.clone();
    match (tool.func)(tc.args.clone(), ctx).await {
        Ok(value) => (value.to_string(), false),
        Err(err) => (err.to_observation(), true),
    }
}

fn is_failure_result(content: &str, is_error: bool) -> bool {
    is_error || FAILURE_MARKERS.iter().any(|m| content.contains(m))
}

/// Key-order-independent signature of a tool call's args (serde_json `Value`
/// objects are already sorted — no `preserve_order` feature — matching Python's
/// `json.dumps(sort_keys=True)`).
fn tool_args_sig(args: &serde_json::Value) -> String {
    serde_json::to_string(args).unwrap_or_else(|_| "{}".to_string())
}

fn truncate(text: &str) -> String {
    if text.chars().count() <= RESULT_TRUNCATE {
        return text.to_string();
    }
    let head: String = text.chars().take(RESULT_TRUNCATE - 1).collect();
    format!("{head}…")
}

/// Counts consecutive identical `(tool, args, result)` triples. `record` returns
/// true when the run length reaches [`MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS`].
#[derive(Default)]
struct IdenticalCallTracker {
    last_key: Option<(String, String, String)>,
    repeats: u32,
}

impl IdenticalCallTracker {
    fn record(&mut self, name: &str, args_sig: &str, result: &str) -> bool {
        let key = (name.to_string(), args_sig.to_string(), result.to_string());
        if self.last_key.as_ref() == Some(&key) {
            self.repeats += 1;
        } else {
            self.last_key = Some(key);
            self.repeats = 1;
        }
        self.repeats >= MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS
    }
}

/// Parse a cockpit `transcript_text()` string into typed prior-turn messages.
/// Port of `session._messages_from_transcript`: `user:`→user, `assistant:`→
/// assistant, `tool[NAME]:`→a system "prior tool result" note (reconstructing a
/// real tool message would need a paired tool_call id). Robustness over fidelity —
/// malformed input yields `[]`.
pub fn messages_from_transcript(transcript: &str) -> Vec<ChatMessage> {
    if transcript.trim().is_empty() {
        return Vec::new();
    }
    let owned;
    let transcript = if transcript.chars().count() > TRANSCRIPT_MAX_CHARS {
        let suffix: String = transcript
            .chars()
            .skip(transcript.chars().count() - TRANSCRIPT_MAX_CHARS)
            .collect();
        owned = format!(
            "[…earlier turns elided to keep prompt within {TRANSCRIPT_MAX_CHARS} \
             chars; ask the operator to /reset if continuity matters…]\n\n{suffix}"
        );
        owned.as_str()
    } else {
        transcript
    };

    let mut out = Vec::new();
    for block in transcript.split("\n\n") {
        let block = block.trim_matches('\n');
        if block.is_empty() {
            continue;
        }
        let (head, body) = match block.split_once('\n') {
            Some((h, b)) => (h, b),
            None => (block, ""),
        };
        let head = head.trim().trim_end_matches(':');
        let body = body.trim();
        if head.is_empty() {
            continue;
        }
        if head == "user" {
            out.push(ChatMessage::user(body.to_string()));
        } else if head == "assistant" {
            out.push(ChatMessage::assistant(body.to_string()));
        } else if head.starts_with("tool") {
            let tool_name = head
                .strip_prefix("tool[")
                .and_then(|s| s.strip_suffix(']'))
                .unwrap_or("");
            let label = if tool_name.is_empty() {
                "prior tool result".to_string()
            } else {
                format!("prior tool result for {tool_name}")
            };
            out.push(ChatMessage::system(format!("({label}):\n{body}")));
        }
        // Unknown roles skipped silently.
    }
    out
}
