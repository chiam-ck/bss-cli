//! Live golden diff — `search_fts` + `get_chunk` over the live `knowledge.doc_chunk`
//! table. `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../.env; set +a        # from crates/bss-knowledge
//! cargo test -p bss-knowledge --test live_smoke -- --ignored --nocapture
//! ```
//!
//! `golden/search.json` pins 6 queries (incl. an empty-result miss and a
//! kinds-filtered scope) plus 2 `get_chunk` probes (hit + miss). It was captured
//! from `bss_knowledge.search` pre-2.0; since the Python oracle was retired at
//! v2.0.0 it is a **Rust-captured regression pin**, not an oracle diff. Because
//! the FTS runs in Postgres, ranks and snippets move with the corpus — so when an
//! indexed doc legitimately changes, recapture with `regen_search_golden` rather
//! than hand-editing.
//!
//! `indexed_at` is deliberately **not** pinned: it is a reindex timestamp, not
//! wire behaviour, and pinning it broke this test on every `make
//! knowledge-reindex`. It is asserted present-and-non-empty instead.
//!
//! Read-only; nothing mutated.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_knowledge::{get_chunk, search_fts};
use serde_json::{json, Value};

/// Placeholder written into the golden in place of the live `indexed_at`.
const INDEXED_AT_SENTINEL: &str = "<not-pinned>";

fn normalize_db_url(raw: &str) -> String {
    raw.replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://")
}

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn golden() -> Value {
    serde_json::from_str(include_str!("golden/search.json")).expect("parse search golden")
}

async fn pool() -> sqlx::PgPool {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    bss_db::connect(&url).await.expect("connect live Postgres")
}

/// Parse one `searches[]` entry's params into `search_fts` arguments.
fn search_params(entry: &Value) -> (String, i64, Option<Vec<String>>) {
    let p = &entry["params"];
    let kinds = p["kinds"]
        .as_array()
        .map(|a| a.iter().map(|v| v.as_str().unwrap().to_string()).collect());
    (
        p["query"].as_str().unwrap().to_string(),
        p["k"].as_i64().unwrap(),
        kinds,
    )
}

/// Replace a live `indexed_at` with the sentinel so the value never gets pinned.
/// Asserts the field is present and non-empty first — that much *is* contract.
fn scrub_indexed_at(v: &mut Value, ctx: &str) {
    if let Some(obj) = v.as_object_mut() {
        let ts = obj
            .get("indexed_at")
            .unwrap_or_else(|| panic!("{ctx}: missing indexed_at"));
        assert!(
            ts.as_str().is_some_and(|s| !s.is_empty()),
            "{ctx}: indexed_at should be a non-empty string, got {ts}"
        );
        obj["indexed_at"] = json!(INDEXED_AT_SENTINEL);
    }
}

