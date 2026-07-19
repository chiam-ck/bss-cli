//! Conversation + ConversationStore — Postgres-backed cockpit session store.
//! Port of `packages/bss-cockpit/bss_cockpit/conversation.py`.
//!
//! Schema: `cockpit.session` / `cockpit.message` / `cockpit.pending_destructive`
//! (alembic 0014). Both cockpit surfaces (CLI REPL + browser veneer) read and
//! write through this one store — no second store, no in-memory shadow. That
//! single-store invariant is the whole point of v0.13.
//!
//! [`Conversation::transcript_text`] is the frozen contract the orchestrator's
//! transcript parser consumes: `role:\ncontent` blocks joined by a blank line, in
//! `created_at` order, with cockpit chrome rows filtered out.

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use serde_json::Value;
use sqlx::{PgPool, Row};

use crate::chrome_filter::is_cockpit_chrome;

/// Errors from the store.
#[derive(Debug)]
pub enum ConversationError {
    Db(sqlx::Error),
    NotFound(String),
}

impl std::fmt::Display for ConversationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConversationError::Db(e) => write!(f, "cockpit store db error: {e}"),
            ConversationError::NotFound(id) => write!(f, "cockpit session {id:?} not found"),
        }
    }
}

impl std::error::Error for ConversationError {}

impl From<sqlx::Error> for ConversationError {
    fn from(e: sqlx::Error) -> Self {
        ConversationError::Db(e)
    }
}

type Result<T> = std::result::Result<T, ConversationError>;

// ── public row shapes ───────────────────────────────────────────────────────

/// One row from `cockpit.message`, structured for render consumption (avoids the
/// lossy transcript re-parse round trip).
#[derive(Debug, Clone)]
pub struct ConversationMessage {
    pub id: i64,
    pub role: String,
    pub content: String,
    pub tool_name: Option<String>,
    pub created_at: DateTime<Utc>,
}

/// One row of [`ConversationStore::list_for`] output.
#[derive(Debug, Clone)]
pub struct ConversationSummary {
    pub session_id: String,
    pub actor: String,
    pub label: Option<String>,
    pub customer_focus: Option<String>,
    pub state: String,
    pub started_at: DateTime<Utc>,
    pub last_active_at: DateTime<Utc>,
    pub message_count: i64,
}

/// Returned by [`Conversation::consume_pending_destructive`] on hit — the agent's
/// prior proposal, so the next turn can flip `allow_destructive=true` and run the
/// named tool. `tool_args` preserves the stored JSON key order (the `::text`
/// read + `IndexMap` parse) so the prompt's arg echo matches the oracle.
#[derive(Debug, Clone)]
pub struct PendingDestructive {
    pub tool_name: String,
    pub tool_args: IndexMap<String, Value>,
    pub proposal_message_id: i64,
    pub proposed_at: DateTime<Utc>,
}

// ── store ───────────────────────────────────────────────────────────────────

fn new_session_id() -> String {
    // `SES-YYYYMMDD-<8 hex>` — readable date + rare-collision suffix.
    let today = bss_clock::now().format("%Y%m%d");
    format!("SES-{today}-{:08x}", rand::random::<u32>())
}

/// Postgres-backed factory. Construct from a [`PgPool`]; every method runs a
/// short-lived query against it (SQLAlchemy's short-lived-session pattern).
#[derive(Clone)]
pub struct ConversationStore {
    pool: PgPool,
}

impl ConversationStore {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Insert a fresh session row and return a handle.
    pub async fn open(
        &self,
        actor: &str,
        label: Option<&str>,
        customer_focus: Option<&str>,
        allow_destructive: bool,
        tenant_id: &str,
    ) -> Result<Conversation> {
        if actor.is_empty() {
            return Err(ConversationError::Db(sqlx::Error::Protocol(
                "Conversation.open: actor must be non-empty".into(),
            )));
        }
        let session_id = new_session_id();
        let now = bss_clock::now();
        sqlx::query(
            r#"
            INSERT INTO cockpit.session (
                id, actor, customer_focus, allow_destructive,
                state, started_at, last_active_at, label, tenant_id
            )
            VALUES ($1, $2, $3, $4, 'active', $5, $5, $6, $7)
            "#,
        )
        .bind(&session_id)
        .bind(actor)
        .bind(customer_focus)
        .bind(allow_destructive)
        .bind(now)
        .bind(label)
        .bind(tenant_id)
        .execute(&self.pool)
        .await?;
        tracing::info!(
            session_id = %session_id,
            actor,
            "cockpit.session.opened"
        );
        Ok(Conversation {
            pool: self.pool.clone(),
            session_id,
            actor: actor.to_string(),
            customer_focus: customer_focus.map(str::to_string),
            allow_destructive,
            state: "active".to_string(),
            label: label.map(str::to_string),
            started_at: now,
            last_active_at: now,
        })
    }

