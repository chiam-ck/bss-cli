//! The reedline cockpit REPL — `bss` with no subcommand. Port of `cli/bss_cli/repl.py`.
//!
//! **s18b/s18c (this slice):** the bootstrap (Postgres-backed `Conversation` store,
//! fail-closed autonomy, operator config), the reedline read loop + branded banner
//! (`repl_ui`), the per-turn driver — the same decision chain the browser cockpit runs
//! (`portals/csr` `run_turn`), printed to the terminal instead of streamed over SSE,
//! reusing the shared `finalize_bubble` / guards / renderers — and the
//! session-management slash commands (`/sessions /new /switch /reset /focus`) on top of
//! `/help /confirm /exit /quit`. The intent commands (`/360 /ports /config /operator` +
//! the list-intent intercept) land in s18d.
//!
//! The agent's tool calls attribute to the **operator** (`channel="cli"`,
//! `actor=OPERATOR_ACTOR`, `service_identity="operator_cockpit"`) — a human runs the
//! cockpit, so writes are not `channel="llm"` (CLAUDE.md v0.5). Contrast `bss ask`,
//! which is single-shot and attributes to the model.

use std::borrow::Cow;
use std::collections::BTreeMap;
use std::process::ExitCode;
use std::sync::Arc;

use bss_cockpit::{
    build_cockpit_prompt, finalize_bubble, is_destructive, renderers, strip_channel_markup,
    strip_reasoning_leakage, BubbleCtx, Conversation, ConversationStore, DestructiveCall,
    OPERATOR_ACTOR,
};
use bss_context::{new_request_id, RequestCtx};
use bss_orchestrator::{
    astream_once, read_autonomy_mode, AgentConfig, AgentEvent, AutonomyMode, OpenRouterChatModel,
    Settings, ToolCtx, ToolRegistry,
};
use indexmap::IndexMap;
use reedline::{
    Prompt, PromptEditMode, PromptHistorySearch, PromptHistorySearchStatus, Reedline, Signal,
};
use serde_json::{json, Value};

use crate::repl_ui;
use crate::runtime::build_agent_registry;

/// Start the cockpit REPL. Blocks until the operator quits (`/exit`, `/quit`,
/// Ctrl-C, or Ctrl-D).
pub async fn run() -> ExitCode {
    let (store, model, autonomy) = match bootstrap().await {
        Ok(t) => t,
        Err(e) => {
            eprintln!("Cockpit unavailable: {e}");
            return ExitCode::from(1);
        }
    };

    let mut conv = match resolve_initial_conversation(&store).await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Cockpit unavailable: {e}");
            return ExitCode::from(1);
        }
    };

    // The agent's tool surface + chat model — built once, reused every turn.
    let registry = match build_agent_registry().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };
    let mut model_client = match OpenRouterChatModel::from_env() {
        Ok(m) => m,
        Err(e) => {
            eprintln!("LLM unavailable: {e}");
            return ExitCode::from(1);
        }
    };

    // v0.13: `--allow-destructive` flag support lands with the arg wiring; default off.
    let allow_destructive_default = false;

    print_banner(&model, &conv, allow_destructive_default);

    let mut editor = Reedline::create();
    loop {
        let prompt = ReplPrompt {
            sid8: last8(&conv.session_id),
        };
        match editor.read_line(&prompt) {
            Ok(Signal::Success(raw)) => {
                let line = raw.trim().to_string();
                if line.is_empty() {
                    continue;
                }
                if line == "/exit" || line == "/quit" {
                    break;
                }
                if line == "/help" {
                    print_help();
                    continue;
                }
                if line == "/confirm" {
                    // v1.5 — typing /confirm IS the trigger: drive a turn against a
                    // synthetic prompt so the model re-issues the staged destructive
                    // (the pending row was flipped to allow this turn).
                    let synthetic = "(operator typed /confirm — proceed with the prior \
                                     destructive proposal now; call the tool)";
                    drive_turn(
                        &mut model_client,
                        &registry,
                        &mut conv,
                        synthetic,
                        &model,
                        allow_destructive_default,
                        autonomy,
                    )
                    .await;
                    continue;
                }
                if line.starts_with('/') {
                    match handle_session_slash(&store, &mut conv, &line).await {
                        SlashOutcome::Handled => {}
                        SlashOutcome::Replaced(new) => {
                            conv = new;
                            print_banner(&model, &conv, allow_destructive_default);
                        }
                        SlashOutcome::Unknown => println!(
                            "'{line}' is not available yet — s18c ships /sessions /new /switch \
                             /reset /focus; /360 /ports /config /operator land in s18d."
                        ),
                    }
                    continue;
                }
                drive_turn(
                    &mut model_client,
                    &registry,
                    &mut conv,
                    &line,
                    &model,
                    allow_destructive_default,
                    autonomy,
                )
                .await;
            }
            Ok(Signal::CtrlC | Signal::CtrlD) => {
                println!();
                break;
            }
            Err(e) => {
                eprintln!("readline error: {e}");
                break;
            }
        }
    }

    ExitCode::SUCCESS
}

