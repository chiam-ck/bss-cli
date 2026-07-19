//! Customer chat surface. Port of `bss_self_serve.routes.chat` (v0.12 PR7 +
//! PR13 conversation memory + popup widget).
//!
//! The **only** orchestrator-mediated route in the self-serve portal — every other
//! route writes directly via bss-clients per the v0.10/v0.11 doctrine. The
//! `check-chat-only` doctrine guard asserts `astream_once` appears only here.
//!
//! Routes:
//! * `GET /chat` — the standalone chat page. Renders the running conversation.
//! * `GET /chat/widget` — the same UI as a fixed bottom-right popup partial; the
//!   FAB in `base.html` loads it via `hx-get` so the customer can chat from any
//!   post-login page.
//! * `POST /chat/message` — cap-check, append the user's message, create a turn,
//!   then redirect (full page) or return an HTMX widget refresh that opens the SSE
//!   stream in place.
//! * `POST /chat/reset` — clear the running conversation.
//! * `GET /chat/events/:session_id` — the SSE stream for one in-flight turn.
//!
//! Doctrine: cap-trip → templated response, **no LLM invocation**.
//! `AgentOwnershipViolation` → generic safety reply, no leaked detail.
//! `TurnUsage` → `chat_caps.record_chat_turn` (cost accounting). `FinalMessage`
//! renders in full — the chat surface owns the user-visible reply, so unlike the
//! agent-log widget it does not truncate.

use std::convert::Infallible;
use std::sync::{Arc, LazyLock, Mutex, MutexGuard};

use axum::body::Body;
use axum::extract::{Path, Query, RawForm, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::Extension;
use bss_orchestrator::{astream_once_to, AgentConfig, AgentEvent};
use bss_portal_auth::IdentityView;
use bss_portal_ui::sse::{format_frame, status_html};
use bss_portal_ui::strip_reasoning_leakage;
use bss_portal_ui::{render_assistant_bubble, render_chat_markdown, render_tool_pill};
use fancy_regex::Regex;
use minijinja::context;
use serde::Deserialize;
use serde_json::Value;

use crate::chat_session::{ChatConversation, ChatTurn, Role};
use crate::deps::require_verified_email;
use crate::middleware::PortalSession;
use crate::profile::parse_form;
use crate::routes::render;
use crate::templating::request_ctx;
use crate::AppState;

const OWNERSHIP_VIOLATION_REPLY: &str =
    "Sorry — I couldn't complete that. Please try again, or contact \
     support if the issue persists.";

const GENERIC_ERROR_REPLY: &str = "Sorry — something went wrong. Please try again.";

/// v0.13.1 — anti-hallucination. Detect FIRST-PERSON ACTIVE escalation claims
/// ("I've escalated this", "I'm raising a case") and verify `case.open_for_me`
/// actually fired this turn. Past-tense / third-person language ("your case has
/// been raised", "case ID is CASE-123") is the LLM legitimately recapping a prior
/// escalation; we don't want to false-positive on those when the customer asks
/// "what's my case ID" later.
static RE_ESCALATION_CLAIM: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(
        r"(?i)\bI(?:'ve| have| am| will|'ll|'m)?\s+(?:escalat\w*|(?:raised?|opened?|filed?|raising|opening|filing)\s+(?:a|the|your)?\s*case)\b",
    )
    .expect("escalation-claim regex is a compile-time constant")
});

const ESCALATION_HALLUCINATION_FALLBACK: &str =
    "I can't take this further on my own — please email support directly \
     at {email} so a human agent can look into it. Sorry for the extra \
     step.";

/// True if the assistant's reply claims to have escalated.
pub fn claims_escalation(text: &str) -> bool {
    RE_ESCALATION_CLAIM.is_match(text).unwrap_or(false)
}

// ── Helpers ──────────────────────────────────────────────────────────

fn is_htmx(headers: &HeaderMap) -> bool {
    headers
        .get("hx-request")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("true"))
        .unwrap_or(false)
}

