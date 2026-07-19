//! LLM-mode step executor — runs `ask:` steps through the orchestrator. Port of
//! `cli/bss_cli/scenarios/llm_executor.py`.
//!
//! One `ask:` step = one turn of the OpenRouter-backed agent over the full tool
//! surface. The step runs under the `llm` channel context (Python's
//! `use_llm_context()`) so every downstream `bss-clients` call carries
//! `X-BSS-Channel: llm` + the model-derived actor — which the final `interaction.list`
//! assertions rely on. The outcome carries the last assistant text and, in call order,
//! every tool that actually executed (the `ToolCallCompleted` events — the canonical
//! "did the tool run?" signal, distinct from what the model merely *asked* to call).

use std::time::Duration;

use bss_context::{new_request_id, RequestCtx};
use bss_orchestrator::{
    astream_once, prompts, AgentConfig, AgentEvent, AutonomyMode, OpenRouterChatModel, Settings,
    ToolCtx, ToolRegistry,
};
use serde_json::Value;

use super::context::ScenarioContext;
use super::schema::AskStep;

/// Everything an `ask:` step produces that expectations evaluate against.
#[derive(Debug, Default, Clone)]
pub struct LlmStepOutcome {
    /// The last assistant reply — reported in the LLM trace once the report surfaces
    /// ask outcomes; the deterministic runner keys only on `tools_called` today.
    #[allow(dead_code)]
    pub final_message: String,
    pub tools_called: Vec<String>,
    /// Populated once a real final-state probe is wired (Task #6); the runner evaluates
    /// `expect_final_state` against it, matching the Python contract (empty today).
    pub final_state_probe: serde_json::Map<String, Value>,
}

/// Run an `ask:` step and return the outcome. `allow_llm` is false under `--no-llm`,
/// which is a hard error for an `ask:` step (Python's `LLMDisabled`).
pub async fn execute_ask_step(
    step: &AskStep,
    context: &ScenarioContext,
    registry: &ToolRegistry,
    allow_llm: bool,
) -> Result<LlmStepOutcome, String> {
    if !allow_llm {
        return Err(format!(
            "ask: step {:?} cannot run with --no-llm. Re-run without --no-llm or convert \
             to an action: step.",
            step.name
        ));
    }

    let settings = Settings::from_env();
    let actor = settings.llm_actor();
    let mut model = OpenRouterChatModel::from_env().map_err(|e| e.to_string())?;

    let prompt = match context.interpolate(&Value::String(step.ask.clone()))? {
        Value::String(s) => s,
        other => other.to_string(),
    };

    let config = AgentConfig {
        allow_destructive: false,
        autonomy: AutonomyMode::Batched,
        tool_filter: None,
        system_prompt: prompts::SYSTEM_PROMPT.to_string(),
        transcript: String::new(),
        ctx: ToolCtx {
            actor: actor.clone(),
            channel: "llm".to_string(),
            tenant: settings.tenant_default.clone(),
            transcript: String::new(),
        },
        model_name: settings.llm_model.clone(),
        crm_audit: None,
    };

    // `use_llm_context()` — override the ambient scenario scope for this turn so the
    // agent's writes audit as the model, not `scenario:<name>`.
    let llm_ctx = RequestCtx {
        request_id: new_request_id(),
        actor,
        channel: "llm".to_string(),
        ..Default::default()
    };
    let fut = bss_context::scope(
        llm_ctx,
        astream_once(&mut model, registry, &prompt, &config),
    );
    let events = tokio::time::timeout(Duration::from_secs_f64(step.timeout_seconds), fut)
        .await
        .map_err(|_| {
            format!(
                "LLM turn timed out after {}s (step {:?})",
                step.timeout_seconds, step.name
            )
        })?;

    if let Some(msg) = events.iter().rev().find_map(|e| match e {
        AgentEvent::Error { message } => Some(message.clone()),
        _ => None,
    }) {
        // Only surface the error when no final text landed — the loop can emit a
        // recoverable error mid-run yet still finish with an answer.
        if !events
            .iter()
            .any(|e| matches!(e, AgentEvent::FinalMessage { .. }))
        {
            return Err(msg);
        }
    }

    let final_message = events
        .iter()
        .rev()
        .find_map(|e| match e {
            AgentEvent::FinalMessage { text } => Some(text.clone()),
            _ => None,
        })
        .unwrap_or_default();
    let tools_called = events
        .iter()
        .filter_map(|e| match e {
            AgentEvent::ToolCallCompleted { name, .. } if !name.is_empty() => Some(name.clone()),
            _ => None,
        })
        .collect();

    Ok(LlmStepOutcome {
        final_message,
        tools_called,
        final_state_probe: serde_json::Map::new(),
    })
}
