//! `bss ask "..."` — single-shot LLM dispatch. Port of
//! `cli/bss_cli/commands/ask.py` + `llm_runner.run_single_shot` →
//! `bss_orchestrator.session.ask_once`.
//!
//! Runs one question through a fresh agent over the full operator tool surface and
//! prints the reply. No session state is retained (the REPL, a later slice, keeps a
//! running transcript). The agent's tool calls attribute to the model
//! (`channel="llm"`, `actor="llm-<model>"`) — the port of Python's
//! `use_llm_context()` — not to the `cli-user` the direct command groups use.

use std::process::ExitCode;

use bss_orchestrator::{
    astream_once, prompts, AgentConfig, AgentEvent, AutonomyMode, OpenRouterChatModel, Settings,
    ToolCtx,
};
use clap::Args;

use crate::runtime::build_agent_registry;

#[derive(Args)]
pub struct AskArgs {
    /// Natural-language request.
    prompt: String,
    /// Permit destructive tool calls (the CLI equivalent of the cockpit `/confirm`).
    #[arg(long = "allow-destructive")]
    allow_destructive: bool,
}

pub async fn run(args: AskArgs) -> ExitCode {
    let settings = Settings::from_env();
    let actor = settings.llm_actor();

    // `use_llm_context()` — every downstream write this turn drives is audited as the
    // model, not the operator. Scopes the whole dispatch so `bss-clients` reads it.
    let ctx = bss_context::RequestCtx {
        request_id: bss_context::new_request_id(),
        actor: actor.clone(),
        channel: "llm".to_string(),
        ..Default::default()
    };
    bss_context::scope(ctx, dispatch(args, settings, actor)).await
}

async fn dispatch(args: AskArgs, settings: Settings, actor: String) -> ExitCode {
    // Missing `BSS_LLM_API_KEY` is the common cause — surface it cleanly, exit 1
    // (Python's `[red]LLM unavailable:[/] {e}`).
    let mut model = match OpenRouterChatModel::from_env() {
        Ok(m) => m,
        Err(e) => {
            eprintln!("LLM unavailable: {e}");
            return ExitCode::from(1);
        }
    };
    let registry = match build_agent_registry().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };

    let config = AgentConfig {
        allow_destructive: args.allow_destructive,
        autonomy: AutonomyMode::Batched,
        // Full surface — `bss ask` is the operator CLI, not the scoped chat.
        tool_filter: None,
        system_prompt: prompts::SYSTEM_PROMPT.to_string(),
        transcript: String::new(),
        ctx: ToolCtx {
            actor,
            channel: "llm".to_string(),
            tenant: "DEFAULT".to_string(),
            transcript: String::new(),
        },
        model_name: settings.llm_model.clone(),
        crm_audit: None,
    };

    let events = astream_once(&mut model, &registry, &args.prompt, &config).await;

    // Prefer the final assistant text (Python's `_last_ai_text`). An `Error` event
    // with no final message is the loop raising past its handlers — Python's
    // `graph.ainvoke` would raise, caught as the same exit-1 `LLM unavailable`.
    if let Some(text) = final_text(&events) {
        render(&text);
        ExitCode::SUCCESS
    } else if let Some(msg) = error_text(&events) {
        eprintln!("LLM unavailable: {msg}");
        ExitCode::from(1)
    } else {
        render("");
        ExitCode::SUCCESS
    }
}

fn final_text(events: &[AgentEvent]) -> Option<String> {
    events.iter().rev().find_map(|e| match e {
        AgentEvent::FinalMessage { text } => Some(text.clone()),
        _ => None,
    })
}

fn error_text(events: &[AgentEvent]) -> Option<String> {
    events.iter().rev().find_map(|e| match e {
        AgentEvent::Error { message } => Some(message.clone()),
        _ => None,
    })
}

/// Python renders `rich.Panel(reply, title="bss ai", border_style="green")`; the box
/// chrome is a documented CLI seam. An empty reply → the `(no reply)` note.
fn render(reply: &str) {
    if reply.trim().is_empty() {
        println!("(no reply)");
        return;
    }
    println!("bss ai");
    println!("{reply}");
}