    /// Re-load an existing session by id. Touches `last_active_at`.
    pub async fn resume(&self, session_id: &str) -> Result<Conversation> {
        let row = sqlx::query(
            r#"
            SELECT id, actor, customer_focus, allow_destructive,
                   state, started_at, last_active_at, label
            FROM cockpit.session
            WHERE id = $1
            "#,
        )
        .bind(session_id)
        .fetch_optional(&self.pool)
        .await?;
        let Some(row) = row else {
            return Err(ConversationError::NotFound(session_id.to_string()));
        };
        let now = bss_clock::now();
        sqlx::query("UPDATE cockpit.session SET last_active_at = $1 WHERE id = $2")
            .bind(now)
            .bind(session_id)
            .execute(&self.pool)
            .await?;
        Ok(Conversation {
            pool: self.pool.clone(),
            session_id: row.try_get("id")?,
            actor: row.try_get("actor")?,
            customer_focus: row.try_get("customer_focus")?,
            allow_destructive: row.try_get("allow_destructive")?,
            state: row.try_get("state")?,
            label: row.try_get("label")?,
            started_at: row.try_get("started_at")?,
            last_active_at: now,
        })
    }

    /// Sessions for `actor`, newest first. `active_only` excludes closed.
    pub async fn list_for(
        &self,
        actor: &str,
        active_only: bool,
        limit: i64,
    ) -> Result<Vec<ConversationSummary>> {
        let clause = if active_only {
            "WHERE actor = $1 AND state = 'active'"
        } else {
            "WHERE actor = $1"
        };
        let sql = format!(
            r#"
            SELECT s.id, s.actor, s.label, s.customer_focus,
                   s.state, s.started_at, s.last_active_at,
                   COALESCE(m.cnt, 0) AS message_count
            FROM cockpit.session s
            LEFT JOIN (
                SELECT session_id, COUNT(*) AS cnt
                FROM cockpit.message
                GROUP BY session_id
            ) m ON m.session_id = s.id
            {clause}
            ORDER BY s.last_active_at DESC
            LIMIT $2
            "#
        );
        let rows = sqlx::query(&sql)
            .bind(actor)
            .bind(limit)
            .fetch_all(&self.pool)
            .await?;
        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            out.push(ConversationSummary {
                session_id: r.try_get("id")?,
                actor: r.try_get("actor")?,
                label: r.try_get("label")?,
                customer_focus: r.try_get("customer_focus")?,
                state: r.try_get("state")?,
                started_at: r.try_get("started_at")?,
                last_active_at: r.try_get("last_active_at")?,
                message_count: r.try_get("message_count")?,
            });
        }
        Ok(out)
    }
}

// ── handle ──────────────────────────────────────────────────────────────────

/// Handle on one cockpit session. Mutating methods write Postgres and update the
/// cached fields. Concurrent writes from another surface are not live — consumers
/// re-resume to pick up changes (v0.13 is single-operator-by-design).
#[derive(Clone)]
pub struct Conversation {
    pool: PgPool,
    pub session_id: String,
    pub actor: String,
    pub customer_focus: Option<String>,
    pub allow_destructive: bool,
    pub state: String,
    pub label: Option<String>,
    pub started_at: DateTime<Utc>,
    pub last_active_at: DateTime<Utc>,
}

impl Conversation {
    pub async fn append_user_turn(&mut self, content: &str) -> Result<i64> {
        self.append_message("user", content, None).await
    }

    pub async fn append_assistant_turn(
        &mut self,
        content: &str,
        tool_calls_json: Option<&Value>,
    ) -> Result<i64> {
        self.append_message("assistant", content, tool_calls_json)
            .await
    }