/// Stable per-identity key for chat caps + the conversation store.
///
/// Linked customers cap on the `customer_id` (the v0.12 contract). Verified-but-
/// unlinked identities cap on the identity id with an `anon-` prefix so they never
/// collide with a `CUST-*` id and the audit row's customer_id reflects the
/// (anonymous) reality. Pre-signup catalog enquiries get their own budget; signing
/// up after that doesn't merge histories — clean break, by design.
pub fn cap_key(identity: &IdentityView) -> String {
    match identity.customer_id.as_deref().filter(|c| !c.is_empty()) {
        Some(cid) => cid.to_string(),
        None => format!("anon-{}", identity.id),
    }
}

/// `auth_context.actor` binding. Linked customers get their `CUST-*` so the
/// `.mine` wrappers resolve. Anonymous (pre-signup) identities get `None` — the
/// wrappers refuse cleanly via `_NoActorBound`, catalog-public reads still work,
/// and the system prompt steers the LLM accordingly.
pub fn chat_actor(identity: &IdentityView) -> Option<String> {
    identity.customer_id.clone().filter(|c| !c.is_empty())
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Customer + primary subscription context for the system prompt.
struct CustomerContext {
    customer: Value,
    primary_sub: Option<Value>,
    email: String,
    name: String,
    plan_id: String,
    is_linked: bool,
}

impl CustomerContext {
    fn browsing() -> Self {
        Self {
            customer: Value::Object(serde_json::Map::new()),
            primary_sub: None,
            email: String::new(),
            name: String::new(),
            plan_id: "(loading)".to_string(),
            is_linked: false,
        }
    }
}

/// Read customer + primary subscription for the system prompt. Best-effort:
/// failures fall through to `(loading)` placeholders rather than blocking the turn.
/// For unlinked (pre-signup) identities no BSS reads happen and the prompt renders
/// in browse-only mode.
async fn load_customer_context(state: &AppState, customer_id: Option<&str>) -> CustomerContext {
    let Some(customer_id) = customer_id else {
        return CustomerContext::browsing();
    };
    let Some(clients) = &state.clients else {
        return CustomerContext::browsing();
    };

    let mut ctx = CustomerContext {
        is_linked: true,
        ..CustomerContext::browsing()
    };

    match clients.crm.get_customer(customer_id).await {
        Ok(customer) => {
            ctx.email = first_str(&customer, &["email", "primaryEmail"]).unwrap_or_default();
            ctx.name =
                first_str(&customer, &["name", "givenName"]).unwrap_or_else(|| "there".to_string());
            ctx.customer = customer;
        }
        Err(e) => {
            tracing::warn!(
                customer_id = %customer_id,
                error = %e,
                "chat.prompt_context_load_failed"
            );
            return ctx;
        }
    }

    match clients.subscription.list_for_customer(customer_id).await {
        Ok(subs) => {
            let subs = subs.as_array().cloned().unwrap_or_default();
            let primary = subs
                .iter()
                .find(|s| {
                    matches!(
                        s.get("state").and_then(Value::as_str),
                        Some("active") | Some("blocked")
                    )
                })
                .or_else(|| subs.first())
                .cloned();
            if let Some(sub) = &primary {
                if let Some(p) = sub.get("offeringId").and_then(Value::as_str) {
                    ctx.plan_id = p.to_string();
                }
            }
            ctx.primary_sub = primary;
        }
        Err(e) => {
            tracing::warn!(
                customer_id = %customer_id,
                error = %e,
                "chat.prompt_context_load_failed"
            );
        }
    }
    ctx
}

/// First non-empty string among `keys`. Mirrors Python's `a or b or ""` chain.
fn first_str(v: &Value, keys: &[&str]) -> Option<String> {
    keys.iter()
        .filter_map(|k| v.get(*k).and_then(Value::as_str))
        .find(|s| !s.is_empty())
        .map(str::to_string)
}

/// One rendered message for the widget template.
#[derive(serde::Serialize)]
struct MessageView {
    role: &'static str,
    body: String,
    /// Assistant bodies only — pre-rendered through the chat-markdown converter so
    /// a page reload looks identical to a live-streamed bubble. The user-side body
    /// is left raw and the template escapes it.
    body_html: Option<String>,
}

fn message_views(conv: Option<&Arc<Mutex<ChatConversation>>>) -> Vec<MessageView> {
    let Some(conv) = conv else {
        return Vec::new();
    };
    lock(conv)
        .messages
        .iter()
        .map(|m| MessageView {
            role: m.role.as_str(),
            body: m.body.clone(),
            body_html: match m.role {
                Role::Assistant => Some(render_chat_markdown(&m.body, false)),
                Role::User => None,
            },
        })
        .collect()
}

/// Validate `?session=<sid>` against the turn store. Returns the id when valid AND
/// owned by the actor, otherwise `None` so the template skips the SSE host.
/// Cross-customer (or cross-anonymous) impersonation via a crafted URL is blocked
/// here; the SSE handler enforces too.
fn resolve_session(state: &AppState, session: Option<&str>, cap_key: &str) -> Option<String> {
    let session = session.filter(|s| !s.is_empty())?;
    let turn = state.chat_turns.get(session)?;
    if lock(&turn).customer_id != cap_key {
        return None;
    }
    Some(session.to_string())
}

/// The template inputs shared by the standalone page and the popup widget. Port of
/// Python's `_render_widget_context` argument set.
#[derive(Default)]
struct ChatView<'a> {
    cap_key: &'a str,
    session_id: Option<String>,
    cap_tripped: Option<&'a str>,
    retry_at: Option<&'a str>,
}

