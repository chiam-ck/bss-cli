//! Prompt-builder + chrome-filter parity vs the Python oracle. Pure (no DB) —
//! runs in CI.
//!
//! `golden/prompts.json` was captured from `bss_cockpit.prompts.build_cockpit_prompt`
//! over five cases (empty md, md+focus, pending-destructive, extra-context, all).
//! Byte-parity here also validates the verbatim `COCKPIT_INVARIANTS` (~15.8 KB) —
//! a behavioural contract with the model (R2). `golden/chrome.json` pins the
//! `is_cockpit_chrome` inventory.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use bss_cockpit::conversation::PendingDestructive;
use bss_cockpit::{build_cockpit_prompt, is_cockpit_chrome, ASSISTANT_CHROME_PREFIXES};
use indexmap::IndexMap;
use serde_json::{json, Value};

fn pending() -> PendingDestructive {
    // Matches the Python golden capture: insertion order subscription_id, reason.
    let mut args: IndexMap<String, Value> = IndexMap::new();
    args.insert("subscription_id".into(), json!("SUB-0005"));
    args.insert("reason".into(), json!("fraud"));
    PendingDestructive {
        tool_name: "subscription.terminate".into(),
        tool_args: args,
        proposal_message_id: 7,
        proposed_at: chrono::Utc::now(),
    }
}

#[test]
fn build_cockpit_prompt_matches_python_oracle() {
    let golden: Value =
        serde_json::from_str(include_str!("golden/prompts.json")).expect("parse prompts golden");
    let pd = pending();

    let mut extra_all: BTreeMap<String, String> = BTreeMap::new();
    extra_all.insert("z".into(), "last".into());
    extra_all.insert("a".into(), "first".into());

    let mut extra_ctx: BTreeMap<String, String> = BTreeMap::new();
    extra_ctx.insert("model".into(), "deepseek/deepseek-v4-pro".into());
    extra_ctx.insert("session".into(), "SES-1".into());

    for case in golden.as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let expected = case["output"].as_str().unwrap();
        let actual = match name {
            "empty" => build_cockpit_prompt("", None, None, None),
            "md_focus" => build_cockpit_prompt(
                "# Persona\n\nHouse rules here.",
                Some("CUST-001"),
                None,
                None,
            ),
            "pending" => build_cockpit_prompt("# P", None, Some(&pd), None),
            "extra" => build_cockpit_prompt("# P", None, None, Some(&extra_ctx)),
            "all" => {
                build_cockpit_prompt("# P\n\nrules", Some("CUST-9"), Some(&pd), Some(&extra_all))
            }
            other => panic!("unknown golden case {other}"),
        };
        assert_eq!(actual, expected, "prompt case {name:?} differs");
    }
}

#[test]
fn is_cockpit_chrome_matches_python_oracle() {
    let golden: Value =
        serde_json::from_str(include_str!("golden/chrome.json")).expect("parse chrome golden");

    for case in golden["is_chrome"].as_array().unwrap() {
        let input = case["input"].as_str().unwrap();
        let expected = case["is_chrome"].as_bool().unwrap();
        assert_eq!(
            is_cockpit_chrome(input),
            expected,
            "is_cockpit_chrome({input:?}) differs"
        );
    }

    // Inventory-lock: the prefix set must match the oracle exactly (an omission
    // re-opens the mimicry/state-confusion/citation-thrash failure modes).
    let expected_prefixes: Vec<&str> = golden["prefixes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(
        ASSISTANT_CHROME_PREFIXES.to_vec(),
        expected_prefixes,
        "chrome prefix inventory drift"
    );
}
