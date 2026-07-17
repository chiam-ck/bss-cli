//! In-memory chat state for the self-serve portal. Port of
//! `bss_self_serve.chat_session` (v0.12).
//!
//! Two collections, both single-process and TTL-evicted:
//!
//! * [`ChatConversationStore`] — one running conversation per customer. Holds the
//!   full message history (user + assistant turns) so the next `POST
//!   /chat/message` can render prior context into the system prompt. The
//!   conversation persists across page navigations within the portal: returning
//!   to `/chat` or popping the floating widget reloads the same thread.
//! * [`ChatTurnStore`] — one in-flight turn per stream. Keyed on a random
//!   `session_id` the SSE handler uses to find the customer's question for a
//!   given `GET /chat/events/{sid}`. A turn always has a back-pointer to the
//!   customer's conversation so the SSE handler can append the assistant's final
//!   text on completion.
//!
//! Single-process only; a later version can swap either store for Redis if
//! multiple portal replicas land.
//!
//! **Port note — shared mutation.** Python hands out the live `ChatConversation`
//! / `ChatTurn` object and the SSE handler mutates it in place (`conv.append`,
//! `turn.done = True`). Rust models that with `Arc<Mutex<..>>` values, so the
//! handler holds the same aliased, mutable state the oracle does.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use uuid::Uuid;

/// Who said it. Serialises to the `"user"` / `"assistant"` strings the templates
/// compare against.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    User,
    Assistant,
}

