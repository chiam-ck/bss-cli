//! The operator cockpit routes. Port of `bss_csr.routes.cockpit`'s handlers.
//!
//! ```text
//! GET  /                              → sessions index
//! GET  /cockpit/{session_id}          → chat thread page
//! POST /cockpit/{session_id}/turn     → enqueue a user message
//! GET  /cockpit/{session_id}/events   → SSE stream (drives the turn)
//! POST /cockpit/{session_id}/reset    → conversation.reset()
//! POST /cockpit/{session_id}/confirm  → flip next turn destructive
//! POST /cockpit/{session_id}/focus    → set/clear customer focus
//! POST /cockpit/new                   → 303 → /cockpit/<new id>
//! ```
//!
//! **No login route, no inbound auth middleware.** The cockpit runs
//! single-operator-by-design behind a secure perimeter; `actor` comes from
//! `.bss-cli/settings.toml` via `bss_cockpit::config::current()` (DECISIONS
//! 2026-05-01).
//!
//! **Doctrine:** this is the only orchestrator-mediated route module in the CSR
//! portal — the guard `rg 'astream_once' portals/csr/.../routes/` must match this
//! file only.
//!
//! The decision logic lives in [`crate::turn`] / [`crate::bubble`] /
//! [`crate::guards`]; this module is the wiring.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_cockpit::{build_cockpit_prompt, knowledge_called, Conversation, OPERATOR_ACTOR};
use bss_orchestrator::{astream_once_to, AgentConfig, AgentEvent};
use bss_portal_ui::sse::{format_frame, status_html};
use bss_portal_ui::{render_assistant_bubble, render_chat_markdown, strip_reasoning_leakage};
use minijinja::context;
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::broadcast;

use crate::bubble::{finalize_bubble, BubbleCtx, DestructiveCall};
use crate::guards::is_destructive;
use crate::inflight::{heartbeat_frame, HEARTBEAT_SECONDS, RELOAD_FRAME_HTML};
use crate::routes::render;
use crate::sessions::{first_user_message_title, group_rows, humanize_time, SessionRow};
use crate::tool_row::render_tool_row_as_pre;
use crate::turn::{plan_turn, TurnPlan};
use crate::AppState;

const SSE_MIME: &str = "text/event-stream";

fn sse_response(body: Body) -> Response {
    let mut resp = Response::new(body);
    let h = resp.headers_mut();
    #[allow(clippy::expect_used)]
    {
        h.insert(
            axum::http::header::CONTENT_TYPE,
            SSE_MIME.parse().expect("static"),
        );
        h.insert(
            axum::http::header::CACHE_CONTROL,
            "no-cache".parse().expect("static"),
        );
        h.insert("X-Accel-Buffering", "no".parse().expect("static"));
        h.insert(
            axum::http::header::CONNECTION,
            "keep-alive".parse().expect("static"),
        );
    }
    resp
}

/// One-shot SSE response built from a fixed frame list.
fn sse_frames(frames: Vec<Vec<u8>>) -> Response {
    let stream = futures_util::stream::iter(
        frames
            .into_iter()
            .map(Ok::<Vec<u8>, Infallible>)
            .collect::<Vec<_>>(),
    );
    sse_response(Body::from_stream(stream))
}

#[allow(clippy::result_large_err)]
pub(crate) fn store(state: &AppState) -> Result<Arc<bss_cockpit::ConversationStore>, Response> {
    state.store.clone().ok_or_else(|| {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            "conversation store unavailable",
        )
            .into_response()
    })
}

/// `Conversation::resume`, mapping a missing session to 404 (Python raises
/// `LookupError` → `HTTPException(404)`).
#[allow(clippy::result_large_err)]
async fn resume(state: &AppState, session_id: &str) -> Result<Conversation, Response> {
    let s = store(state)?;
    s.resume(session_id)
        .await
        .map_err(|e| (StatusCode::NOT_FOUND, format!("{e}")).into_response())
}

