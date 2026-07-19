//! End-to-end live smoke — a real agent turn driven by the OpenRouter model against
//! the running stack. `#[ignore]` (paid, non-deterministic); the human soak seed.
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test openrouter_agent_live -- --ignored --nocapture
//! ```
//!
//! Exercises the whole loop: `OpenRouterChatModel` → `astream_once` → the ported
//! catalog read tools → a final message. Assertions are tolerant (the model's phrasing
//! varies); the smoke fails only on a loop error or a missing final message.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_orchestrator::{
    astream_once, prompts::SYSTEM_PROMPT, AgentConfig, AgentEvent, OpenRouterChatModel,
    ToolRegistry,
};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

#[tokio::test]
#[ignore = "paid + non-deterministic OpenRouter call; run with --ignored"]
async fn one_real_agent_turn_lists_plans() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let catalog = CatalogClient::new(
        env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".to_string()),
        Arc::new(TokenAuthProvider::new(token).unwrap()),
    )
    .unwrap();

    let mut registry = ToolRegistry::new();
    bss_orchestrator::tools::catalog::register_catalog_tools(&mut registry, catalog);

    let mut model = OpenRouterChatModel::from_env().expect("BSS_LLM_API_KEY must be set");

    let config = AgentConfig {
        system_prompt: SYSTEM_PROMPT.to_string(),
        ..Default::default()
    };

    let events = astream_once(
        &mut model,
        &registry,
        "List the mobile plans we currently offer, with their monthly prices.",
        &config,
    )
    .await;

    // Print the event stream for the human running the soak.
    for ev in &events {
        println!("{}", ev.to_value());
    }

    assert!(
        !events.iter().any(|e| matches!(e, AgentEvent::Error { .. })),
        "the loop produced an Error event"
    );
    let final_text = events.iter().rev().find_map(|e| match e {
        AgentEvent::FinalMessage { text } => Some(text.clone()),
        _ => None,
    });
    let final_text = final_text.expect("a FinalMessage event");
    assert!(!final_text.trim().is_empty(), "final message is non-empty");
    assert!(
        !final_text.starts_with("(model error"),
        "the model call failed: {final_text}"
    );
}
