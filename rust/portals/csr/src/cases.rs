//! Case queue + thread + workbench actions (v1.6 cockpit CRM). Port of
//! `bss_csr.routes.cases` and `bss_csr.routes.case`.
//!
//! The queue is a read view over `crm.list_cases`. The thread (`/case/{id}`) is
//! the one v0.5 surface kept into v0.13 — useful for copy-paste case-id deep links
//! from a chat session, Slack, or a runbook. It grew the CRM workbench in v1.6:
//! notes, transitions, priority, ticket lifecycle, each a single policy-gated
//! `bss-clients` call.
//!
//! v1.6.1 (operator directive) — the destructive verbs (`case.close`,
//! `ticket.cancel`) are direct CRUD behind the **two-step UI confirm**: the form
//! must carry `confirm=yes` or the route refuses. The policy layer stays the
//! server-side gate; only `cockpit.rs` talks to the orchestrator.
//!
//! **Doctrine (CLAUDE.md):** the Case API speaks the internal snake_case DTO
//! (`customer_id`/`opened_at`), not TMF camelCase — every read goes through
//! `views::field`, which reads both (the v0.13 page silently blanked those fields).

use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use bss_clients::{ticket_trigger_for_state, ClientError};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{back_to, render, write_result, CONFIRM_REQUIRED};
use crate::views::{field, field_str, flatten_case, flatten_ticket, fmt_dt};
use crate::AppState;

const PAGE_SIZE: i64 = 25;

const CASE_STATES: [&str; 5] = [
    "open",
    "in_progress",
    "pending_customer",
    "resolved",
    "closed",
];

/// Case-state → workbench transition buttons `(label, trigger)`. Mirrors
/// `services/crm/app/domain/case_state.py`; an invalid trigger degrades to a
/// PolicyViolation flash, never a 500.
const CASE_ACTIONS: &[(&str, &[(&str, &str)])] = &[
    ("open", &[("Take", "take"), ("Resolve", "resolve")]),
    (
        "in_progress",
        &[("Await customer", "await_customer"), ("Resolve", "resolve")],
    ),
    ("pending_customer", &[("Resume", "resume")]),
    ("resolved", &[]),
    ("closed", &[]),
];

/// Ticket-state → `(label, target state)` for `transition_ticket`. `resolve` has
/// its own form (resolution notes required), so it isn't a plain transition here.
const TICKET_ACTIONS: &[(&str, &[(&str, &str)])] = &[
    ("open", &[("Acknowledge", "acknowledged")]),
    ("acknowledged", &[("Start", "in_progress")]),
    ("in_progress", &[]),
    ("pending", &[("Resume", "in_progress")]),
    (
        "resolved",
        &[("Close", "closed"), ("Reopen", "in_progress")],
    ),
    ("closed", &[]),
    ("cancelled", &[]),
];

const TICKET_TYPES: [&str; 4] = [
    "information_request",
    "technical",
    "subscription",
    "billing",
];
const PRIORITIES: [&str; 5] = ["low", "normal", "medium", "high", "critical"];

fn actions_for(table: &[(&str, &[(&str, &str)])], state: &str) -> Vec<Value> {
    table
        .iter()
        .find(|(s, _)| *s == state)
        .map(|(_, acts)| acts.iter().map(|(l, t)| json!([l, t])).collect())
        .unwrap_or_default()
}

// ── GET /cases ───────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct ListQuery {
    #[serde(default)]
    state: String,
    #[serde(default)]
    customer: String,
    #[serde(default)]
    page: i64,
}

pub async fn cases_list(State(state): State<AppState>, Query(q): Query<ListQuery>) -> Response {
    if !(0..=10_000).contains(&q.page) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "page out of range").into_response();
    }
    let state_clean = q.state.trim().to_string();
    let customer_clean = q.customer.trim().to_string();

    let mut rows: Vec<Value> = Vec::new();
    let mut has_next = false;
    if let Some(clients) = &state.clients {
        let raw = match clients
            .crm
            .list_cases_paged(
                opt(&customer_clean),
                opt(&state_clean),
                None,
                Some(PAGE_SIZE + 1),
                Some(q.page * PAGE_SIZE),
            )
            .await
        {
            Ok(v) => v.as_array().cloned().unwrap_or_default(),
            Err(e) => {
                tracing::warn!(status = e.status_code(), "csr.cases.list_failed");
                Vec::new()
            }
        };
        has_next = raw.len() as i64 > PAGE_SIZE;
        rows = raw
            .iter()
            .take(PAGE_SIZE as usize)
            .map(flatten_case)
            .collect();
    }

    render(
        &state,
        "cases_list.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "cases",
            "model": "(env default)",
            "state": state_clean,
            "customer": customer_clean,
            "states": CASE_STATES,
            "rows": rows,
            "page": q.page,
            "has_prev": q.page > 0,
            "has_next": has_next,
        })),
    )
}