/// Wire the cockpit store + read the operator config. Port of
/// `_bootstrap_store_and_config`: `BSS_DB_URL` is required, `BSS_REPL_LLM_AUTONOMY`
/// is validated fail-closed (a bad value crashes the boot, never silently defaults),
/// and the model label comes from `settings.toml` (falling back to the orchestrator
/// default). `actor` is hardcoded to `OPERATOR_ACTOR` (v0.13.1 — single-operator).
async fn bootstrap() -> Result<(Arc<ConversationStore>, String, AutonomyMode), String> {
    let db_url = std::env::var("BSS_DB_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| {
            "BSS_DB_URL is not set. Source .env or export it before running the cockpit."
                .to_string()
        })?;

    // v1.5 — fail-closed autonomy validation (mirrors the cockpit portal lifespan).
    let autonomy = read_autonomy_mode().map_err(|e| e.to_string())?;

    let pool = bss_db::connect(&db_url)
        .await
        .map_err(|e| format!("cockpit store connect failed: {e}"))?;
    let store = Arc::new(ConversationStore::new(pool));

    let model = bss_cockpit::current(None)
        .ok()
        .and_then(|c| c.settings.llm.model)
        .filter(|m| !m.is_empty())
        .unwrap_or_else(|| Settings::from_env().llm_model);

    Ok((store, model, autonomy))
}

/// Resume the operator's most-recent active session, or open a fresh one. Port of
/// the default (no `--session`/`--new`) branch of `run_repl`.
async fn resolve_initial_conversation(store: &ConversationStore) -> Result<Conversation, String> {
    let recent = store
        .list_for(OPERATOR_ACTOR, true, 1)
        .await
        .map_err(|e| e.to_string())?;
    if let Some(summary) = recent.first() {
        store
            .resume(&summary.session_id)
            .await
            .map_err(|e| e.to_string())
    } else {
        let tenant = Settings::from_env().tenant_default;
        store
            .open(OPERATOR_ACTOR, None, None, false, &tenant)
            .await
            .map_err(|e| e.to_string())
    }
}