/// Render the widget partial or the standalone page — shared context for both.
fn render_chat(
    state: &AppState,
    template: &str,
    portal: &PortalSession,
    view: ChatView<'_>,
) -> Response {
    let conv = state.chat_conversations.get(view.cap_key);
    let messages = message_views(conv.as_ref());
    render(
        state,
        template,
        context! {
            customer_id => view.cap_key,
            session_id => view.session_id,
            has_history => !messages.is_empty(),
            messages => messages,
            cap_tripped => view.cap_tripped,
            retry_at => view.retry_at,
            request => request_ctx("/chat", portal.identity_email()),
        },
    )
}

#[derive(Deserialize, Default)]
pub struct ChatQuery {
    session: Option<String>,
    cap_tripped: Option<String>,
    retry_at: Option<String>,
}

// ── Routes ───────────────────────────────────────────────────────────

/// `GET /chat` — the standalone chat page. Available to any verified-email
/// identity; linked-customer status determines which tools the LLM can reach (the
/// `.mine` wrappers refuse without a customer).
pub async fn chat_page(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<ChatQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/chat") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let key = cap_key(&identity);
    let session_id = resolve_session(&state, q.session.as_deref(), &key);
    render_chat(
        &state,
        "chat_page.html",
        &portal,
        ChatView {
            cap_key: &key,
            session_id,
            cap_tripped: q.cap_tripped.as_deref(),
            retry_at: q.retry_at.as_deref(),
        },
    )
}

/// `GET /chat/widget` — the popup widget partial, loaded by the FAB's `hx-get`
/// into `#chat-widget-host` on every page with a verified-email session. Linked or
/// anonymous — both can browse the catalog via chat; `.mine` writes are gated by
/// the wrapper's actor check.
pub async fn chat_widget(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<ChatQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/chat") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let key = cap_key(&identity);
    let session_id = resolve_session(&state, q.session.as_deref(), &key);
    render_chat(
        &state,
        "chat_widget.html",
        &portal,
        ChatView {
            cap_key: &key,
            session_id,
            cap_tripped: q.cap_tripped.as_deref(),
            retry_at: q.retry_at.as_deref(),
        },
    )
}

/// Render the widget directly (the HTMX branch of the POST routes — Python calls
/// `chat_widget(...)` inline).
fn widget_response(state: &AppState, portal: &PortalSession, view: ChatView<'_>) -> Response {
    render_chat(state, "chat_widget.html", portal, view)
}