impl Role {
    pub fn as_str(&self) -> &'static str {
        match self {
            Role::User => "user",
            Role::Assistant => "assistant",
        }
    }

    /// The transcript label — `User:` / `Assistant:`.
    fn label(&self) -> &'static str {
        match self {
            Role::User => "User",
            Role::Assistant => "Assistant",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConversationMessage {
    pub role: Role,
    pub body: String,
}

/// One customer's running chat thread.
#[derive(Debug)]
pub struct ChatConversation {
    pub customer_id: String,
    pub messages: Vec<ConversationMessage>,
    last_active_at: Instant,
}

impl ChatConversation {
    fn new(customer_id: String) -> Self {
        Self {
            customer_id,
            messages: Vec::new(),
            last_active_at: Instant::now(),
        }
    }

    pub fn append(&mut self, role: Role, body: &str) {
        self.messages.push(ConversationMessage {
            role,
            body: body.to_string(),
        });
        self.last_active_at = Instant::now();
    }

    /// Render as `User: ...\nAssistant: ...` text suitable for the system
    /// prompt's prior-conversation context block and for `case.open_for_me`'s
    /// transcript hashing.
    ///
    /// **Frozen contract** — the transcript is SHA-256'd into
    /// `crm.case.chat_transcript_hash`, so the exact bytes matter: lines joined
    /// with `\n`, plus a single trailing `\n` when non-empty; empty string when
    /// there are no messages.
    pub fn transcript_text(&self) -> String {
        if self.messages.is_empty() {
            return String::new();
        }
        let mut out = self
            .messages
            .iter()
            .map(|m| format!("{}: {}", m.role.label(), m.body))
            .collect::<Vec<_>>()
            .join("\n");
        out.push('\n');
        out
    }
}

/// One in-flight chat turn (one SSE stream).
#[derive(Debug)]
pub struct ChatTurn {
    pub session_id: String,
    pub customer_id: String,
    pub question: String,
    created_at: Instant,
    pub done: bool,
    pub error: Option<String>,
    pub final_text: String,
    pub ownership_violation: bool,
}

/// One conversation per customer_id. Bounded TTL idle eviction.
pub struct ChatConversationStore {
    ttl: Duration,
    items: Mutex<HashMap<String, Arc<Mutex<ChatConversation>>>>,
}

impl ChatConversationStore {
    pub fn new(ttl_seconds: u64) -> Self {
        Self {
            ttl: Duration::from_secs(ttl_seconds),
            items: Mutex::new(HashMap::new()),
        }
    }

    pub fn get_or_create(&self, customer_id: &str) -> Arc<Mutex<ChatConversation>> {
        let mut items = lock(&self.items);
        self.prune_locked(&mut items);
        items
            .entry(customer_id.to_string())
            .or_insert_with(|| Arc::new(Mutex::new(ChatConversation::new(customer_id.to_string()))))
            .clone()
    }

    pub fn get(&self, customer_id: &str) -> Option<Arc<Mutex<ChatConversation>>> {
        let mut items = lock(&self.items);
        self.prune_locked(&mut items);
        items.get(customer_id).cloned()
    }

    pub fn reset(&self, customer_id: &str) {
        lock(&self.items).remove(customer_id);
    }

    fn prune_locked(&self, items: &mut HashMap<String, Arc<Mutex<ChatConversation>>>) {
        let now = Instant::now();
        let ttl = self.ttl;
        items.retain(|_, conv| {
            let last = lock(conv).last_active_at;
            now.duration_since(last) < ttl
        });
    }
}

impl Default for ChatConversationStore {
    fn default() -> Self {
        Self::new(3600)
    }
}

/// Per-stream lookup. The conversation store owns the durable history; this
/// store is the per-SSE-stream working set.
pub struct ChatTurnStore {
    ttl: Duration,
    items: Mutex<HashMap<String, Arc<Mutex<ChatTurn>>>>,
}

impl ChatTurnStore {
    pub fn new(ttl_seconds: u64) -> Self {
        Self {
            ttl: Duration::from_secs(ttl_seconds),
            items: Mutex::new(HashMap::new()),
        }
    }

    pub fn create(&self, customer_id: &str, question: &str) -> Arc<Mutex<ChatTurn>> {
        let turn = Arc::new(Mutex::new(ChatTurn {
            session_id: Uuid::new_v4().simple().to_string(),
            customer_id: customer_id.to_string(),
            question: question.to_string(),
            created_at: Instant::now(),
            done: false,
            error: None,
            final_text: String::new(),
            ownership_violation: false,
        }));
        let session_id = lock(&turn).session_id.clone();
        let mut items = lock(&self.items);
        self.prune_locked(&mut items);
        items.insert(session_id, turn.clone());
        turn
    }

    pub fn get(&self, session_id: &str) -> Option<Arc<Mutex<ChatTurn>>> {
        let mut items = lock(&self.items);
        self.prune_locked(&mut items);
        items.get(session_id).cloned()
    }

    fn prune_locked(&self, items: &mut HashMap<String, Arc<Mutex<ChatTurn>>>) {
        let now = Instant::now();
        let ttl = self.ttl;
        items.retain(|_, turn| now.duration_since(lock(turn).created_at) < ttl);
    }
}

impl Default for ChatTurnStore {
    fn default() -> Self {
        Self::new(1800)
    }
}

/// These maps are process-local caches; a poisoned lock is recoverable.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn transcript_text_is_empty_without_messages() {
        let conv = ChatConversation::new("CUST-1".to_string());
        assert_eq!(conv.transcript_text(), "");
    }

    #[test]
    fn transcript_text_format_is_the_frozen_contract() {
        // Byte-exact: "<Label>: <body>" lines joined by \n, single trailing \n.
        // This text is SHA-256'd into crm.case.chat_transcript_hash.
        let mut conv = ChatConversation::new("CUST-1".to_string());
        conv.append(Role::User, "hi there");
        conv.append(Role::Assistant, "hello!");
        conv.append(Role::User, "bye");
        assert_eq!(
            conv.transcript_text(),
            "User: hi there\nAssistant: hello!\nUser: bye\n"
        );
    }

    #[test]
    fn transcript_text_single_message() {
        let mut conv = ChatConversation::new("CUST-1".to_string());
        conv.append(Role::User, "solo");
        assert_eq!(conv.transcript_text(), "User: solo\n");
    }

    /// The transcript's *hash* is what actually lands in
    /// `crm.case.chat_transcript_hash` via `case.open_for_me`, so pin the digest
    /// itself — golden, captured from the running Python oracle. A drift in the
    /// join/trailing-newline rules changes this value.
    #[test]
    fn transcript_hash_matches_the_oracle() {
        use sha2::{Digest, Sha256};
        let mut conv = ChatConversation::new("CUST-1".to_string());
        conv.append(Role::User, "hi there");
        conv.append(Role::Assistant, "hello!");
        conv.append(Role::User, "bye");
        let digest = Sha256::digest(conv.transcript_text().as_bytes());
        assert_eq!(
            format!("{digest:x}"),
            "cad2a20c74d831e46537084563f7b7e262163f0bd220743eadd9b2506c0257a2"
        );
    }

    #[test]
    fn conversation_store_get_or_create_is_stable() {
        let store = ChatConversationStore::default();
        let a = store.get_or_create("CUST-1");
        lock(&a).append(Role::User, "first");
        // Same key → the same conversation, history intact.
        let b = store.get_or_create("CUST-1");
        assert_eq!(lock(&b).messages.len(), 1);
        assert_eq!(lock(&b).messages[0].body, "first");
    }

    #[test]
    fn conversation_store_isolates_customers() {
        let store = ChatConversationStore::default();
        lock(&store.get_or_create("CUST-1")).append(Role::User, "mine");
        assert!(store.get("CUST-2").is_none());
        assert_eq!(lock(&store.get_or_create("CUST-2")).messages.len(), 0);
    }

    #[test]
    fn conversation_store_reset_clears_history() {
        let store = ChatConversationStore::default();
        lock(&store.get_or_create("CUST-1")).append(Role::User, "old");
        store.reset("CUST-1");
        assert!(store.get("CUST-1").is_none());
        // Next message starts a fresh history — no prior context.
        assert_eq!(lock(&store.get_or_create("CUST-1")).messages.len(), 0);
    }

    #[test]
    fn conversation_store_evicts_idle() {
        let store = ChatConversationStore::new(0);
        store.get_or_create("CUST-1");
        assert!(store.get("CUST-1").is_none(), "ttl=0 → immediately idle");
    }

    #[test]
    fn turn_store_roundtrip() {
        let store = ChatTurnStore::default();
        let turn = store.create("CUST-1", "what's my balance?");
        let sid = lock(&turn).session_id.clone();
        assert_eq!(sid.len(), 32, "uuid4 hex, matching Python's uuid4().hex");

        let found = store.get(&sid).expect("turn is retrievable by session id");
        assert_eq!(lock(&found).customer_id, "CUST-1");
        assert_eq!(lock(&found).question, "what's my balance?");
        assert!(!lock(&found).done);

        // The handle is shared, not a copy — the SSE handler's mutations are
        // visible to a later lookup (Python hands out the live object).
        lock(&found).done = true;
        assert!(lock(&store.get(&sid).unwrap()).done);
    }

    #[test]
    fn turn_store_sessions_are_unique() {
        let store = ChatTurnStore::default();
        let a = lock(&store.create("CUST-1", "q")).session_id.clone();
        let b = lock(&store.create("CUST-1", "q")).session_id.clone();
        assert_ne!(a, b);
    }

    #[test]
    fn turn_store_unknown_session() {
        let store = ChatTurnStore::default();
        assert!(store.get("nope").is_none());
    }

    #[test]
    fn turn_store_evicts_expired() {
        let store = ChatTurnStore::new(0);
        let turn = store.create("CUST-1", "q");
        let sid = lock(&turn).session_id.clone();
        assert!(store.get(&sid).is_none(), "ttl=0 → immediately expired");
    }
}