/// Stream one cockpit turn through `astream_once` and render it to the terminal.
///
/// Mirrors the browser cockpit's `run_turn` sink: capture tool calls, stage a
/// destructive proposal (blocked → pending; executed → wrap-up override), render
/// ASCII cards on `*.get`-shaped results, then hand the raw final message to the
/// shared `finalize_bubble` for the anti-mimicry / propose / citation overrides.
#[allow(clippy::too_many_arguments)]
async fn drive_turn(
    model_client: &mut OpenRouterChatModel,
    registry: &ToolRegistry,
    conv: &mut Conversation,
    line: &str,
    model_name: &str,
    allow_destructive_default: bool,
    autonomy: AutonomyMode,
) {
    // v0.20.2 — pull the prior transcript BEFORE appending the new user turn so the
    // model sees [prior history] + [new HumanMessage], not a doubled user turn.
    let prior_transcript = conv.transcript_text().await.unwrap_or_default();
    if let Err(e) = conv.append_user_turn(line).await {
        eprintln!("session error: {e}");
        return;
    }

    // A pending /confirm flips allow_destructive for this turn only.
    let pending = conv.consume_pending_destructive().await.ok().flatten();
    let allow_this_turn = allow_destructive_default || pending.is_some();

    let operator_md = bss_cockpit::current(None)
        .map(|c| c.operator_md)
        .unwrap_or_default();
    let mut extra: BTreeMap<String, String> = BTreeMap::new();
    extra.insert("model".to_string(), model_name.to_string());
    extra.insert("session_id".to_string(), conv.session_id.clone());
    let system_prompt = build_cockpit_prompt(
        &operator_md,
        conv.customer_focus.as_deref(),
        pending.as_ref(),
        Some(&extra),
    );

    let config = AgentConfig {
        allow_destructive: allow_this_turn,
        autonomy,
        tool_filter: Some("operator_cockpit".to_string()),
        system_prompt,
        transcript: prior_transcript,
        ctx: ToolCtx {
            actor: OPERATOR_ACTOR.to_string(),
            channel: "cli".to_string(),
            tenant: Settings::from_env().tenant_default,
            transcript: String::new(),
        },
        model_name: String::new(),
        crm_audit: None,
    };

    // Attribute the agent's downstream writes to the operator (not channel="llm").
    let ctx = RequestCtx {
        request_id: new_request_id(),
        actor: OPERATOR_ACTOR.to_string(),
        channel: "cli".to_string(),
        service_identity: "operator_cockpit".to_string(),
        ..Default::default()
    };
    let events = bss_context::scope(ctx, astream_once(model_client, registry, line, &config)).await;

    let mut captured: Vec<Value> = Vec::new();
    let mut last_proposal: Option<DestructiveCall> = None;
    let mut executed: Vec<DestructiveCall> = Vec::new();
    let mut tool_rows: Vec<(String, String)> = Vec::new();
    let mut final_text: Option<String> = None;
    let mut errored = false;
    let mut cards_shown = 0usize;

    for event in events {
        match event {
            AgentEvent::ToolCallStarted {
                name,
                args,
                call_id,
            } => {
                captured.push(json!({ "name": name, "args": args, "call_id": call_id }));
            }
            AgentEvent::ToolCallCompleted {
                name,
                call_id,
                result,
                result_full,
                ..
            } => {
                let raw = if result_full.is_empty() {
                    result
                } else {
                    result_full
                };
                if !name.is_empty() && is_destructive(&name) {
                    let args = captured
                        .iter()
                        .rev()
                        .find(|c| c.get("call_id").and_then(Value::as_str) == Some(&call_id))
                        .and_then(|c| c.get("args").cloned())
                        .unwrap_or_else(|| Value::Object(Default::default()));
                    let call = DestructiveCall {
                        name: name.clone(),
                        args,
                    };
                    if raw.contains("DESTRUCTIVE_OPERATION_BLOCKED") {
                        last_proposal = Some(call);
                    } else {
                        executed.push(call);
                    }
                }
                // Show an ASCII card ONLY when a renderer is registered (Python REPL's
                // `if card:`). Renderer-less tools (knowledge.*, …) return `None` → no
                // card, so `cards_shown` stays 0 and the model's prose answer is what
                // the operator sees — NOT the raw JSON. (The browser cockpit dumps raw
                // JSON as a <pre> because it always also shows the final bubble; the
                // REPL suppresses the bubble when a card rendered, so it must not treat
                // a renderer-less result as a card.)
                if let Some(rendered) = renderers::dispatch::render_tool_result(&name, &raw) {
                    if !rendered.is_empty() {
                        tool_rows.push((name.clone(), rendered.clone()));
                        println!("{rendered}");
                        cards_shown += 1;
                    }
                }
            }
            AgentEvent::FinalMessage { text } => {
                // Strip Harmony/channel markup then reasoning-channel leakage at the
                // boundary so neither display nor persistence carries the artefacts.
                final_text = Some(strip_reasoning_leakage(&strip_channel_markup(&text)));
            }
            AgentEvent::Error { .. } => errored = true,
            _ => {}
        }
    }

    for (name, body) in &tool_rows {
        let _ = conv.append_tool_turn(name, body).await;
    }

    if errored {
        let msg = "Sorry — something went wrong. Please try again.";
        println!("LLM error: {msg}");
        let _ = conv
            .append_assistant_turn(
                &format!("(error: {msg})"),
                tool_calls_json(&captured).as_ref(),
            )
            .await;
        return;
    }

    let Some(raw_final) = final_text else {
        // The loop ended without a terminal message — nothing to render.
        return;
    };

    let outcome = finalize_bubble(
        &raw_final,
        &BubbleCtx {
            captured_tool_calls: &captured,
            last_proposal: last_proposal.as_ref(),
            executed_destructive: &executed,
        },
    );
    if outcome.anti_mimicry_stall {
        println!(
            "⚠ no pending action — the model narrated a proposal in prose instead of \
             calling the tool. Nothing is staged for /confirm."
        );
    }
    if outcome.knowledge_hallucination {
        println!(
            "⚠ citation guard tripped — replacing un-cited handbook/doctrine claim with \
             safe fallback."
        );
    }

    let asst_id = conv
        .append_assistant_turn(&outcome.text, tool_calls_json(&captured).as_ref())
        .await
        .ok();

    // v1.5 — stage whenever a destructive proposal landed (regardless of the per-turn
    // allow flag): granular mode can re-gate the second destructive even mid-loop.
    if let (Some(p), Some(mid)) = (&last_proposal, asst_id) {
        if let Err(e) = conv
            .set_pending_destructive(&p.name, &args_map(&p.args), mid)
            .await
        {
            eprintln!("stage pending failed: {e}");
        } else {
            println!(
                "Pending /confirm for {} — type /confirm to authorise the next turn.",
                p.name
            );
        }
    }

    // A rendered card already answered the turn — skip the redundant prose panel.
    if cards_shown > 0 {
        return;
    }
    // The prose reply — Rich `Panel(Markdown(text), title="bss ai", border_style=
    // "green")`, ported to an ANSI panel with light markdown formatting.
    println!("{}", crate::repl_ui::reply_panel(&outcome.text));
}