/// `POST /chat/message` — append the user's message to the conversation and either
/// redirect (full page) or return a widget refresh (HTMX) so the SSE stream picks
/// up the new turn.
pub async fn chat_message(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let identity = match require_verified_email(&portal, "/chat") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let key = cap_key(&identity);
    let form = parse_form(&body);
    let message = form
        .iter()
        .find(|(k, _)| k == "message")
        .map(|(_, v)| v.trim().to_string())
        .unwrap_or_default();

    if message.is_empty() {
        return if is_htmx(&headers) {
            widget_response(
                &state,
                &portal,
                ChatView {
                    cap_key: &key,
                    ..Default::default()
                },
            )
        } else {
            Redirect::to("/chat").into_response()
        };
    }

    // Doctrine: cap-trip → templated response, no LLM invocation.
    let cap = state.chat_caps.check_caps(&key, bss_clock::now()).await;
    if !cap.allowed {
        let reason = cap.reason.unwrap_or_else(|| "cap_check_failed".to_string());
        let retry_at = cap.retry_at.map(|t| t.to_rfc3339());
        tracing::info!(cap_key = %key, reason = %reason, "chat.cap_tripped");
        return if is_htmx(&headers) {
            widget_response(
                &state,
                &portal,
                ChatView {
                    cap_key: &key,
                    session_id: None,
                    cap_tripped: Some(&reason),
                    retry_at: retry_at.as_deref(),
                },
            )
        } else {
            let mut qs = format!("cap_tripped={}", urlencode(&reason));
            if let Some(r) = &retry_at {
                qs.push_str(&format!("&retry_at={}", urlencode(r)));
            }
            Redirect::to(&format!("/chat?{qs}")).into_response()
        };
    }

    let conv = state.chat_conversations.get_or_create(&key);
    lock(&conv).append(Role::User, &message);

    let turn = state.chat_turns.create(&key, &message);
    let session_id = lock(&turn).session_id.clone();

    if is_htmx(&headers) {
        widget_response(
            &state,
            &portal,
            ChatView {
                cap_key: &key,
                session_id: Some(session_id),
                ..Default::default()
            },
        )
    } else {
        Redirect::to(&format!("/chat?session={session_id}")).into_response()
    }
}

/// `POST /chat/reset` — clear the running conversation. The next message starts a
/// fresh history — no prior context.
pub async fn chat_reset(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
) -> Response {
    let identity = match require_verified_email(&portal, "/chat") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let key = cap_key(&identity);
    state.chat_conversations.reset(&key);
    if is_htmx(&headers) {
        widget_response(
            &state,
            &portal,
            ChatView {
                cap_key: &key,
                ..Default::default()
            },
        )
    } else {
        Redirect::to("/chat").into_response()
    }
}

