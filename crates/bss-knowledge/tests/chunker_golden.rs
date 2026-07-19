//! Chunker + paths parity vs the Python oracle. Pure (no DB) — runs in CI.
//!
//! `golden/chunker.json` was captured from `bss_knowledge.chunker.chunk_markdown`
//! over the three distinct split policies (CLAUDE.md `##`, DECISIONS.md dated,
//! HANDBOOK/ARCHITECTURE `##`+`###`) plus a runbook. `golden/paths.json` pins
//! the allowlist + kind mapping + re-rank weights.
//!
//! The docs are read live from the repo root; behaviour-frozen (R5/R7) means
//! they don't drift during the migration. Regenerate the golden if a doc changes.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_knowledge::{chunk_markdown, kind_for, kind_rank_weight, INDEXED_PATHS};
use serde_json::{json, Value};

fn repo_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("resolve repo root")
}

#[test]
fn chunker_matches_python_oracle() {
    let golden: Value =
        serde_json::from_str(include_str!("golden/chunker.json")).expect("parse chunker golden");
    let root = repo_root();

    for (doc_path, expected) in golden.as_object().expect("golden is object") {
        let text = std::fs::read_to_string(root.join(doc_path))
            .unwrap_or_else(|e| panic!("read {doc_path}: {e}"));
        let actual: Vec<Value> = chunk_markdown(doc_path, &text)
            .into_iter()
            .map(|c| {
                json!({
                    "source_path": c.source_path,
                    "anchor": c.anchor,
                    "heading_path": c.heading_path,
                    "content": c.content,
                })
            })
            .collect();
        let expected_arr = expected.as_array().expect("expected array");

        assert_eq!(
            actual.len(),
            expected_arr.len(),
            "{doc_path}: chunk count differs (rust {} vs python {})",
            actual.len(),
            expected_arr.len()
        );
        for (i, (a, e)) in actual.iter().zip(expected_arr.iter()).enumerate() {
            assert_eq!(a, e, "{doc_path}: chunk {i} differs");
        }
    }
}

#[test]
fn paths_and_weights_match_python_oracle() {
    let golden: Value =
        serde_json::from_str(include_str!("golden/paths.json")).expect("parse paths golden");

    let expected_paths: Vec<String> = golden["indexed_paths"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    let actual_paths: Vec<String> = INDEXED_PATHS.iter().map(|s| s.to_string()).collect();
    assert_eq!(actual_paths, expected_paths, "INDEXED_PATHS drift");

    for (path, kind) in golden["kind_for_path"].as_object().unwrap() {
        assert_eq!(
            kind_for(path),
            Some(kind.as_str().unwrap()),
            "kind_for({path}) drift"
        );
    }

    for (kind, weight) in golden["kind_rank_weights"].as_object().unwrap() {
        let w = weight.as_f64().unwrap();
        assert_eq!(kind_rank_weight(kind), w, "kind_rank_weight({kind}) drift");
    }
}