pub(crate) fn model_label(state: &AppState) -> String {
    let cfg = bss_cockpit::current(None);
    let _ = state;
    cfg.as_ref()
        .ok()
        .and_then(|c| c.settings.llm.model.clone())
        .filter(|m| !m.is_empty())
        .unwrap_or_else(|| "(env default)".to_string())
}

// ── GET / — sessions index ───────────────────────────────────────────

pub async fn index(State(state): State<AppState>) -> Response {
    let s = match store(&state) {
        Ok(s) => s,
        Err(r) => return r,
    };
    let rows = s
        .list_for(OPERATOR_ACTOR, false, 50)
        .await
        .unwrap_or_default();
    let now = bss_clock::now();

    // Resolve customer focus → name in a single pass, best-effort (falls back to
    // the CUST-id if the lookup fails).
    let mut focus_names: BTreeMap<String, String> = BTreeMap::new();
    if let Some(clients) = &state.clients {
        let mut ids: Vec<String> = rows
            .iter()
            .filter_map(|r| r.customer_focus.clone())
            .collect();
        ids.sort();
        ids.dedup();
        for id in ids {
            let name = match clients.crm.get_customer(&id).await {
                Ok(c) => individual_name(&c).unwrap_or_else(|| id.clone()),
                Err(_) => id.clone(),
            };
            focus_names.insert(id, name);
        }
    }

    let mut resolved: Vec<(SessionRow, chrono::DateTime<chrono::Utc>)> = Vec::new();
    for r in &rows {
        // The first user message is the human-friendly title.
        let title = match s.resume(&r.session_id).await {
            Ok(conv) => {
                let transcript = conv.transcript_text().await.unwrap_or_default();
                first_user_message_title(&transcript, r.label.as_deref())
            }
            Err(_) => r
                .label
                .clone()
                .unwrap_or_else(|| "(empty conversation)".to_string()),
        };
        resolved.push((
            SessionRow {
                session_id: r.session_id.clone(),
                title,
                focus_label: r
                    .customer_focus
                    .as_ref()
                    .and_then(|c| focus_names.get(c).cloned()),
                last_active_human: humanize_time(r.last_active_at, now),
                message_count: r.message_count,
            },
            r.last_active_at,
        ));
    }

    render(
        &state,
        "sessions_index.html",
        context! {
            active_page => "sessions",
            model => model_label(&state),
            grouped_sessions => group_rows(resolved, now),
        },
    )
}

