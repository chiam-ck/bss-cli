//! Knowledge tools — FTS over the indexed doc corpus (v0.20). Port of
//! `orchestrator/bss_orchestrator/tools/knowledge.py`, backed by the already-ported
//! `bss_knowledge` crate's `search_fts` / `get_chunk` (Postgres FTS).
//!
//! operator_cockpit-only (doctrine guard 15). The Python side gates registration on
//! `BSS_KNOWLEDGE_ENABLED` via `_maybe_register`; here the caller makes that call —
//! `register_knowledge_tools` is invoked only when knowledge is enabled and a pool
//! is available (P6/P7 wiring). Both tools take a live `sqlx` pool (the Python
//! `_get_engine` lazy-engine equivalent is the caller's pool).

use std::sync::Arc;

use bss_knowledge::{get_chunk, search_fts};
use futures_util::future::FutureExt;
use serde_json::{json, Value};
use sqlx::PgPool;

use super::{req_str, RegisteredTool, ToolError, ToolRegistry};

const DESC_SEARCH: &str = include_str!("desc/knowledge_search.txt");
const DESC_GET: &str = include_str!("desc/knowledge_get.txt");

fn db_err(e: sqlx::Error) -> ToolError {
    ToolError::Other {
        kind: "DB_ERROR".to_string(),
        detail: e.to_string(),
    }
}

/// The `knowledge.get` NOT_FOUND message, byte-for-byte with Python's f-string
/// (`anchor={anchor!r}` → single-quoted). Extracted so a unit test pins it (R2).
fn not_found_message(anchor: &str, source_path: &str) -> String {
    format!(
        "No indexed chunk at anchor='{anchor}' in '{source_path}'. The section may \
         have been re-anchored or removed since the last reindex. Try knowledge.search \
         with related keywords."
    )
}

/// Register `knowledge.search` + `knowledge.get`, each capturing a clone of `pool`.
/// Call only when `BSS_KNOWLEDGE_ENABLED` is set (mirrors Python `_maybe_register`).
pub fn register_knowledge_tools(registry: &mut ToolRegistry, pool: PgPool) {
    // knowledge.search — FTS; wraps hits + the echoed query (key order via D9).
    let p = pool.clone();
    registry.register(RegisteredTool {
        name: "knowledge.search".to_string(),
        description: DESC_SEARCH.to_string(),
        func: Arc::new(move |args, _ctx| {
            let p = p.clone();
            async move {
                let query = req_str(&args, "query")?;
                let k = args.get("k").and_then(Value::as_i64).unwrap_or(5);
                let kinds: Option<Vec<String>> =
                    args.get("kinds").and_then(Value::as_array).map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    });
                let hits = search_fts(&p, &query, k, kinds.as_deref())
                    .await
                    .map_err(db_err)?;
                let hit_values: Vec<Value> = hits.iter().map(|h| h.to_value()).collect();
                Ok(json!({ "hits": hit_values, "query": query }))
            }
            .boxed()
        }),
    });

    // knowledge.get — one chunk by anchor + source_path, or the NOT_FOUND sentinel.
    let p = pool;
    registry.register(RegisteredTool {
        name: "knowledge.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let p = p.clone();
            async move {
                let anchor = req_str(&args, "anchor")?;
                let source_path = req_str(&args, "source_path")?;
                match get_chunk(&p, &anchor, &source_path).await.map_err(db_err)? {
                    Some(v) => Ok(v),
                    None => Ok(json!({
                        "error": "NOT_FOUND",
                        "message": not_found_message(&anchor, &source_path),
                    })),
                }
            }
            .boxed()
        }),
    });
}

#[cfg(test)]
mod tests {
    use super::not_found_message;

    #[test]
    fn not_found_message_matches_python_fstring() {
        // Byte-for-byte with Python's
        // f"No indexed chunk at anchor={anchor!r} in {source_path!r}. The section
        //   may have been re-anchored or removed since the last reindex. Try
        //   knowledge.search with related keywords."
        // Single unbroken literal (no `\`-continuation) so it is an independent
        // oracle for the helper's own line continuations.
        let expected = "No indexed chunk at anchor='84-rotate-api-tokens' in 'docs/HANDBOOK.md'. The section may have been re-anchored or removed since the last reindex. Try knowledge.search with related keywords.";
        assert_eq!(
            not_found_message("84-rotate-api-tokens", "docs/HANDBOOK.md"),
            expected
        );
    }
}