/// `GET /chat/events/:session_id` — the SSE stream for one in-flight chat turn.
///
/// Loads prior turns of the running conversation so the system prompt carries
/// context — the LLM answers the next user message in continuity. On FinalMessage
/// the assistant's reply lands in the conversation so subsequent turns see it.
pub async fn chat_events(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(session_id): Path<String>,
) -> Response {
    let identity = match require_verified_email(&portal, "/chat") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let key = cap_key(&identity);
    let actor = chat_actor(&identity);

    let Some(turn) = state.chat_turns.get(&session_id) else {
        return (StatusCode::NOT_FOUND, "chat turn not found").into_response();
    };
    if lock(&turn).customer_id != key {
        tracing::warn!(
            actor = %key,
            session_id = %session_id,
            owner = %lock(&turn).customer_id,
            "chat.cross_customer_session_attempt"
        );
        return (StatusCode::FORBIDDEN, "not your chat session").into_response();
    }

    let conv = state.chat_conversations.get_or_create(&key);
    let ctx = load_customer_context(&state, actor.as_deref()).await;

    let question = lock(&turn).question.clone();

    // Prior messages = everything in the conversation EXCEPT the latest user
    // message (the one this turn is answering — the LLM sees it as `prompt`, not
    // as prior context).
    let prior_messages: Vec<(String, String)> = lock(&conv)
        .messages
        .iter()
        .filter(|m| !(m.role == Role::User && m.body == question))
        .map(|m| (m.role.as_str().to_string(), m.body.clone()))
        .collect();

    let account_state = if ctx.is_linked {
        ctx.customer
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("active")
            .to_string()
    } else {
        "browsing".to_string()
    };

    let operator_name = state
        .settings
        .operator_name
        .clone()
        .unwrap_or_else(|| bss_branding::current(None).brand_name);

    let system_prompt = bss_orchestrator::prompts::build_customer_chat_prompt(
        if ctx.name.is_empty() {
            "there"
        } else {
            &ctx.name
        },
        if ctx.email.is_empty() {
            &identity.email
        } else {
            &ctx.email
        },
        &account_state,
        &ctx.plan_id,
        &bss_orchestrator::prompts::build_balance_summary(ctx.primary_sub.as_ref()),
        &operator_name,
        &state.settings.operator_support_email,
        &prior_messages,
        ctx.is_linked,
    );

    // Transcript for case.open_for_me — full running text, including the latest
    // user message.
    let transcript = lock(&conv).transcript_text();

    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<Result<Vec<u8>, Infallible>>();

    // Already finished (e.g. a reconnect after the turn completed) → one frame.
    if lock(&turn).done {
        let _ = tx.send(Ok(format_frame("status", &status_html("done"))));
        return sse_response(rx);
    }

    let support_email = state.settings.operator_support_email.clone();
    tokio::spawn(run_turn(
        state.clone(),
        tx,
        turn,
        conv,
        key,
        actor,
        question,
        system_prompt,
        transcript,
        support_email,
    ));

    sse_response(rx)
}