    /// Append a tool-role message — the tool's stringified result. `tool_calls_json`
    /// is stored as `{"tool_name": <name>}` so `list_messages` / `transcript_text`
    /// can recover the name.
    pub async fn append_tool_turn(&mut self, tool_name: &str, content: &str) -> Result<i64> {
        let tc = serde_json::json!({ "tool_name": tool_name });
        self.append_message("tool", content, Some(&tc)).await
    }

    async fn append_message(
        &mut self,
        role: &str,
        content: &str,
        tool_calls_json: Option<&Value>,
    ) -> Result<i64> {
        let now = bss_clock::now();
        let tc_text: Option<String> = tool_calls_json.map(to_json);
        let row = sqlx::query(
            r#"
            INSERT INTO cockpit.message (
                session_id, role, content, tool_calls_json, created_at, tenant_id
            )
            VALUES ($1, $2, $3, CAST($4 AS json), $5, 'DEFAULT')
            RETURNING id
            "#,
        )
        .bind(&self.session_id)
        .bind(role)
        .bind(content)
        .bind(tc_text)
        .bind(now)
        .fetch_one(&self.pool)
        .await?;
        sqlx::query("UPDATE cockpit.session SET last_active_at = $1 WHERE id = $2")
            .bind(now)
            .bind(&self.session_id)
            .execute(&self.pool)
            .await?;
        self.last_active_at = now;
        Ok(row.try_get("id")?)
    }