/// `[{name, args, call_id}, …]` for the assistant turn's `tool_calls_json`, or
/// `None` when no tool fired.
fn tool_calls_json(captured: &[Value]) -> Option<Value> {
    if captured.is_empty() {
        None
    } else {
        Some(Value::Array(captured.to_vec()))
    }
}

/// The staged args as an insertion-ordered map (`set_pending_destructive` stores the
/// JSON so the text order round-trips — the P5b arg key-order seam).
fn args_map(args: &Value) -> IndexMap<String, Value> {
    match args.as_object() {
        Some(m) => m.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
        None => IndexMap::new(),
    }
}

/// The last 8 chars of a session id — the prompt's short handle.
fn last8(session_id: &str) -> String {
    let n = session_id.chars().count();
    session_id.chars().skip(n.saturating_sub(8)).collect()
}

/// The result of a session-management slash command. `Replaced` hands the loop a new
/// active conversation (and triggers a fresh banner); `Unknown` means the token isn't
/// an s18c command, so the loop prints the not-yet-available hint.
enum SlashOutcome {
    Handled,
    Replaced(Conversation),
    Unknown,
}

/// Handle the s18c session slash commands (`/sessions /new /switch /reset /focus`).
/// Port of the matching arms of `_handle_slash` in `cli/bss_cli/repl.py`.
async fn handle_session_slash(
    store: &ConversationStore,
    conv: &mut Conversation,
    line: &str,
) -> SlashOutcome {
    let mut parts = line.splitn(2, char::is_whitespace);
    let cmd = parts.next().unwrap_or("");
    let arg = parts.next().unwrap_or("").trim();

    match cmd {
        "/sessions" => {
            cmd_sessions(store, &conv.actor).await;
            SlashOutcome::Handled
        }
        "/new" => {
            if let Err(e) = conv.close().await {
                eprintln!("close failed: {e}");
            }
            let label = (!arg.is_empty()).then_some(arg);
            let tenant = Settings::from_env().tenant_default;
            match store
                .open(OPERATOR_ACTOR, label, None, false, &tenant)
                .await
            {
                Ok(new) => {
                    let suffix = label.map(|l| format!(" label={l:?}")).unwrap_or_default();
                    println!(
                        "{} {}{}",
                        repl_ui::paint("green", "Opened"),
                        repl_ui::paint("yellow", &new.session_id),
                        repl_ui::paint("dim", &suffix),
                    );
                    SlashOutcome::Replaced(new)
                }
                Err(e) => {
                    eprintln!("open failed: {e}");
                    SlashOutcome::Handled
                }
            }
        }
        "/switch" => {
            if arg.is_empty() {
                println!("/switch needs a session id (SES-...)");
                return SlashOutcome::Handled;
            }
            match store.resume(arg).await {
                Ok(new) => {
                    println!(
                        "{} {}",
                        repl_ui::paint("green", "Resumed"),
                        repl_ui::paint("yellow", &new.session_id),
                    );
                    print_prior_turns(&new).await;
                    SlashOutcome::Replaced(new)
                }
                Err(_) => {
                    println!("Session {arg} not found.");
                    SlashOutcome::Handled
                }
            }
        }
        "/reset" => {
            match conv.reset().await {
                Ok(()) => println!(
                    "{} messages on {}.",
                    repl_ui::paint("green", "Cleared"),
                    conv.session_id
                ),
                Err(e) => eprintln!("reset failed: {e}"),
            }
            SlashOutcome::Handled
        }
        "/focus" => {
            if arg.is_empty() || arg.eq_ignore_ascii_case("clear") {
                match conv.set_focus(None).await {
                    Ok(()) => println!("{}", repl_ui::paint("green", "Focus cleared.")),
                    Err(e) => eprintln!("focus failed: {e}"),
                }
            } else {
                match conv.set_focus(Some(arg)).await {
                    Ok(()) => println!(
                        "{} {}",
                        repl_ui::paint("green", "Focus pinned to"),
                        repl_ui::paint("yellow", arg),
                    ),
                    Err(e) => eprintln!("focus failed: {e}"),
                }
            }
            SlashOutcome::Handled
        }
        _ => SlashOutcome::Unknown,
    }
}