/// Drive one agent turn, translating [`AgentEvent`]s into SSE frames.
#[allow(clippy::too_many_arguments)]
async fn run_turn(
    state: AppState,
    tx: tokio::sync::mpsc::UnboundedSender<Result<Vec<u8>, Infallible>>,
    turn: Arc<Mutex<ChatTurn>>,
    conv: Arc<Mutex<ChatConversation>>,
    cap_key: String,
    actor: Option<String>,
    question: String,
    system_prompt: String,
    transcript: String,
    support_email: String,
) {
    if tx
        .send(Ok(format_frame("status", &status_html("live"))))
        .is_err()
    {
        return;
    }

    let (Some(registry), Some(clients)) = (&state.chat_registry, &state.clients) else {
        tracing::error!("chat.registry_unavailable");
        let _ = tx.send(Ok(format_frame(
            "message",
            &render_assistant_bubble(GENERIC_ERROR_REPLY, true, false),
        )));
        let _ = tx.send(Ok(format_frame("status", &status_html("error"))));
        return;
    };

    let mut model = match bss_orchestrator::OpenRouterChatModel::from_env() {
        Ok(m) => m,
        Err(e) => {
            tracing::error!(error = %e, "chat.model_unavailable");
            let _ = tx.send(Ok(format_frame(
                "message",
                &render_assistant_bubble(GENERIC_ERROR_REPLY, true, false),
            )));
            let _ = tx.send(Ok(format_frame("status", &status_html("error"))));
            return;
        }
    };

    let config = AgentConfig {
        allow_destructive: true,
        tool_filter: Some("customer_self_serve".to_string()),
        system_prompt,
        transcript,
        ctx: bss_orchestrator::ToolCtx {
            // auth_context.actor = customer_id when linked, "" for anonymous. The
            // .mine wrappers refuse cleanly when empty; catalog reads still work.
            actor: actor.clone().unwrap_or_default(),
            channel: "portal-chat".to_string(),
            tenant: state.settings.env.clone(),
            transcript: String::new(),
        },
        model_name: String::new(),
        crm_audit: Some(clients.crm.clone()),
        ..Default::default()
    };

    // v0.13.1 — track tool calls this turn so we can detect escalation
    // hallucinations. If the assistant claims to have escalated but never called
    // case.open_for_me, replace the text with a safe fallback.
    let mut called_tools: Vec<String> = Vec::new();
    // Usage events are recorded after the loop — record_chat_turn is async and the
    // sink is sync. Python awaits it inline; the ordering that matters (usage
    // before the browser disconnects on `done`) is preserved because we record
    // before returning either way.
    let mut usage: Option<(i64, i64, String)> = None;
    let mut outcome: Option<TurnOutcome> = None;

    {
        let tx = &tx;
        let turn = &turn;
        let conv = &conv;
        let called_tools = &mut called_tools;
        let usage = &mut usage;
        let outcome = &mut outcome;
        let support_email = &support_email;

        let mut sink = |event: AgentEvent| -> bool {
            match event {
                // Token usage: record cost; don't render.
                AgentEvent::TurnUsage {
                    prompt_tok,
                    completion_tok,
                    model,
                } => {
                    *usage = Some((prompt_tok, completion_tok, model));
                    true
                }

                // Trip-wire — generic safety reply, never leaked detail.
                AgentEvent::Error { message } if message.contains("AgentOwnershipViolation") => {
                    {
                        let mut t = lock(turn);
                        t.ownership_violation = true;
                        t.error = Some("ownership_violation".to_string());
                        t.final_text = OWNERSHIP_VIOLATION_REPLY.to_string();
                    }
                    lock(conv).append(Role::Assistant, OWNERSHIP_VIOLATION_REPLY);
                    let _ = tx.send(Ok(format_frame(
                        "message",
                        &render_assistant_bubble(OWNERSHIP_VIOLATION_REPLY, true, false),
                    )));
                    let _ = tx.send(Ok(format_frame("status", &status_html("error"))));
                    *outcome = Some(TurnOutcome::Stopped);
                    false
                }

                AgentEvent::Error { message } => {
                    lock(turn).error = Some(message);
                    lock(conv).append(Role::Assistant, GENERIC_ERROR_REPLY);
                    let _ = tx.send(Ok(format_frame(
                        "message",
                        &render_assistant_bubble(GENERIC_ERROR_REPLY, true, false),
                    )));
                    let _ = tx.send(Ok(format_frame("status", &status_html("error"))));
                    *outcome = Some(TurnOutcome::Stopped);
                    false
                }

                // Tool calls render as small inline pills so the customer can see
                // what the agent is doing.
                AgentEvent::ToolCallStarted { name, .. } => {
                    called_tools.push(name.clone());
                    tx.send(Ok(format_frame("message", &render_tool_pill(&name))))
                        .is_ok()
                }

                AgentEvent::FinalMessage { text } => {
                    let mut text = strip_reasoning_leakage(&text);
                    let mut is_error = false;
                    // Anti-hallucination: if the reply claims to have escalated but
                    // case.open_for_me wasn't actually called this turn, replace it
                    // with a safe fallback. Doctrine-coupled: the v0.12 escalation
                    // contract is "the case row IS the escalation"; a model
                    // hallucinating the sentence without the side effect is a
                    // doctrine violation we fix at the edge, not by retrying.
                    if claims_escalation(&text)
                        && !called_tools.iter().any(|t| t == "case.open_for_me")
                    {
                        tracing::warn!(
                            cap_key = %lock(turn).customer_id,
                            called_tools = ?called_tools,
                            "chat.escalation_hallucination"
                        );
                        text = ESCALATION_HALLUCINATION_FALLBACK.replace("{email}", support_email);
                        is_error = true;
                    }
                    {
                        let mut t = lock(turn);
                        t.done = true;
                        t.final_text = text.clone();
                    }
                    lock(conv).append(Role::Assistant, &text);
                    let _ = tx.send(Ok(format_frame(
                        "message",
                        &render_assistant_bubble(&text, is_error, false),
                    )));
                    let _ = tx.send(Ok(format_frame("status", &status_html("done"))));
                    *outcome = Some(TurnOutcome::Stopped);
                    false
                }

                // Everything else (PromptReceived, ToolCallCompleted) is hidden from
                // the chat log — the audit row + `bss trace` carry the forensic
                // record.
                _ => true,
            }
        };

        astream_once_to(&mut model, registry, &question, &config, &mut sink).await;
    }

    // Cost accounting — Python records this inline on the TurnUsage event; the
    // sink is sync, so it lands here. Either way it runs before the response ends.
    if let Some((prompt_tok, completion_tok, model)) = usage {
        state
            .chat_caps
            .record_chat_turn(
                &cap_key,
                prompt_tok,
                completion_tok,
                Some(&model),
                bss_clock::now(),
            )
            .await;
    }

    // The loop ended without a terminal event (model returned nothing renderable).
    if outcome.is_none() {
        let _ = tx.send(Ok(format_frame("status", &status_html("done"))));
    }
}

