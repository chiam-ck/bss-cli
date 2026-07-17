//! `strip_fake_propose` goldens — byte-for-byte against the Python oracle.
//!
//! This is the v1.5 anti-mimicry runtime backstop: it removes propose-banner and
//! narrated-call shapes so the operator never reads a `/confirm` prompt for an
//! action that will never fire (no real tool_call was made). The regexes are
//! subtle and the false-positive/false-negative trade-off is deliberate, so every
//! case is pinned against the oracle rather than against my reading of them.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_cockpit::strip_fake_propose;
use serde_json::Value;

fn golden() -> Value {
    let raw = std::fs::read_to_string(format!(
        "{}/tests/golden/strip_fake_propose.json",
        env!("CARGO_MANIFEST_DIR")
    ))
    .expect("golden fixture is present");
    serde_json::from_str(&raw).expect("golden fixture parses")
}

#[test]
fn strip_fake_propose_matches_the_oracle() {
    let want = golden();
    let cases: Vec<(&str, &str)> = vec![
        // Shape A — the banner, with and without a step marker.
        (
            "banner",
            "⚠ PROPOSE: subscription.terminate subscription_id='SUB-0001'\nType /confirm to proceed.",
        ),
        ("banner_step", "PROPOSE [step 2]: order.cancel order_id='ORD-007'"),
        // Shape B — a narrated call, with the "I propose ..." lead and the
        // "/confirm" boilerplate around it. All three must go together: a
        // half-stripped propose still misleads the operator into typing /confirm.
        (
            "narrated_call",
            "I propose to terminate the line. `subscription.terminate(subscription_id='SUB-1')` Please type /confirm to proceed.",
        ),
        ("bare_call", "Calling subscription.terminate(subscription_id='SUB-1') now."),
        // Legitimate prose survives untouched, and is NOT flagged as modified.
        ("clean_prose", "Your balance is 2GB. Nothing to strip here."),
        // The carve-out that matters: prose LEGITIMATELY mentioning /confirm (e.g.
        // a knowledge-grounded answer) must survive, and must not be flagged —
        // the caller uses `modified` to decide whether to show a stall warning.
        (
            "legit_confirm",
            "The handbook says destructive actions need /confirm to authorise.",
        ),
        ("empty", ""),
        // Backtick-wrapped calls take their backticks with them (a leftover ``
        // renders as an ugly empty inline-code fragment).
        ("backticked", "``subscription.terminate(subscription_id='SUB-1')``"),
        // The narration lead is stripped mid-paragraph too — that's the lookbehind.
        (
            "narration_midsent",
            "All set. I'll call customer.get(customer_id='CUST-1') to check.",
        ),
        // Paragraph breaks survive the whitespace collapse.
        ("multiline", "First para.\n\n⚠ PROPOSE: a.b(x=1)\n\nSecond para."),
    ];

    for (name, input) in cases {
        let (cleaned, modified) = strip_fake_propose(input);
        assert_eq!(
            cleaned,
            want[name]["cleaned"].as_str().unwrap(),
            "case {name}: cleaned text diverged\n  input : {input:?}\n  rust  : {cleaned:?}"
        );
        assert_eq!(
            modified,
            want[name]["modified"].as_bool().unwrap(),
            "case {name}: `modified` flag diverged (it gates the stall warning)"
        );
    }
}