/// `givenName familyName` off a TMF629 customer, or `name`, or `None`.
fn individual_name(c: &Value) -> Option<String> {
    let ind = c.get("individual");
    let parts: Vec<&str> = ["givenName", "familyName"]
        .iter()
        .filter_map(|k| ind.and_then(|i| i.get(*k)).and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .collect();
    let joined = parts.join(" ").trim().to_string();
    if !joined.is_empty() {
        return Some(joined);
    }
    c.get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

// ── POST /cockpit/new ────────────────────────────────────────────────

pub async fn new_session(State(state): State<AppState>) -> Response {
    let s = match store(&state) {
        Ok(s) => s,
        Err(r) => return r,
    };
    match s.open(OPERATOR_ACTOR, None, None, false, &tenant()).await {
        Ok(conv) => Redirect::to(&format!("/cockpit/{}", conv.session_id)).into_response(),
        Err(e) => {
            tracing::error!(error = %e, "cockpit.new_session_failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not open a session",
            )
                .into_response()
        }
    }
}

// ── GET /cockpit/{id} — the thread page ──────────────────────────────

#[derive(Deserialize, Default)]
pub struct ThreadQuery {
    #[serde(default)]
    turn: String,
    #[serde(default)]
    draft: String,
}

pub async fn thread(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
    Query(q): Query<ThreadQuery>,
) -> Response {
    let conv = match resume(&state, &session_id).await {
        Ok(c) => c,
        Err(r) => return r,
    };

    // v0.13.1 — read the STRUCTURED rows. The prior path serialized to text and
    // re-parsed on \n\n boundaries, which truncated assistant bubbles whose body
    // contained blank lines (a paragraph break + a markdown table). Nothing on
    // this path parses transcript text.
    let rows = conv.list_messages().await.unwrap_or_default();
    let mut blocks: Vec<Value> = Vec::new();
    // v0.20.1 — track tool calls since the most recent user turn so a rehydrated
    // assistant bubble renders pipe tables iff a knowledge.* tool fired in the
    // SAME turn. Mirrors the live SSE path's `allow_tables=knowledge_called(...)`.
    let mut turn_tools: Vec<Value> = Vec::new();
    for row in &rows {
        let tool_name = row.tool_name.clone().unwrap_or_default();
        match row.role.as_str() {
            "user" => turn_tools.clear(),
            "tool" => turn_tools.push(serde_json::json!({ "name": tool_name })),
            _ => {}
        }
        let body_html = match row.role.as_str() {
            "assistant" => render_chat_markdown(&row.content, knowledge_called(&turn_tools)),
            // The same helper the SSE stream uses — one shape on both wires.
            "tool" => render_tool_row_as_pre(&tool_name, &row.content),
            _ => row.content.clone(),
        };
        blocks.push(serde_json::json!({
            "role": row.role,
            "tool_name": tool_name,
            "body": row.content,
            "body_html": body_html,
        }));
    }

    // Thread title from the first user message — a direct lookup over the
    // structured rows, no parsing.
    let thread_title = rows
        .iter()
        .find(|r| r.role == "user")
        .map(|r| {
            if r.content.chars().count() > 80 {
                format!("{}…", r.content.chars().take(77).collect::<String>())
            } else {
                r.content.clone()
            }
        })
        .or_else(|| conv.label.clone())
        .unwrap_or_else(|| "(empty conversation)".to_string());

    let focus_label = match (&conv.customer_focus, &state.clients) {
        (Some(cid), Some(clients)) => Some(match clients.crm.get_customer(cid).await {
            Ok(c) => individual_name(&c).unwrap_or_else(|| cid.clone()),
            Err(_) => cid.clone(),
        }),
        (Some(cid), None) => Some(cid.clone()),
        _ => None,
    };

    let mut resp = render(
        &state,
        "cockpit_thread.html",
        context! {
            active_page => "thread",
            model => model_label(&state),
            conversation => context! {
                session_id => conv.session_id.clone(),
                customer_focus => conv.customer_focus.clone(),
                label => conv.label.clone(),
            },
            thread_title => thread_title,
            focus_label => focus_label,
            transcript_blocks => blocks,
            stream_session_id => q.turn,
            // v1.6 — CRM screens hand off with a drafted message; it lands in
            // the compose box, never auto-sends.
            draft => q.draft.chars().take(2000).collect::<String>(),
        },
    );
    // v1.6.1 — Safari caches GETs; a cached thread page is a stale transcript
    // (the "have to nudge it" symptom amplifier).
    #[allow(clippy::expect_used)]
    resp.headers_mut().insert(
        axum::http::header::CACHE_CONTROL,
        "no-store".parse().expect("static"),
    );
    resp
}

// ── POST /cockpit/{id}/turn ──────────────────────────────────────────

#[derive(Deserialize)]
pub struct TurnForm {
    message: String,
}

pub async fn post_turn(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
    Form(form): Form<TurnForm>,
) -> Response {
    let mut conv = match resume(&state, &session_id).await {
        Ok(c) => c,
        Err(r) => return r,
    };
    let text = form.message.trim();
    if text.is_empty() {
        return Redirect::to(&format!("/cockpit/{session_id}")).into_response();
    }
    if let Err(e) = conv.append_user_turn(text).await {
        tracing::error!(error = %e, "cockpit.append_user_failed");
        return (StatusCode::INTERNAL_SERVER_ERROR, "could not append").into_response();
    }
    // The events endpoint reads the latest user message off the conversation
    // row; no separate turn store needed. `?turn=1` tells the template to attach
    // the SSE connection on the next render.
    Redirect::to(&format!("/cockpit/{session_id}?turn=1")).into_response()
}

// ── POST /cockpit/{id}/reset ─────────────────────────────────────────

pub async fn post_reset(State(state): State<AppState>, Path(session_id): Path<String>) -> Response {
    let conv = match resume(&state, &session_id).await {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(e) = conv.reset().await {
        tracing::error!(error = %e, "cockpit.reset_failed");
    }
    Redirect::to(&format!("/cockpit/{session_id}")).into_response()
}

// ── POST /cockpit/{id}/confirm ───────────────────────────────────────

/// A no-op except as a marker. The next turn's SSE handler consumes any
/// `pending_destructive` row; this POST exists so the browser has a button to
/// press, for parity with the REPL's `/confirm` slash command.
pub async fn post_confirm(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> Response {
    // 404 if missing — same as the oracle.
    if let Err(r) = resume(&state, &session_id).await {
        return r;
    }
    Redirect::to(&format!("/cockpit/{session_id}")).into_response()
}

// ── POST /cockpit/{id}/focus ─────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct FocusForm {
    #[serde(default)]
    customer_id: String,
}

pub async fn post_focus(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
    Form(form): Form<FocusForm>,
) -> Response {
    let mut conv = match resume(&state, &session_id).await {
        Ok(c) => c,
        Err(r) => return r,
    };
    let trimmed = form.customer_id.trim();
    let focus = if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    };
    if let Err(e) = conv.set_focus(focus).await {
        tracing::error!(error = %e, "cockpit.set_focus_failed");
    }
    Redirect::to(&format!("/cockpit/{session_id}")).into_response()
}

// ── GET /cockpit/{id}/events — the SSE turn stream ───────────────────

pub async fn events(State(state): State<AppState>, Path(session_id): Path<String>) -> Response {
    let conv = match resume(&state, &session_id).await {
        Ok(c) => c,
        Err(r) => return r,
    };

    // v1.6.1 — a turn is already running for this session (EventSource
    // reconnected after a dropped stream, a second tab, …): attach as an
    // OBSERVER instead of double-driving the agent.
    if let Some(rx) = state.inflight.observe(&session_id) {
        return sse_response(Body::from_stream(observe_stream(rx)));
    }

    let transcript = conv.transcript_text().await.unwrap_or_default();
    let pending = conv.consume_pending_destructive().await.unwrap_or(None);

    let plan = plan_turn(&transcript, pending.is_some());
    let drive = match plan {
        // Page reload after the turn already streamed.
        TurnPlan::Nothing => return sse_frames(vec![format_frame("status", &status_html("done"))]),
        // v1.6.1 — the page that opened this stream may predate the answer (Safari
        // dropped the original stream; the detached task finished anyway). The
        // reload marker lets a stale page refresh itself; a page that already
        // streamed the turn ignores it.
        TurnPlan::Replay => {
            return sse_frames(vec![
                format_frame("message", RELOAD_FRAME_HTML),
                format_frame("status", &status_html("done")),
            ])
        }
        TurnPlan::Drive(d) => *d,
    };

    let (Some(registry), Some(clients)) = (&state.chat_registry, &state.clients) else {
        tracing::error!("cockpit.registry_unavailable");
        return sse_frames(vec![format_frame("status", &status_html("error"))]);
    };

    // Focus snapshot — mirror v0.5 agent_bridge: when focus is pinned, surface a
    // customer/sub snapshot so the LLM can act in one shot without discovery
    // rounds (some models leak tool-call markup as text when starved of context).
    let mut extra: BTreeMap<String, String> = BTreeMap::new();
    extra.insert("model".to_string(), model_label(&state));
    extra.insert("session_id".to_string(), conv.session_id.clone());
    if let Some(cid) = &conv.customer_focus {
        if let Some(snap) = load_focus_snapshot(&state, cid).await {
            extra.insert("focus_snapshot".to_string(), snap);
        }
    }

    let operator_md = bss_cockpit::current(None)
        .map(|c| c.operator_md.clone())
        .unwrap_or_default();
    let system_prompt = build_cockpit_prompt(
        &operator_md,
        conv.customer_focus.as_deref(),
        pending.as_ref(),
        Some(&extra),
    );

    let (tx, rx) = broadcast::channel::<Vec<u8>>(256);
    let handle = tokio::spawn(run_turn(
        state.clone(),
        session_id.clone(),
        conv,
        Box::new(drive),
        system_prompt,
        registry.clone(),
        clients.clone(),
        tx.clone(),
    ));
    state.inflight.insert(&session_id, handle, tx);

    sse_response(Body::from_stream(observe_stream(rx)))
}

/// Forward frames to one client, heart-beating through silences.
///
/// Lagged receivers (a slow client on a chatty turn) skip ahead rather than
/// erroring — the transcript is persisted regardless, and the reload marker at
/// the end brings the page back into sync.
fn observe_stream(
    mut rx: broadcast::Receiver<Vec<u8>>,
) -> impl futures_util::Stream<Item = Result<Vec<u8>, Infallible>> {
    futures_util::stream::unfold(Some(rx_state(&mut rx)), |st| async move {
        let mut rx = st?;
        loop {
            match tokio::time::timeout(Duration::from_secs(HEARTBEAT_SECONDS), rx.recv()).await {
                // Silence → an observable heartbeat so a client watchdog can tell
                // a healthy quiet stream from a dead socket.
                Err(_) => return Some((Ok(heartbeat_frame()), Some(rx))),
                Ok(Ok(frame)) => return Some((Ok(frame), Some(rx))),
                // The turn finished and dropped the sender.
                Ok(Err(broadcast::error::RecvError::Closed)) => return None,
                Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            }
        }
    })
}

fn rx_state(rx: &mut broadcast::Receiver<Vec<u8>>) -> broadcast::Receiver<Vec<u8>> {
    rx.resubscribe()
}

/// Drive one cockpit turn to completion. **Detached**: every persistence beat
/// (tool rows, the assistant bubble, the pending_destructive row) happens here,
/// so a dropped SSE connection can no longer cancel the turn — the client merely
/// stops watching.
#[allow(clippy::too_many_arguments)]
async fn run_turn(
    state: AppState,
    session_id: String,
    mut conv: Conversation,
    drive: Box<crate::turn::DriveTurn>,
    system_prompt: String,
    registry: Arc<bss_orchestrator::ToolRegistry>,
    clients: Arc<crate::clients::CockpitClients>,
    tx: broadcast::Sender<Vec<u8>>,
) {
    let _ = tx.send(format_frame("status", &status_html("live")));

    let mut model = match bss_orchestrator::OpenRouterChatModel::from_env() {
        Ok(m) => m,
        Err(e) => {
            tracing::error!(error = %e, "cockpit.model_unavailable");
            let _ = tx.send(format_frame("status", &status_html("error")));
            state.inflight.remove(&session_id);
            return;
        }
    };

    let config = AgentConfig {
        allow_destructive: drive.allow_destructive,
        autonomy: state.autonomy_mode,
        tool_filter: Some("operator_cockpit".to_string()),
        system_prompt,
        transcript: drive.prior_transcript.clone(),
        ctx: bss_orchestrator::ToolCtx {
            actor: OPERATOR_ACTOR.to_string(),
            channel: "portal-csr".to_string(),
            tenant: state.settings.env.clone(),
            transcript: String::new(),
        },
        model_name: String::new(),
        crm_audit: Some(clients.crm.clone()),
    };

    // Collected inside the sink, applied after the loop (the sink is sync).
    let mut captured: Vec<Value> = Vec::new();
    let mut last_proposal: Option<DestructiveCall> = None;
    let mut executed: Vec<DestructiveCall> = Vec::new();
    let mut final_text: Option<String> = None;
    let mut errored = false;
    // Tool rows to persist + emit, in order.
    let mut tool_rows: Vec<(String, String)> = Vec::new();

    {
        let tx = &tx;
        let captured = &mut captured;
        let last_proposal = &mut last_proposal;
        let executed = &mut executed;
        let final_text = &mut final_text;
        let errored = &mut errored;
        let tool_rows = &mut tool_rows;

        let mut sink = |event: AgentEvent| -> bool {
            match event {
                AgentEvent::ToolCallStarted {
                    name,
                    args,
                    call_id,
                } => {
                    captured.push(serde_json::json!({
                        "name": name, "args": args, "call_id": call_id
                    }));
                    let _ = tx.send(format_frame(
                        "message",
                        &bss_portal_ui::render_tool_pill(&name),
                    ));
                    true
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
                    // v1.5 — when a destructive tool's result IS the structured
                    // DESTRUCTIVE_OPERATION_BLOCKED response (blocked at the gate,
                    // OR granular mode re-gating after a prior destructive already
                    // fired this loop), capture it as the proposal to stage. This
                    // is what lets the SECOND destructive in a granular compound
                    // action surface as a fresh /confirm even though the turn
                    // began with allow_destructive=true.
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
                            *last_proposal = Some(call);
                        } else {
                            executed.push(call);
                        }
                    }
                    // v0.19 Option-1 doctrine: a registered renderer produces the
                    // ASCII card; no renderer → the raw JSON verbatim. The LLM is
                    // forbidden from reformatting it, and this path makes any
                    // markdown table coming back as the bubble a visible bug.
                    let rendered =
                        bss_cockpit::renderers::dispatch::render_tool_result(&name, &raw)
                            .unwrap_or(raw);
                    if !rendered.is_empty() {
                        tool_rows.push((name.clone(), rendered.clone()));
                        // Suppress the duplicate pill — ToolCallStarted emitted one.
                        let _ = tx.send(format_frame(
                            "message",
                            &render_tool_row_as_pre(&name, &rendered),
                        ));
                    }
                    true
                }
                AgentEvent::Error { .. } => {
                    *errored = true;
                    false
                }
                AgentEvent::FinalMessage { text } => {
                    *final_text = Some(strip_reasoning_leakage(&text));
                    false
                }
                _ => true,
            }
        };

        astream_once_to(
            &mut model,
            &registry,
            &drive.user_message,
            &config,
            &mut sink,
        )
        .await;
    }

    // Persist tool rows (the sink is sync; the store is async).
    for (name, body) in &tool_rows {
        if let Err(e) = conv.append_tool_turn(name, body).await {
            tracing::error!(error = %e, "cockpit.append_tool_failed");
        }
    }

    if errored {
        let text = "Sorry — something went wrong. Please try again.";
        let _ = conv
            .append_assistant_turn(text, tool_calls_json(&captured).as_ref())
            .await;
        let _ = tx.send(format_frame(
            "message",
            &render_assistant_bubble(text, true, false),
        ));
        let _ = tx.send(format_frame("status", &status_html("error")));
        state.inflight.remove(&session_id);
        return;
    }

    let Some(raw_final) = final_text else {
        // The loop ended without a terminal event.
        let _ = tx.send(format_frame("status", &status_html("done")));
        state.inflight.remove(&session_id);
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
    if outcome.empty_final_after_tool_calls {
        tracing::warn!(session_id = %session_id, "cockpit.empty_final_after_tool_calls");
    }
    if outcome.anti_mimicry_stall {
        tracing::warn!(session_id = %session_id, "cockpit.anti_mimicry_stall");
    }
    if outcome.knowledge_hallucination {
        tracing::warn!(session_id = %session_id, "cockpit.knowledge_hallucination");
    }

    let asst_id = conv
        .append_assistant_turn(&outcome.text, tool_calls_json(&captured).as_ref())
        .await
        .ok();

    // v1.5 — stage whenever a destructive proposal landed, REGARDLESS of the
    // per-turn allow flag. Gating this on allow_destructive (pre-v1.5) would
    // silently drop the granular path where the turn began authorised and the
    // second destructive re-gated.
    if let (Some(p), Some(mid)) = (&last_proposal, asst_id) {
        if let Err(e) = conv
            .set_pending_destructive(&p.name, &args_map(&p.args), mid)
            .await
        {
            tracing::error!(error = %e, "cockpit.stage_pending_failed");
        }
    }

    // v0.20.1 — opt into pipe-table rendering when a renderer-less knowledge tool
    // fired this turn: `knowledge.*` has no ASCII renderer, so the LLM's prose IS
    // the answer and relayed handbook tables should render as <table>.
    let _ = tx.send(format_frame(
        "message",
        &render_assistant_bubble(&outcome.text, false, knowledge_called(&captured)),
    ));
    let _ = tx.send(format_frame("status", &status_html("done")));
    state.inflight.remove(&session_id);
}

/// The tenant for a freshly-opened conversation — `BSS_TENANT_DEFAULT`, the same
/// source the orchestrator uses.
pub(crate) fn tenant() -> String {
    bss_orchestrator::Settings::from_env().tenant_default
}

/// `set_pending_destructive` stores args as an `IndexMap` so the stored `json`
/// column's text order round-trips (the P5b arg key-order seam). Convert without
/// disturbing insertion order — serde_json has `preserve_order` on (D9).
fn args_map(args: &Value) -> indexmap::IndexMap<String, Value> {
    match args.as_object() {
        Some(m) => m.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
        None => indexmap::IndexMap::new(),
    }
}

fn tool_calls_json(captured: &[Value]) -> Option<Value> {
    if captured.is_empty() {
        None
    } else {
        Some(Value::Array(captured.to_vec()))
    }
}

/// Best-effort customer + subscription snapshot for the focus block. `None` on
/// any read failure — the prompt falls back to a focus-only block.
async fn load_focus_snapshot(state: &AppState, customer_id: &str) -> Option<String> {
    let clients = state.clients.as_ref()?;
    let cust = clients.crm.get_customer(customer_id).await.ok()?;
    let mut snap = serde_json::Map::new();
    snap.insert("customer_id".into(), Value::String(customer_id.to_string()));
    snap.insert(
        "customer_name".into(),
        Value::String(individual_name(&cust).unwrap_or_else(|| customer_id.to_string())),
    );
    snap.insert(
        "customer_status".into(),
        cust.get("status")
            .cloned()
            .unwrap_or(Value::String("?".into())),
    );
    snap.insert(
        "kyc_status".into(),
        cust.get("kycStatus")
            .cloned()
            .unwrap_or(Value::String("?".into())),
    );

    if let Ok(subs) = clients.subscription.list_for_customer(customer_id).await {
        if let Some(primary) = subs.as_array().and_then(|a| a.first()) {
            // Surface the first sub's state + headline balance so the LLM doesn't
            // need a subscription.get round-trip to discover the line is blocked.
            snap.insert(
                "subscription_id".into(),
                primary.get("id").cloned().unwrap_or_default(),
            );
            snap.insert(
                "subscription_state".into(),
                primary
                    .get("state")
                    .cloned()
                    .unwrap_or(Value::String("?".into())),
            );
            snap.insert(
                "msisdn".into(),
                primary.get("msisdn").cloned().unwrap_or_default(),
            );
            snap.insert(
                "offering_id".into(),
                primary.get("offeringId").cloned().unwrap_or_default(),
            );
            if let Some(balances) = primary.get("balances").and_then(Value::as_array) {
                let data_row = balances
                    .iter()
                    .find(|b| {
                        b.get("type").and_then(Value::as_str) == Some("data")
                            || b.get("allowanceType").and_then(Value::as_str) == Some("data")
                    })
                    .or_else(|| balances.first());
                if let Some(d) = data_row {
                    snap.insert(
                        "data_remaining".into(),
                        d.get("remaining")
                            .or_else(|| d.get("used"))
                            .cloned()
                            .unwrap_or_default(),
                    );
                    snap.insert(
                        "data_total".into(),
                        d.get("total")
                            .cloned()
                            .unwrap_or(Value::String(String::new())),
                    );
                }
            }
        }
    }
    // Python: `json.dumps(snapshot, separators=(",", ":"))` — compact.
    serde_json::to_string(&Value::Object(snap)).ok()
}
