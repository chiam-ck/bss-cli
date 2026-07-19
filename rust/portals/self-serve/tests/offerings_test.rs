//! `offerings::flatten_offerings` byte-parity against the Python oracle
//! (`bss_self_serve.offerings.flatten_offerings`): sort order, GB/unlimited
//! formatting, voice_minutes fallback, roaming suppression.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde_json::Value;

use bss_self_serve::offerings::flatten_offerings;

#[test]
fn flatten_matches_oracle() {
    let golden: Value =
        serde_json::from_str(include_str!("offerings_golden.json")).expect("fixture parses");
    let input = golden["in"].as_array().unwrap().clone();
    let got = flatten_offerings(&input);

    // Compare as JSON values so key/None handling matches the oracle exactly.
    let got_json = serde_json::to_value(&got).unwrap();
    assert_eq!(
        got_json, golden["out"],
        "flatten_offerings diverged from oracle"
    );
}
