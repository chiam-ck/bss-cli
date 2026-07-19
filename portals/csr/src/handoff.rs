//! `POST /cockpit/handoff` — CRM screens hand work to the chat. Port of
//! `bss_csr.routes.handoff`.
//!
//! v1.6 doctrine: the CRM screens are supplementary views; the conversation stays
//! the canonical write path for anything destructive, compound, or money-moving.
//! Every "Ask the agent" button POSTs here with an optional customer focus and a
//! drafted message. We open a fresh session (pinned to the customer when given) and
//! land the operator on the thread with the draft PREFILLED in the compose box —
//! never auto-sent. The operator reviews, edits, and presses Enter; destructive
//! verbs then ride the normal propose-then-`/confirm` contract unchanged.

use axum::extract::{Form, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_cockpit::OPERATOR_ACTOR;
use serde::Deserialize;

use crate::routes::urlencode;
use crate::AppState;

#[derive(Deserialize, Default)]
pub struct HandoffForm {
    #[serde(default)]
    customer_id: String,
    #[serde(default)]
    draft: String,
    #[serde(default)]
    label: String,
}

/// `POST /cockpit/handoff`.
pub async fn cockpit_handoff(
    State(state): State<AppState>,
    Form(form): Form<HandoffForm>,
) -> Response {
    let s = match crate::cockpit::store(&state) {
        Ok(s) => s,
        Err(r) => return r,
    };
    let customer_id = form.customer_id.trim();
    let draft = form.draft.trim();
    let label = form.label.trim();

    let conv = match s
        .open(
            OPERATOR_ACTOR,
            (!label.is_empty()).then_some(label),
            (!customer_id.is_empty()).then_some(customer_id),
            false,
            &crate::cockpit::tenant(),
        )
        .await
    {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "csr.handoff.open_failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not open a session",
            )
                .into_response();
        }
    };

    // The draft prefills the compose box (never auto-sent), capped at 2000 chars.
    let mut url = format!("/cockpit/{}", conv.session_id);
    if !draft.is_empty() {
        let capped: String = draft.chars().take(2000).collect();
        url.push_str(&format!("?draft={}", urlencode(&capped)));
    }
    tracing::info!(
        session_id = %conv.session_id,
        has_draft = !draft.is_empty(),
        "cockpit.handoff"
    );
    Redirect::to(&url).into_response()
}