/// Render the operator's recent active cockpit sessions as a bordered table. Port of
/// `_cmd_sessions` (the Rich `Table`); columns are visible-width padded so the ANSI
/// color on the session id doesn't skew alignment.
async fn cmd_sessions(store: &ConversationStore, actor: &str) {
    let rows = match store.list_for(actor, true, 50).await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("sessions failed: {e}");
            return;
        }
    };
    if rows.is_empty() {
        println!("No active cockpit sessions for {actor}.");
        return;
    }
    let mut lines = Vec::with_capacity(rows.len() + 1);
    lines.push(repl_ui::paint(
        "dim",
        &format!(
            "{:<22} {:<14} {:<12} {:<16} {:>4}",
            "Session", "Label", "Focus", "Last active", "Msgs"
        ),
    ));
    for r in &rows {
        let sess = repl_ui::paint("yellow", &format!("{:<22}", clip(&r.session_id, 22)));
        let label = format!("{:<14}", clip(r.label.as_deref().unwrap_or("—"), 14));
        let focus = format!(
            "{:<12}",
            clip(r.customer_focus.as_deref().unwrap_or("—"), 12)
        );
        let last = r.last_active_at.format("%Y-%m-%d %H:%M");
        lines.push(format!(
            "{sess} {label} {focus} {last:<16} {:>4}",
            r.message_count
        ));
    }
    println!(
        "{}",
        repl_ui::framed(&format!("Cockpit sessions for {actor}"), lines, "green")
    );
}

