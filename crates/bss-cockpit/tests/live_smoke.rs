//! Live store smoke — the Conversation store against the real `cockpit` schema.
//! `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-cockpit
//! cargo test -p bss-cockpit --test live_smoke -- --ignored --nocapture
//! ```
//!
//! Pins the transcript_text frozen contract (chrome dropped, tool prefix), the
//! structured message view, and the pending-destructive round trip incl. the
//! stored-JSON key-order preservation. Creates its own session and deletes it +
//! its rows afterwards — no shared-state residue.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_cockpit::ConversationStore;
use indexmap::IndexMap;
use serde_json::{json, Value};
use sqlx::Executor;

fn normalize_db_url(raw: &str) -> String {
    raw.replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://")
}

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn conversation_store_round_trip() {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&url).await.expect("connect live Postgres");
    let store = ConversationStore::new(pool.clone());

    let mut conv = store
        .open(
            "operator-rust-smoke",
            Some("rust-live-smoke"),
            None,
            false,
            "DEFAULT",
        )
        .await
        .expect("open session");
    let sid = conv.session_id.clone();

    // Append a mix of turns incl. a chrome assistant row that must be dropped.
    conv.append_user_turn("hello").await.unwrap();
    conv.append_assistant_turn("hi there", None).await.unwrap();
    conv.append_assistant_turn("(no reply)", None)
        .await
        .unwrap();
    let tool_msg_id = conv
        .append_tool_turn("customer.get", "{\"id\": \"CUST-1\"}")
        .await
        .unwrap();

    // transcript_text: chrome dropped, tool prefixed, blank line between turns.
    let transcript = conv.transcript_text().await.unwrap();
    let expected =
        "user:\nhello\n\nassistant:\nhi there\n\ntool[customer.get]:\n{\"id\": \"CUST-1\"}";
    assert_eq!(transcript, expected, "transcript_text contract drift");

    // structured view: 4 rows, tool row carries its name.
    let msgs = conv.list_messages().await.unwrap();
    assert_eq!(msgs.len(), 4);
    let tool_row = msgs.iter().find(|m| m.role == "tool").unwrap();
    assert_eq!(tool_row.tool_name.as_deref(), Some("customer.get"));

    // pending-destructive round trip with key-order preservation.
    let mut args: IndexMap<String, Value> = IndexMap::new();
    args.insert("subscription_id".into(), json!("SUB-0005"));
    args.insert("reason".into(), json!("fraud"));
    conv.set_pending_destructive("subscription.terminate", &args, tool_msg_id)
        .await
        .unwrap();

    let peeked = conv.peek_pending_destructive().await.unwrap().unwrap();
    assert_eq!(peeked.tool_name, "subscription.terminate");
    assert_eq!(peeked.proposal_message_id, tool_msg_id);
    let keys: Vec<&str> = peeked.tool_args.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        vec!["subscription_id", "reason"],
        "stored key order lost"
    );

    let consumed = conv.consume_pending_destructive().await.unwrap().unwrap();
    assert_eq!(consumed.tool_name, "subscription.terminate");
    assert!(
        conv.peek_pending_destructive().await.unwrap().is_none(),
        "consume should have deleted the row"
    );

    // resume picks up the same session, then close flips state.
    let resumed = store.resume(&sid).await.unwrap();
    assert_eq!(resumed.actor, "operator-rust-smoke");
    conv.close().await.unwrap();
    assert_eq!(conv.state, "closed");

    // cleanup — remove the test session + its rows.
    pool.execute(sqlx::query("DELETE FROM cockpit.message WHERE session_id = $1").bind(&sid))
        .await
        .unwrap();
    pool.execute(
        sqlx::query("DELETE FROM cockpit.pending_destructive WHERE session_id = $1").bind(&sid),
    )
    .await
    .unwrap();
    pool.execute(sqlx::query("DELETE FROM cockpit.session WHERE id = $1").bind(&sid))
        .await
        .unwrap();
}
