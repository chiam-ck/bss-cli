//! v2.1 — the catalog-management conversation, driven through the real ReAct loop
//! with a scripted model. Pure (no DB/HTTP) — runs in CI.
//!
//! Proves the three properties the operator asked for, in the order a real turn
//! hits them:
//!   1. an under-specified call is bounced with `MISSING_REQUIRED_FIELDS` and never
//!      reaches the catalog service (the client points at a dead port — a call that
//!      got through would surface `CLIENT_ERROR` instead);
//!   2. that bounce does NOT count toward the 3-strike failure bail, so the model
//!      can ask the operator and continue the conversation;
//!   3. a complete call is `DESTRUCTIVE_OPERATION_BLOCKED` until `/confirm`, then
//!      executes on the authorised turn.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_orchestrator::{
    astream_once, default_registry, AgentConfig, AgentEvent, MockChatModel, ToolRegistry,
};

const FIXTURE: &str = r#"{
  "responses": [
    {
      "name": "under-specified-then-ask",
      "match": "add a new plan",
      "steps": [
        { "tool_calls": [{ "name": "catalog.add_offering", "args": { "name": "Lite" } }] },
        { "content": "What plan id, price, and data allowance should it have?" }
      ]
    },
    {
      "name": "no-allowance",
      "match": "plan with no allowances",
      "steps": [
        { "tool_calls": [{ "name": "catalog.add_offering",
            "args": { "offering_id": "PLAN_XS", "name": "Lite", "amount": "9.00" } }] },
        { "content": "How much data should PLAN_XS include?" }
      ]
    },
    {
      "name": "complete-call",
      "match": "add PLAN_XS at 9.00 with 5GB",
      "steps": [
        { "tool_calls": [{ "name": "catalog.add_offering",
            "args": { "offering_id": "PLAN_XS", "name": "Lite", "amount": "9.00",
                      "data_mb": 5120 } }] },
        { "content": "Staged." }
      ]
    },
    {
      "name": "retire",
      "match": "retire PLAN_XS",
      "steps": [
        { "tool_calls": [{ "name": "catalog.retire_offering",
            "args": { "offering_id": "PLAN_XS" } }] },
        { "content": "Staged." }
      ]
    },
    {
      "name": "three-elicitations",
      "match": "keep guessing",
      "steps": [
        { "tool_calls": [
          { "name": "catalog.add_offering", "args": { "name": "a" } },
          { "name": "catalog.add_offering", "args": { "name": "b" } },
          { "name": "catalog.add_offering", "args": { "name": "c" } }
        ] },
        { "content": "I need the plan id, price and allowance." }
      ]
    }
  ]
}"#;

/// The full operator surface for the catalog family. The client points at a dead
/// port on purpose: any call that clears the elicitation gate fails in transport,
/// which is how these tests tell "gated" apart from "executed".
fn registry() -> ToolRegistry {
    let mut reg = default_registry();
    let auth = Arc::new(TokenAuthProvider::new("x").unwrap());
    let catalog = CatalogClient::new("http://127.0.0.1:1", auth).unwrap();
    bss_orchestrator::tools::catalog::register_catalog_admin_write_tools(&mut reg, catalog);
    reg
}

async fn run(prompt: &str, allow_destructive: bool) -> Vec<AgentEvent> {
    let mut model = MockChatModel::from_json(FIXTURE).expect("parse fixture");
    let reg = registry();
    let config = AgentConfig {
        allow_destructive,
        tool_filter: Some("operator_cockpit".to_string()),
        model_name: "mock".to_string(),
        ..Default::default()
    };
    astream_once(&mut model, &reg, prompt, &config).await
}

/// Every `ToolCallCompleted` result body, in order.
fn tool_results(events: &[AgentEvent]) -> Vec<String> {
    events
        .iter()
        .filter_map(|e| match e {
            AgentEvent::ToolCallCompleted { result_full, .. } => Some(result_full.clone()),
            _ => None,
        })
        .collect()
}

fn final_text(events: &[AgentEvent]) -> String {
    events
        .iter()
        .find_map(|e| match e {
            AgentEvent::FinalMessage { text } => Some(text.clone()),
            _ => None,
        })
        .unwrap_or_default()
}

fn errors(events: &[AgentEvent]) -> Vec<String> {
    events
        .iter()
        .filter_map(|e| match e {
            AgentEvent::Error { message } => Some(message.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn an_under_specified_offering_is_bounced_back_as_a_question() {
    let events = run("add a new plan", true).await;
    let results = tool_results(&events);
    assert_eq!(results.len(), 1);
    assert!(
        results[0].contains("MISSING_REQUIRED_FIELDS"),
        "got {}",
        results[0]
    );
    // Named all at once, so the model asks one question rather than probing.
    assert!(results[0].contains("'offering_id'"));
    assert!(results[0].contains("'amount'"));
    // Never reached the (dead) service.
    assert!(!results[0].contains("CLIENT_ERROR"));
    // The turn survives to ask the operator.
    assert!(errors(&events).is_empty());
    assert!(final_text(&events).contains("What plan id"));
}

#[tokio::test]
async fn a_plan_that_grants_nothing_is_bounced_too() {
    let events = run("plan with no allowances", true).await;
    let results = tool_results(&events);
    assert!(
        results[0].contains("MISSING_REQUIRED_FIELDS"),
        "{results:?}"
    );
    assert!(results[0].contains("at least one allowance"), "{results:?}");
    assert!(final_text(&events).contains("How much data"));
}

/// The load-bearing one: an elicitation bounce must not burn the 3-strike budget.
/// If `MISSING_REQUIRED_FIELDS` ever joins `agent::FAILURE_MARKERS`, this fails and
/// the operator's multi-turn conversation dies on the third clarifying question.
#[tokio::test]
async fn elicitation_does_not_trip_the_three_strike_bail() {
    let events = run("keep guessing", true).await;
    assert_eq!(tool_results(&events).len(), 3);
    assert!(
        errors(&events).is_empty(),
        "elicitation must not bail the turn: {:?}",
        errors(&events)
    );
    assert!(final_text(&events).contains("I need the plan id"));
}

#[tokio::test]
async fn a_complete_offering_needs_confirm_before_it_executes() {
    // Without /confirm: staged as a proposal, nothing written.
    let events = run("add PLAN_XS at 9.00 with 5GB", false).await;
    let results = tool_results(&events);
    assert!(
        results[0].contains("DESTRUCTIVE_OPERATION_BLOCKED"),
        "got {}",
        results[0]
    );
    assert!(results[0].contains("catalog.add_offering"));

    // With /confirm (the authorised turn): the gate lets it through, so it reaches
    // the dead port and reports transport failure. Reaching transport IS the proof
    // that /confirm — and nothing else — is what stands between propose and write.
    let events = run("add PLAN_XS at 9.00 with 5GB", true).await;
    let results = tool_results(&events);
    assert!(
        !results[0].contains("DESTRUCTIVE_OPERATION_BLOCKED"),
        "got {}",
        results[0]
    );
    assert!(results[0].contains("CLIENT_ERROR"), "got {}", results[0]);
}

#[tokio::test]
async fn retire_is_confirm_gated_as_well() {
    let events = run("retire PLAN_XS", false).await;
    let results = tool_results(&events);
    assert!(
        results[0].contains("DESTRUCTIVE_OPERATION_BLOCKED"),
        "got {}",
        results[0]
    );
    assert!(results[0].contains("catalog.retire_offering"));
}
