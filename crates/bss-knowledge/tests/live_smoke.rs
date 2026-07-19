//! Live golden diff — `search_fts` + `get_chunk` against the same live Postgres
//! the Python oracle reads. `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-knowledge
//! cargo test -p bss-knowledge --test live_smoke -- --ignored --nocapture
//! ```
//!
//! `golden/search.json` was captured from `bss_knowledge.search` over the live
//! `knowledge.doc_chunk` table (6 queries incl. an empty-result miss and a
//! kinds-filtered scope + get_chunk hit/miss). Since the FTS runs in Postgres,
//! byte-parity of ranking + snippets is structural; this pins the Rust-side
//! re-rank + wire shape. Read-only; nothing mutated.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_knowledge::{get_chunk, search_fts};
use serde_json::Value;

fn normalize_db_url(raw: &str) -> String {
    raw.replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://")
}

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn search_and_get_chunk_match_python_oracle() {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&url).await.expect("connect live Postgres");

    let golden: Value =
        serde_json::from_str(include_str!("golden/search.json")).expect("parse search golden");

    // ── search_fts ────────────────────────────────────────────────────────
    for entry in golden["searches"].as_array().unwrap() {
        let p = &entry["params"];
        let query = p["query"].as_str().unwrap();
        let k = p["k"].as_i64().unwrap();
        let kinds: Option<Vec<String>> = p["kinds"]
            .as_array()
            .map(|a| a.iter().map(|v| v.as_str().unwrap().to_string()).collect());

        let hits = search_fts(&pool, query, k, kinds.as_deref())
            .await
            .unwrap_or_else(|e| panic!("search {query:?}: {e}"));
        let expected = entry["hits"].as_array().unwrap();

        assert_eq!(
            hits.len(),
            expected.len(),
            "search {query:?}: hit count differs (rust {} vs python {})",
            hits.len(),
            expected.len()
        );
        for (i, (h, e)) in hits.iter().zip(expected.iter()).enumerate() {
            // The exported wire contract (`to_value`) deliberately omits `rank`
            // — both Python `to_dict` and Rust `to_value` do. Compare it byte
            // for byte (anchor / source_path / heading_path / kind / snippet /
            // content, all produced by Postgres).
            let mut expected_shape = e.clone();
            expected_shape.as_object_mut().unwrap().remove("rank");
            assert_eq!(
                h.to_value(),
                expected_shape,
                "search {query:?}: hit {i} wire shape differs"
            );
            // `rank` is an internal re-rank score that only drives ordering
            // (which the position-wise match above already pins). The
            // `f32→f64` widen-then-multiply can round 1 ULP off the Python
            // path, so compare it within tolerance rather than bit-for-bit.
            let expected_rank = e["rank"].as_f64().unwrap();
            assert!(
                (h.rank - expected_rank).abs() < 1e-12,
                "search {query:?}: hit {i} rank drift rust={} python={}",
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
        let actual = get_chunk(&pool, anchor, source_path)
            .await
            .unwrap_or_else(|e| panic!("get_chunk {anchor}: {e}"));
        let actual_val = actual.map_or(Value::Null, |v| v);
        assert_eq!(
            actual_val, entry["result"],
            "get_chunk({anchor}, {source_path}) differs"
        );
    }
}