/// Print the last few turns of a just-resumed session for context (Python renders a
/// dim-bordered `Panel` of the transcript tail).
async fn print_prior_turns(conv: &Conversation) {
    let Ok(transcript) = conv.transcript_text().await else {
        return;
    };
    if transcript.is_empty() {
        return;
    }
    let blocks: Vec<&str> = transcript.split("\n\n").collect();
    let tail = blocks[blocks.len().saturating_sub(5)..].join("\n\n");
    let lines: Vec<String> = tail.split('\n').map(str::to_string).collect();
    println!("{}", repl_ui::framed("prior turns (last 5)", lines, "dim"));
}

/// Truncate a plain string to `n` chars (session ids / labels are ASCII-ish; the
/// table columns are visible-width padded after this).
fn clip(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect()
    }
}

fn print_help() {
    println!("bss cockpit — slash commands (s18c):");
    println!("  /sessions         list your recent cockpit sessions");
    println!("  /new [label]      close current → open a fresh session");
    println!("  /switch SES-...   resume a specific session id");
    println!("  /reset            clear messages on the current session");
    println!("  /focus CUST-NNN   pin a customer for the system prompt (/focus clear to unset)");
    println!("  /confirm          authorise the last proposed destructive action");
    println!("  /help             this cheat sheet");
    println!("  /exit, /quit      leave the cockpit");
    println!("Anything else is a natural-language request to the operator agent.");
    println!("(/360 /ports /config /operator + the list-intent intercept land in s18d.)");
}

/// The banner shown at start and on session switch — the branded ANSI panel (ASCII
/// logo, tagline, meta, hints), read from operator branding per render. Port of
/// `_render_banner`.
fn print_banner(model: &str, conv: &Conversation, allow_destructive_default: bool) {
    let focus = conv.customer_focus.as_deref().unwrap_or("—");
    let brand = bss_branding::current(None);
    println!(
        "{}",
        crate::repl_ui::banner(
            &brand,
            OPERATOR_ACTOR,
            model,
            &conv.session_id,
            focus,
            allow_destructive_default,
        )
    );
}

/// The reedline prompt: `bss:<last-8-of-session>> ` (ANSI-coloured to match the
/// Python `prompt_toolkit` prompt shape).
struct ReplPrompt {
    sid8: String,
}

impl Prompt for ReplPrompt {
    fn render_prompt_left(&self) -> Cow<'_, str> {
        Cow::Owned(format!("bss:{}", self.sid8))
    }
    fn render_prompt_right(&self) -> Cow<'_, str> {
        Cow::Borrowed("")
    }
    fn render_prompt_indicator(&self, _edit_mode: PromptEditMode) -> Cow<'_, str> {
        Cow::Borrowed("> ")
    }
    fn render_prompt_multiline_indicator(&self) -> Cow<'_, str> {
        Cow::Borrowed("… ")
    }
    fn render_prompt_history_search_indicator(
        &self,
        history_search: PromptHistorySearch,
    ) -> Cow<'_, str> {
        let prefix = match history_search.status {
            PromptHistorySearchStatus::Passing => "",
            PromptHistorySearchStatus::Failing => "failing ",
        };
        Cow::Owned(format!(
            "({prefix}reverse-search: {}) ",
            history_search.term
        ))
    }
}