/// Recapture `golden/search.json` from the live index. Run after an indexed doc
/// legitimately changes (a Phase 0 amendment, a HANDBOOK edit) — the FTS ranks
/// and snippets move with the corpus, so a docs PR reds this test by design:
///
/// ```bash
/// BSS_REGEN_GOLDEN=1 cargo test -p bss-knowledge --test live_smoke \
///     regen_search_golden -- --ignored --nocapture
/// ```
///
/// Then re-run the parity test (a fresh cargo invocation re-embeds the file).
/// The query set is preserved from the existing golden; only results are
/// recaptured. Env-guarded so a blanket `--ignored` sweep can't clobber the
/// committed fixture.
#[tokio::test]
#[ignore = "rewrites a committed fixture; needs BSS_REGEN_GOLDEN=1"]
async fn regen_search_golden() {
    if env("BSS_REGEN_GOLDEN").as_deref() != Some("1") {
        eprintln!("refusing to regenerate: set BSS_REGEN_GOLDEN=1 to confirm");
        return;
    }
    let pool = pool().await;
    let old = golden();

    let mut searches = Vec::new();
    for entry in old["searches"].as_array().unwrap() {
        let (query, k, kinds) = search_params(entry);
        let hits = search_fts(&pool, &query, k, kinds.as_deref())
            .await
            .unwrap_or_else(|e| panic!("search {query:?}: {e}"));
        let hits: Vec<Value> = hits
            .iter()
            .map(|h| {
                let mut v = h.to_value();
                // `rank` trails the wire shape — `to_value` omits it (both the
                // Python `to_dict` and the Rust impl do), the golden carries it.
                v.as_object_mut()
                    .unwrap()
                    .insert("rank".into(), json!(h.rank));
                v
            })
            .collect();
        searches.push(json!({ "params": entry["params"], "hits": hits }));
    }

    let mut get_chunks = Vec::new();
    for entry in old["get_chunk"].as_array().unwrap() {
        let p = &entry["params"];
        let anchor = p["anchor"].as_str().unwrap();
        let source_path = p["source_path"].as_str().unwrap();
        let mut result = get_chunk(&pool, anchor, source_path)
            .await
            .unwrap_or_else(|e| panic!("get_chunk {anchor}: {e}"))
            .unwrap_or(Value::Null);
        if !result.is_null() {
            scrub_indexed_at(&mut result, &format!("get_chunk({anchor})"));
        }
        get_chunks.push(json!({ "params": p, "result": result }));
    }

    let out = json!({ "searches": searches, "get_chunk": get_chunks });
    let pretty = serde_json::to_string_pretty(&out).expect("serialize");
    let dest = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/search.json");
    std::fs::write(&dest, pretty).expect("write golden");
    eprintln!("regenerated {}", dest.display());
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn search_and_get_chunk_match_golden() {
    let pool = pool().await;
    let golden = golden();

    // ── search_fts ────────────────────────────────────────────────────────
    for entry in golden["searches"].as_array().unwrap() {
        let (query, k, kinds) = search_params(entry);
        let hits = search_fts(&pool, &query, k, kinds.as_deref())
            .await
            .unwrap_or_else(|e| panic!("search {query:?}: {e}"));
        let expected = entry["hits"].as_array().unwrap();

        assert_eq!(
            hits.len(),
            expected.len(),
            "search {query:?}: hit count differs (live {} vs golden {}). If a doc \
             changed, recapture with regen_search_golden.",
            hits.len(),
            expected.len()
        );
        for (i, (h, e)) in hits.iter().zip(expected.iter()).enumerate() {
            // The exported wire contract (`to_value`) deliberately omits `rank`.
            // Compare it byte for byte (anchor / source_path / heading_path /
            // kind / snippet / content, all produced by Postgres).
            let mut expected_shape = e.clone();
            expected_shape.as_object_mut().unwrap().remove("rank");
            assert_eq!(
                h.to_value(),
                expected_shape,
                "search {query:?}: hit {i} wire shape differs"
            );
            // `rank` is an internal re-rank score that only drives ordering
            // (which the position-wise match above already pins). The
            // `f32→f64` widen-then-multiply can round 1 ULP off, so compare it
            // within tolerance rather than bit-for-bit.
            let expected_rank = e["rank"].as_f64().unwrap();
            assert!(
                (h.rank - expected_rank).abs() < 1e-12,
                "search {query:?}: hit {i} rank drift live={} golden={}",
                h.rank,
                expected_rank
            );
        }
    }

    // ── get_chunk ─────────────────────────────────────────────────────────
    for entry in golden["get_chunk"].as_array().unwrap() {
        let p = &entry["params"];
        let anchor = p["anchor"].as_str().unwrap();
        let source_path = p["source_path"].as_str().unwrap();
        let mut actual = get_chunk(&pool, anchor, source_path)
            .await
            .unwrap_or_else(|e| panic!("get_chunk {anchor}: {e}"))
            .unwrap_or(Value::Null);
        if !actual.is_null() {
            scrub_indexed_at(&mut actual, &format!("get_chunk({anchor})"));
        }
        assert_eq!(
            actual, entry["result"],
            "get_chunk({anchor}, {source_path}) differs"
        );
    }
}
