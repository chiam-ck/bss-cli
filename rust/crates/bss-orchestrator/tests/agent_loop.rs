//! Fixture-driven ReAct-loop transcript test. Pure (frozen clock, no DB/HTTP) —
//! runs in CI. Proves the P5c slice-1 core: the loop drives the MockChatModel
//! fixture player, executes tools, emits the AgentEvent sequence, and the guard
//! stack (destructive gating, 3-strike failure bail, identical-call stuck bail).
//!
//! All cases run in one test because the clock is a process-global (freeze once).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_orchestrator::{astream_once, default_registry, AgentConfig, AgentEvent, MockChatModel};
use chrono::TimeZone;

const FIXTURE: &str = r#"{
  "responses": [
    {
      "name": "clock-roundtrip",
      "match": "what time",
      "steps": [
        { "tool_calls": [{ "name": "clock.now", "args": {} }] },
        { "content": "Checked the clock." }
      ]
    },
    {
      "name": "destructive-block",
      "match": "terminate the line",
      "steps": [
        { "tool_calls": [{ "name": "subscription.terminate", "args": { "subscription_id": "SUB-1" } }] },
        { "content": "That needs a /confirm." }
      ]
    },
    {
      "name": "failure-bail",
      "match": "break it",
      "steps": [
        { "tool_calls": [
          { "name": "customer.get", "args": { "customer_id": "CUST-1" } },
          { "name": "customer.get", "args": { "customer_id": "CUST-1" } },
          { "name": "customer.get", "args": { "customer_id": "CUST-1" } }
        ] }
      ]
    },
    {
      "name": "stuck-bail",
      "match": "freeze it thrice",
      "steps": [
        { "tool_calls": [
          { "name": "clock.freeze", "args": {} },
          { "name": "clock.freeze", "args": {} },
          { "name": "clock.freeze", "args": {} }
        ] }
      ]
    }
  ]
}"#;

fn kinds(events: &[AgentEvent]) -> Vec<String> {
    events
        .iter()
        .map(|e| e.to_value()["event"].as_str().unwrap().to_string())
        .collect()
}

async fn run(prompt: &str, allow_destructive: bool) -> Vec<AgentEvent> {
    let mut model = MockChatModel::from_json(FIXTURE).expect("parse fixture");
    let registry = default_registry();
    let config = AgentConfig {
        allow_destructive,
        model_name: "mock".to_string(),
        ..Default::default()
    };
    astream_once(&mut model, &registry, prompt, &config).await
}

#[tokio::test]
async fn react_loop_transcripts() {
    // Freeze the clock so clock.now is deterministic.
    let fixed = chrono::Utc.with_ymd_and_hms(2026, 7, 13, 12, 0, 0).unwrap();
    bss_clock::freeze(Some(fixed));

    // ── A. happy tool round trip ────────────────────────────────────────────
    let ev = run("what time is it?", false).await;
    assert_eq!(
        kinds(&ev),
        vec![
            "prompt_received",
            "tool_call_started",
            "tool_call_completed",
            "turn_usage",
            "final_message"
        ]
    );
    let started = &ev[1].to_value();
    assert_eq!(started["name"], "clock.now");
    assert_eq!(started["call_id"], "mock_call_1_0");
    let completed = &ev[2].to_value();
    assert_eq!(
        completed["result_full"],
        "{\"now\":\"2026-07-13T12:00:00+00:00\",\"source\":\"system\"}"
    );
    assert_eq!(completed["is_error"], false);
    assert_eq!(ev[4].to_value()["text"], "Checked the clock.");

    // ── B. destructive block (allow_destructive=false) ──────────────────────
    let ev = run("please terminate the line", false).await;
    assert_eq!(
        kinds(&ev),
        vec![
            "prompt_received",
            "tool_call_started",
            "tool_call_completed",
            "turn_usage",
            "final_message"
        ]
    );
    let blocked = &ev[2].to_value();
    assert_eq!(blocked["is_error"], false);
    assert!(blocked["result_full"]
        .as_str()
        .unwrap()
        .contains("DESTRUCTIVE_OPERATION_BLOCKED"));
    assert_eq!(ev[4].to_value()["text"], "That needs a /confirm.");

    // With allow_destructive=true the gate opens; the (unregistered-in-slice-1)
    // tool then reports Unknown tool — the point is the gate no longer blocks.
    let ev = run("please terminate the line", true).await;
    let after_gate = &ev[2].to_value();
    assert!(
        !after_gate["result_full"]
            .as_str()
            .unwrap()
            .contains("DESTRUCTIVE_OPERATION_BLOCKED"),
        "allow_destructive=true should pass the gate"
    );

    // ── C. 3-strike failure bail (unknown tool, not destructive) ────────────
    let ev = run("break it now", false).await;
    let k = kinds(&ev);
    assert_eq!(k.last().unwrap(), "error", "failure bail ends in error");
    assert!(!k.contains(&"final_message".to_string()));
    assert!(!k.contains(&"turn_usage".to_string()));
    // three (started, completed) pairs before the bail
    assert_eq!(k.iter().filter(|s| *s == "tool_call_completed").count(), 3);
    assert!(ev.last().unwrap().to_value()["message"]
        .as_str()
        .unwrap()
        .contains("3 consecutive tool failures"));

    // ── D. identical-call stuck bail (repeated success-shaped result) ───────
    let ev = run("freeze it thrice", false).await;
    let k = kinds(&ev);
    assert_eq!(k.last().unwrap(), "error", "stuck bail ends in error");
    assert_eq!(k.iter().filter(|s| *s == "tool_call_completed").count(), 3);
    assert!(ev.last().unwrap().to_value()["message"]
        .as_str()
        .unwrap()
        .contains("identical calls"));

    bss_clock::reset_for_tests();
}