enum TurnOutcome {
    Stopped,
}

/// Wrap the frame receiver in a `text/event-stream` response. The frames are
/// pre-encoded by `bss_portal_ui::sse::format_frame`, so this streams raw bytes
/// rather than re-encoding through axum's `Sse` type (which would double-encode).
fn sse_response(rx: tokio::sync::mpsc::UnboundedReceiver<Result<Vec<u8>, Infallible>>) -> Response {
    let stream = futures_util::stream::unfold(rx, |mut rx| async move {
        rx.recv().await.map(|item| (item, rx))
    });
    let mut resp = Response::new(Body::from_stream(stream));
    let h = resp.headers_mut();
    #[allow(clippy::expect_used)]
    {
        h.insert(
            axum::http::header::CONTENT_TYPE,
            "text/event-stream".parse().expect("static header value"),
        );
        h.insert(
            axum::http::header::CACHE_CONTROL,
            "no-cache".parse().expect("static header value"),
        );
        h.insert(
            "X-Accel-Buffering",
            "no".parse().expect("static header value"),
        );
    }
    resp
}

/// Minimal query-component percent-encoding.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn identity(customer_id: Option<&str>) -> IdentityView {
        IdentityView {
            id: "abc123".to_string(),
            email: "a@b.c".to_string(),
            customer_id: customer_id.map(str::to_string),
            email_verified_at: Some(bss_clock::now()),
            status: "active".to_string(),
        }
    }

    #[test]
    fn cap_key_uses_customer_id_when_linked() {
        assert_eq!(cap_key(&identity(Some("CUST-001"))), "CUST-001");
    }

    #[test]
    fn cap_key_prefixes_anonymous_identities() {
        // Never collides with a CUST-* id, so an anonymous budget is its own.
        assert_eq!(cap_key(&identity(None)), "anon-abc123");
        assert_eq!(cap_key(&identity(Some(""))), "anon-abc123");
    }

    #[test]
    fn chat_actor_is_none_when_unlinked() {
        assert_eq!(
            chat_actor(&identity(Some("CUST-001"))).as_deref(),
            Some("CUST-001")
        );
        assert_eq!(chat_actor(&identity(None)), None);
        assert_eq!(chat_actor(&identity(Some(""))), None);
    }

    /// Golden — the v0.13.1 anti-hallucination detector. FIRST-PERSON ACTIVE claims
    /// trip; past-tense / third-person recaps must NOT (the customer asking "what's
    /// my case ID" gets a legitimate recap).
    #[test]
    fn escalation_claim_detection_matches_oracle() {
        // Trips.
        for s in [
            "I've escalated this to a human agent.",
            "I have escalated this",
            "I am escalating this now",
            "I'll escalate this for you",
            "I'm raising a case for you",
            "I have opened a case",
            "I will file a case",
            "I escalated it",
            "I am opening your case",
        ] {
            assert!(claims_escalation(s), "should trip: {s}");
        }
        // Does not trip.
        for s in [
            "Your case has been raised.",
            "The case ID is CASE-123.",
            "A case was opened yesterday.",
            "Your balance is 2GB.",
            "",
        ] {
            assert!(!claims_escalation(s), "should NOT trip: {s}");
        }
    }

    #[test]
    fn first_str_picks_first_non_empty() {
        let v = serde_json::json!({"email": "", "primaryEmail": "x@y.z"});
        assert_eq!(
            first_str(&v, &["email", "primaryEmail"]).as_deref(),
            Some("x@y.z")
        );
        assert_eq!(first_str(&serde_json::json!({}), &["email"]), None);
    }
}