fn opt(s: &str) -> Option<&str> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

// ── GET /case/{id} ───────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
    #[serde(default)]
    err: String,
}

pub async fn case_thread(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let case_raw = match clients.crm.get_case(&case_id).await {
        Ok(c) => c,
        Err(ClientError::NotFound(_)) => {
            return (StatusCode::NOT_FOUND, format!("Case {case_id} not found")).into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "csr.case.get_failed");
            return (StatusCode::BAD_GATEWAY, "CRM error").into_response();
        }
    };

    // Embedded tickets win; fall back to a list call only when absent.
    let mut tickets = case_raw
        .get("tickets")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if tickets.is_empty() {
        if let Ok(v) = clients
            .crm
            .list_tickets(None, Some(&case_id), None, None)
            .await
        {
            tickets = v.as_array().cloned().unwrap_or_default();
        }
    }
    let ticket_views: Vec<Value> = tickets.iter().map(flatten_ticket).collect();

    // Agents for the assign dropdown — best-effort; an empty list hides the control.
    let agents: Vec<Value> = match clients.crm.list_agents(Some("active")).await {
        Ok(v) => v
            .as_array()
            .map(|arr| {
                arr.iter()
                    .map(|a| {
                        let id = a.get("id").and_then(Value::as_str).unwrap_or("");
                        json!({
                            "id": id,
                            "name": a.get("name").and_then(Value::as_str).unwrap_or(id),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default(),
        Err(_) => Vec::new(),
    };

    // Notes sorted by created_at (string compare, matching Python's `str(...)` key).
    let mut notes = case_raw
        .get("notes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    notes.sort_by(|a, b| {
        field_str(Some(a), &["created_at"], "").cmp(&field_str(Some(b), &["created_at"], ""))
    });

    let transcript_hash = field(Some(&case_raw), &["chat_transcript_hash"])
        .and_then(Value::as_str)
        .map(str::to_string);
    let transcript_view = match &transcript_hash {
        Some(hash) => match clients.crm.get_chat_transcript(hash).await {
            Ok(row) => Some(json!({
                "hash": row.get("hash").and_then(Value::as_str).unwrap_or(hash),
                "body": row.get("body").and_then(Value::as_str).unwrap_or(""),
                "recorded_at": row.get("recorded_at").and_then(Value::as_str).unwrap_or(""),
            })),
            Err(e) => {
                tracing::warn!(case_id = %case_id, status = e.status_code(),
                    "csr.case.chat_transcript_fetch_failed");
                Some(json!({
                    "hash": hash,
                    "body": Value::Null,
                    "recorded_at": "",
                    "error": "Transcript is no longer retrievable. It may have been archived.",
                }))
            }
        },
        None => None,
    };

    let case_state = field_str(Some(&case_raw), &["state"], "unknown");
    let customer_id = field_str(Some(&case_raw), &["customer_id"], "");

    let note_views: Vec<Value> = notes
        .iter()
        .map(|n| {
            json!({
                "id": n.get("id").and_then(Value::as_str).unwrap_or(""),
                "body": n.get("body").and_then(Value::as_str).unwrap_or(""),
                "author": field_str(Some(n), &["author_agent_id", "author", "created_by"], "system"),
                "at": fmt_dt(&field_str(Some(n), &["created_at"], "")),
            })
        })
        .collect();

    // workbench_context — action availability for the template.
    let ticket_actions_by_id: serde_json::Map<String, Value> = ticket_views
        .iter()
        .map(|t| {
            let id = t
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let tstate = t.get("state").and_then(Value::as_str).unwrap_or("");
            (id, Value::Array(actions_for(TICKET_ACTIONS, tstate)))
        })
        .collect();

    render(
        &state,
        "case_thread.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "cases",
            "actor": bss_cockpit::OPERATOR_ACTOR,
            "model": crate::cockpit::model_label(&state),
            "case": {
                "id": case_raw.get("id").and_then(Value::as_str).unwrap_or(&case_id),
                "subject": case_raw.get("subject").and_then(Value::as_str).unwrap_or(""),
                "description": case_raw.get("description").and_then(Value::as_str).unwrap_or(""),
                "state": case_state,
                "priority": field_str(Some(&case_raw), &["priority"], ""),
                "category": field_str(Some(&case_raw), &["category"], ""),
                "resolution_code": field_str(Some(&case_raw), &["resolution_code"], ""),
                "agent_id": field_str(
                    Some(&case_raw),
                    &["opened_by_agent_id", "agent_id", "assigned_agent_id"],
                    "",
                ),
                "customer_id": customer_id,
                "created_at": fmt_dt(&field_str(Some(&case_raw), &["opened_at", "created_at"], "")),
                "closed_at": fmt_dt(&field_str(Some(&case_raw), &["closed_at"], "")),
                "chat_transcript_hash": transcript_hash,
            },
            "tickets": ticket_views,
            "agents": agents,
            "notes": note_views,
            "transcript": transcript_view,
            "flash": q.flash,
            "err": q.err.chars().take(300).collect::<String>(),
            // workbench_context(case_state, ticket_views)
            "case_actions": actions_for(CASE_ACTIONS, &case_state),
            "ticket_actions_by_id": ticket_actions_by_id,
            "ticket_types": TICKET_TYPES,
            "priorities": PRIORITIES,
            "case_is_open": case_state != "closed",
        })),
    )
}

// ── writes ───────────────────────────────────────────────────────────

fn back_to_case(case_id: &str, flash: &str, err: &str) -> Response {
    back_to(&format!("/case/{case_id}"), flash, err)
}

/// `_run` — run one write; flash the outcome back onto the case page. The base's
/// `write_result` already maps `Policy`→message and other errors→status code.
fn run(case_id: &str, action: &str, r: Result<Value, ClientError>) -> Response {
    write_result(&format!("/case/{case_id}"), action, r)
}

#[derive(Deserialize)]
pub struct CloseForm {
    resolution_code: String,
    #[serde(default)]
    confirm: String,
}

/// `POST /case/{id}/close` — **confirm-gated**.
pub async fn case_close(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Form(form): Form<CloseForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_case(&case_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .close_case(&case_id, form.resolution_code.trim())
        .await;
    run(&case_id, "case_closed", r)
}

#[derive(Deserialize, Default)]
pub struct ConfirmForm {
    #[serde(default)]
    confirm: String,
}

/// `POST /case/{id}/ticket/{ticket_id}/cancel` — **confirm-gated**.
pub async fn ticket_cancel(
    State(state): State<AppState>,
    Path((case_id, ticket_id)): Path<(String, String)>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_case(&case_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.crm.cancel_ticket(&ticket_id).await;
    run(&case_id, "ticket_cancelled", r)
}

#[derive(Deserialize)]
pub struct NoteForm {
    body: String,
}

/// `POST /case/{id}/note`.
pub async fn case_add_note(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Form(form): Form<NoteForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.crm.add_case_note(&case_id, form.body.trim()).await;
    run(&case_id, "note_added", r)
}

#[derive(Deserialize)]
pub struct TransitionForm {
    trigger: String,
}

/// `POST /case/{id}/transition`. An unknown trigger is rejected with the oracle's
/// `{trigger!r}` message before any client call.
pub async fn case_transition(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Form(form): Form<TransitionForm>,
) -> Response {
    let valid = CASE_ACTIONS
        .iter()
        .flat_map(|(_, acts)| acts.iter().map(|(_, t)| *t))
        .any(|t| t == form.trigger);
    if !valid {
        return back_to_case(
            &case_id,
            "",
            &format!("Unknown transition {}", py_repr(&form.trigger)),
        );
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.crm.transition_case(&case_id, &form.trigger).await;
    run(&case_id, "transitioned", r)
}

#[derive(Deserialize)]
pub struct PriorityForm {
    priority: String,
}

/// `POST /case/{id}/priority`.
pub async fn case_priority(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Form(form): Form<PriorityForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .update_case_priority(&case_id, &form.priority)
        .await;
    run(&case_id, "priority_updated", r)
}

#[derive(Deserialize)]
pub struct OpenTicketForm {
    customer_id: String,
    #[serde(default = "information_request")]
    ticket_type: String,
    subject: String,
}

fn information_request() -> String {
    "information_request".to_string()
}

/// `POST /case/{id}/ticket`.
pub async fn case_open_ticket(
    State(state): State<AppState>,
    Path(case_id): Path<String>,
    Form(form): Form<OpenTicketForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let ttype = if TICKET_TYPES.contains(&form.ticket_type.as_str()) {
        form.ticket_type.as_str()
    } else {
        "information_request"
    };
    let r = clients
        .crm
        .open_ticket(
            ttype,
            form.subject.trim(),
            Some(&case_id),
            Some(&form.customer_id),
            None,
            None,
            None,
        )
        .await;
    run(&case_id, "ticket_opened", r)
}

#[derive(Deserialize)]
pub struct AssignForm {
    agent_id: String,
}

/// `POST /case/{id}/ticket/{ticket_id}/assign`.
pub async fn ticket_assign(
    State(state): State<AppState>,
    Path((case_id, ticket_id)): Path<(String, String)>,
    Form(form): Form<AssignForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.crm.assign_ticket(&ticket_id, &form.agent_id).await;
    run(&case_id, "ticket_assigned", r)
}

#[derive(Deserialize)]
pub struct TicketTransitionForm {
    to_state: String,
}

/// `POST /case/{id}/ticket/{ticket_id}/transition`.
///
/// The route validates `to_state` against the workbench-button targets, then the
/// client resolves target→trigger (an `in_progress` target costs one `get_ticket`
/// read, three triggers land there). Note `in_progress` IS a valid button target
/// even though it isn't a direct [`ticket_trigger_for_state`] key.
pub async fn ticket_transition(
    State(state): State<AppState>,
    Path((case_id, ticket_id)): Path<(String, String)>,
    Form(form): Form<TicketTransitionForm>,
) -> Response {
    let valid = TICKET_ACTIONS
        .iter()
        .flat_map(|(_, acts)| acts.iter().map(|(_, s)| *s))
        .any(|s| s == form.to_state);
    if !valid {
        return back_to_case(
            &case_id,
            "",
            &format!("Unknown ticket transition {}", py_repr(&form.to_state)),
        );
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    // Resolve target → trigger the same way the ticket.transition tool does.
    let r = resolve_and_transition(clients, &ticket_id, &form.to_state).await;
    run(&case_id, "ticket_transitioned", r)
}

/// Map a ticket target state to its trigger (reading current state for
/// `in_progress`), then POST the transition. Mirrors the client's
/// `transition_ticket(to_state=…)` mapping.
async fn resolve_and_transition(
    clients: &crate::clients::CockpitClients,
    ticket_id: &str,
    to_state: &str,
) -> Result<Value, ClientError> {
    let trigger = if to_state == "in_progress" {
        let current = clients.crm.get_ticket(ticket_id).await?;
        let src = current.get("state").and_then(Value::as_str).unwrap_or("");
        bss_clients::ticket_in_progress_trigger(src)
            .map(str::to_string)
            // The button only ever offers reachable transitions; an unreachable
            // one is a stale page — let the server's own validation answer.
            .unwrap_or_else(|| to_state.to_string())
    } else {
        ticket_trigger_for_state(to_state)
            .map(str::to_string)
            .unwrap_or_else(|| to_state.to_string())
    };
    clients.crm.transition_ticket(ticket_id, &trigger).await
}

#[derive(Deserialize)]
pub struct ResolveForm {
    resolution_notes: String,
}

/// `POST /case/{id}/ticket/{ticket_id}/resolve`.
pub async fn ticket_resolve(
    State(state): State<AppState>,
    Path((case_id, ticket_id)): Path<(String, String)>,
    Form(form): Form<ResolveForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .resolve_ticket(&ticket_id, form.resolution_notes.trim())
        .await;
    run(&case_id, "ticket_resolved", r)
}

/// Python's `repr()` of a string — single quotes. The transition error copy uses
/// `{trigger!r}`, so the operator sees `'foo'`, not `"foo"`.
fn py_repr(s: &str) -> String {
    format!("'{s}'")
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn actions_for_reads_the_fsm_tables() {
        assert_eq!(
            actions_for(CASE_ACTIONS, "open"),
            vec![json!(["Take", "take"]), json!(["Resolve", "resolve"])]
        );
        assert_eq!(actions_for(CASE_ACTIONS, "closed"), Vec::<Value>::new());
        // Unknown state → empty, never a panic.
        assert_eq!(actions_for(CASE_ACTIONS, "bogus"), Vec::<Value>::new());
        assert_eq!(
            actions_for(TICKET_ACTIONS, "resolved"),
            vec![json!(["Close", "closed"]), json!(["Reopen", "in_progress"])]
        );
    }

    #[test]
    fn transition_valid_sets_match_the_button_targets() {
        let case_triggers: Vec<&str> = CASE_ACTIONS
            .iter()
            .flat_map(|(_, a)| a.iter().map(|(_, t)| *t))
            .collect();
        assert!(case_triggers.contains(&"take"));
        assert!(case_triggers.contains(&"resume"));
        assert!(!case_triggers.contains(&"delete"));

        let ticket_targets: Vec<&str> = TICKET_ACTIONS
            .iter()
            .flat_map(|(_, a)| a.iter().map(|(_, s)| *s))
            .collect();
        // in_progress is a valid *button target* even though it isn't a direct
        // trigger key — the client resolves it from the ticket's current state.
        assert!(ticket_targets.contains(&"in_progress"));
        assert!(ticket_targets.contains(&"closed"));
    }

    #[test]
    fn py_repr_uses_single_quotes() {
        assert_eq!(py_repr("bogus"), "'bogus'");
    }
}