    /// Structured view of the message log, in `created_at, id` order.
    pub async fn list_messages(&self) -> Result<Vec<ConversationMessage>> {
        let rows = sqlx::query(
            r#"
            SELECT id, role, content, tool_calls_json, created_at
            FROM cockpit.message
            WHERE session_id = $1
            ORDER BY created_at, id
            "#,
        )
        .bind(&self.session_id)
        .fetch_all(&self.pool)
        .await?;
        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let role: String = r.try_get("role")?;
            let tc: Option<Value> = r.try_get("tool_calls_json")?;
            let tool_name = if role == "tool" {
                tc.as_ref()
                    .and_then(|v| v.get("tool_name"))
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
            } else {
                None
            };
            out.push(ConversationMessage {
                id: r.try_get("id")?,
                role,
                content: r.try_get("content")?,
                tool_name,
                created_at: r.try_get("created_at")?,
            });
        }
        Ok(out)
    }

    /// Plain-text transcript for `astream_once(transcript=…)`. `role:\ncontent`
    /// blocks joined by a blank line, in `created_at` order; tool rows carry a
    /// `tool[NAME]:` prefix; assistant chrome rows are dropped (see
    /// [`crate::chrome_filter`]). This format is a frozen contract.
    pub async fn transcript_text(&self) -> Result<String> {
        let rows = sqlx::query(
            r#"
            SELECT role, content, tool_calls_json
            FROM cockpit.message
            WHERE session_id = $1
            ORDER BY created_at, id
            "#,
        )
        .bind(&self.session_id)
        .fetch_all(&self.pool)
        .await?;
        let mut out: Vec<String> = Vec::new();
        for r in rows {
            let role: String = r.try_get("role")?;
            let content: String = r.try_get("content")?;
            if role == "tool" {
                let tc: Option<Value> = r.try_get("tool_calls_json")?;
                let tool_name = tc
                    .as_ref()
                    .and_then(|v| v.get("tool_name"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let prefix = if tool_name.is_empty() {
                    "tool".to_string()
                } else {
                    format!("tool[{tool_name}]")
                };
                out.push(format!("{prefix}:\n{content}"));
            } else if role == "assistant" && is_cockpit_chrome(&content) {
                // Drop the chrome row — the LLM never said it, so showing it back
                // as "prior reasoning" only invites mimicry.
                continue;
            } else {
                out.push(format!("{role}:\n{content}"));
            }
        }
        Ok(out.join("\n\n"))
    }

    /// Clear all messages on this session (keeps the row). Also clears any
    /// pending-destructive row (explicit, though the FK would CASCADE).
    pub async fn reset(&self) -> Result<()> {
        sqlx::query("DELETE FROM cockpit.message WHERE session_id = $1")
            .bind(&self.session_id)
            .execute(&self.pool)
            .await?;
        sqlx::query("DELETE FROM cockpit.pending_destructive WHERE session_id = $1")
            .bind(&self.session_id)
            .execute(&self.pool)
            .await?;
        tracing::info!(session_id = %self.session_id, "cockpit.session.reset");
        Ok(())
    }

    /// Mark this session `state='closed'`. Idempotent.
    pub async fn close(&mut self) -> Result<()> {
        sqlx::query(
            "UPDATE cockpit.session SET state = 'closed', last_active_at = $1 WHERE id = $2",
        )
        .bind(bss_clock::now())
        .bind(&self.session_id)
        .execute(&self.pool)
        .await?;
        self.state = "closed".to_string();
        Ok(())
    }

    /// Pin a customer for the system-prompt focus block. `None` clears.
    pub async fn set_focus(&mut self, customer_id: Option<&str>) -> Result<()> {
        sqlx::query("UPDATE cockpit.session SET customer_focus = $1 WHERE id = $2")
            .bind(customer_id)
            .bind(&self.session_id)
            .execute(&self.pool)
            .await?;
        self.customer_focus = customer_id.map(str::to_string);
        Ok(())
    }

    /// Stash an in-flight propose. Replaces any prior unconfirmed row.
    pub async fn set_pending_destructive(
        &self,
        tool_name: &str,
        args: &IndexMap<String, Value>,
        proposal_message_id: i64,
    ) -> Result<()> {
        let args_text = serde_json::to_string(args).unwrap_or_else(|_| "{}".to_string());
        sqlx::query(
            r#"
            INSERT INTO cockpit.pending_destructive (
                session_id, proposed_at, tool_name,
                tool_args_json, proposal_message_id, tenant_id
            )
            VALUES ($1, $2, $3, CAST($4 AS json), $5, 'DEFAULT')
            ON CONFLICT (session_id) DO UPDATE SET
                proposed_at = EXCLUDED.proposed_at,
                tool_name = EXCLUDED.tool_name,
                tool_args_json = EXCLUDED.tool_args_json,
                proposal_message_id = EXCLUDED.proposal_message_id
            "#,
        )
        .bind(&self.session_id)
        .bind(bss_clock::now())
        .bind(tool_name)
        .bind(args_text)
        .bind(proposal_message_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    /// Read the in-flight propose row WITHOUT deleting it. `None` if absent.
    pub async fn peek_pending_destructive(&self) -> Result<Option<PendingDestructive>> {
        let row = sqlx::query(
            r#"
            SELECT tool_name, tool_args_json::text AS args_text,
                   proposal_message_id, proposed_at
            FROM cockpit.pending_destructive
            WHERE session_id = $1
            "#,
        )
        .bind(&self.session_id)
        .fetch_optional(&self.pool)
        .await?;
        row.map(pending_from_row).transpose()
    }

    /// Atomically read+delete the in-flight propose row. `None` if absent.
    pub async fn consume_pending_destructive(&self) -> Result<Option<PendingDestructive>> {
        let row = sqlx::query(
            r#"
            DELETE FROM cockpit.pending_destructive
            WHERE session_id = $1
            RETURNING tool_name, tool_args_json::text AS args_text,
                      proposal_message_id, proposed_at
            "#,
        )
        .bind(&self.session_id)
        .fetch_optional(&self.pool)
        .await?;
        row.map(pending_from_row).transpose()
    }
}

fn pending_from_row(r: sqlx::postgres::PgRow) -> Result<PendingDestructive> {
    let args_text: Option<String> = r.try_get("args_text")?;
    // `tool_args_json::text` preserves the stored JSON key order; parse into an
    // IndexMap so the prompt's arg echo matches the oracle's insertion order.
    let tool_args: IndexMap<String, Value> = args_text
        .as_deref()
        .and_then(|t| serde_json::from_str(t).ok())
        .unwrap_or_default();
    Ok(PendingDestructive {
        tool_name: r.try_get("tool_name")?,
        tool_args,
        proposal_message_id: r.try_get("proposal_message_id")?,
        proposed_at: r.try_get("proposed_at")?,
    })
}

/// Serialize a value as a compact JSON string for the `CAST(... AS json)` insert
/// path (Python `_to_json`, `separators=(",", ":")`).
fn to_json(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "null".to_string())
}
