//! Assertion evaluator for `assert:` steps and LLM `expect_final_state`. Port of
//! `cli/bss_cli/scenarios/assertions.py`.
//!
//! Keys are dot-paths: `foo.bar` walks object keys; on a list, a numeric segment
//! indexes directly, a non-numeric segment matches `{allowanceType|type|id|name|
//! channel}` on each item. Values are scalars (equality) or operator objects
//! (`eq/ne/gt/gte/lt/lte/in/not_in/starts_with/contains/not_null`). The special key
//! `any_match` requires its sub-map to hold on at least one element of a list value.

use std::time::{Duration, Instant};

use serde_json::Value;

use super::schema::Poll;

/// A single path-level mismatch — pretty-printable.
#[derive(Debug, Clone)]
pub struct AssertionFailure {
    pub path: String,
    pub expected: Value,
    pub actual: Value,
    pub reason: String,
}

impl AssertionFailure {
    pub fn format(&self) -> String {
        format!(
            "  ✗ {}: expected {} got {} ({})",
            self.path,
            py_repr(&self.expected),
            py_repr(&self.actual),
            self.reason
        )
    }
}

#[derive(Debug, Clone, Default)]
pub struct AssertionResult {
    pub ok: bool,
    pub failures: Vec<AssertionFailure>,
    /// The value the assertion last evaluated against — carried for the LLM-slice
    /// `expect_final_state` reporting; unused by the deterministic runner today.
    #[allow(dead_code)]
    pub last_value: Value,
}

impl AssertionResult {
    pub fn format(&self) -> String {
        if self.ok {
            "✓".to_string()
        } else {
            self.failures
                .iter()
                .map(AssertionFailure::format)
                .collect::<Vec<_>>()
                .join("\n")
        }
    }
}

const LIST_KEY_CANDIDATES: &[&str] = &["allowanceType", "type", "id", "name", "channel"];

/// Walk a dot-path, returning `None` on a miss (the Python `_SENTINEL`). Distinct from
/// resolving to a JSON `null`, which is a legitimate value.
fn resolve_path<'a>(obj: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = obj;
    for seg in path.split('.') {
        match cur {
            Value::Null => return None,
            Value::Array(items) => {
                if let Ok(idx) = seg.parse::<usize>() {
                    cur = items.get(idx)?;
                    continue;
                }
                let matched = items.iter().find(|item| {
                    item.is_object()
                        && LIST_KEY_CANDIDATES
                            .iter()
                            .any(|k| item.get(k).and_then(Value::as_str) == Some(seg))
                })?;
                cur = matched;
            }
            Value::Object(map) => {
                cur = map.get(seg)?;
            }
            _ => return None,
        }
    }
    Some(cur)
}

/// `(ok, reason)` — an operator object, a list, or scalar equality.
fn match_value(expected: &Value, actual: &Value) -> (bool, String) {
    if let Value::Object(ops) = expected {
        return match_operator(ops, actual);
    }
    if expected.is_array() && actual.is_array() {
        return if expected == actual {
            (true, String::new())
        } else {
            (false, "list inequality".to_string())
        };
    }
    if expected == actual {
        (true, String::new())
    } else {
        (false, "inequality".to_string())
    }
}

fn match_operator(ops: &serde_json::Map<String, Value>, actual: &Value) -> (bool, String) {
    for (op, expected) in ops {
        match apply_op(op, expected, actual) {
            Ok((true, _)) => {}
            Ok((false, why)) => return (false, why),
            Err(e) => return (false, e),
        }
    }
    (true, String::new())
}

/// Numeric comparison via `f64` when both sides are numbers (JSON numbers only compare
/// meaningfully as f64; scenario thresholds are small integers/decimals).
fn num_cmp(actual: &Value, expected: &Value) -> Option<std::cmp::Ordering> {
    actual
        .as_f64()
        .zip(expected.as_f64())
        .and_then(|(a, e)| a.partial_cmp(&e))
}

fn apply_op(op: &str, expected: &Value, actual: &Value) -> Result<(bool, String), String> {
    use std::cmp::Ordering::{Greater, Less};
    let ok = match op {
        "eq" => actual == expected,
        "ne" => actual != expected,
        "gt" => matches!(num_cmp(actual, expected), Some(Greater)),
        "gte" => matches!(
            num_cmp(actual, expected),
            Some(Greater | std::cmp::Ordering::Equal)
        ),
        "lt" => matches!(num_cmp(actual, expected), Some(Less)),
        "lte" => matches!(
            num_cmp(actual, expected),
            Some(Less | std::cmp::Ordering::Equal)
        ),
        "in" => expected.as_array().is_some_and(|arr| arr.contains(actual)),
        "not_in" => expected.as_array().is_some_and(|arr| !arr.contains(actual)),
        "starts_with" => match (actual.as_str(), expected.as_str()) {
            (Some(a), Some(e)) => a.starts_with(e),
            _ => false,
        },
        "contains" => match actual {
            Value::String(s) => expected.as_str().is_some_and(|e| s.contains(e)),
            Value::Array(a) => a.contains(expected),
            Value::Object(o) => expected.as_str().is_some_and(|e| o.contains_key(e)),
            _ => return Ok((false, "contains on non-container".to_string())),
        },
        "not_null" => {
            let want_not_null = truthy(expected);
            (!actual.is_null()) == want_not_null
        }
        other => return Err(format!("unknown operator: {other:?}")),
    };
    Ok((ok, op.to_string()))
}

/// Evaluate every key/value in `expect` against `actual`.
pub fn evaluate_expect(expect: &serde_json::Map<String, Value>, actual: &Value) -> AssertionResult {
    let mut failures = Vec::new();
    for (path, expected) in expect {
        if path == "any_match" {
            if let Some(failure) = eval_any_match(expected, actual) {
                failures.push(failure);
            }
            continue;
        }
        match resolve_path(actual, path) {
            None => failures.push(AssertionFailure {
                path: path.clone(),
                expected: expected.clone(),
                actual: Value::Null,
                reason: "path did not resolve".to_string(),
            }),
            Some(resolved) => {
                let (ok, why) = match_value(expected, resolved);
                if !ok {
                    failures.push(AssertionFailure {
                        path: path.clone(),
                        expected: expected.clone(),
                        actual: resolved.clone(),
                        reason: why,
                    });
                }
            }
        }
    }
    AssertionResult {
        ok: failures.is_empty(),
        failures,
        last_value: actual.clone(),
    }
}

fn eval_any_match(expected: &Value, actual: &Value) -> Option<AssertionFailure> {
    let Value::Array(items) = actual else {
        return Some(AssertionFailure {
            path: "any_match".to_string(),
            expected: expected.clone(),
            actual: actual.clone(),
            reason: "target is not a list".to_string(),
        });
    };
    let sub = match expected {
        Value::Object(m) => m,
        _ => {
            return Some(AssertionFailure {
                path: "any_match".to_string(),
                expected: expected.clone(),
                actual: actual.clone(),
                reason: "any_match value must be a mapping".to_string(),
            })
        }
    };
    for item in items {
        if evaluate_expect(sub, item).ok {
            return None;
        }
    }
    Some(AssertionFailure {
        path: "any_match".to_string(),
        expected: expected.clone(),
        actual: Value::String(format!("<{} items, none matched>", items.len())),
        reason: "no element satisfied all sub-expectations".to_string(),
    })
}

/// Run `fetch()` + `evaluate_expect` until green or the poll deadline. With no poll,
/// a single evaluation.
pub async fn poll_until<F, Fut>(
    mut fetch: F,
    expect: &serde_json::Map<String, Value>,
    poll: Option<&Poll>,
) -> Result<AssertionResult, String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<Value, String>>,
{
    let Some(poll) = poll else {
        let value = fetch().await?;
        return Ok(evaluate_expect(expect, &value));
    };
    let deadline = Instant::now() + Duration::from_secs_f64(poll.timeout_seconds);
    let interval = Duration::from_secs_f64((poll.interval_ms as f64 / 1000.0).max(0.01));
    loop {
        let value = fetch().await?;
        let last = evaluate_expect(expect, &value);
        if last.ok || Instant::now() >= deadline {
            return Ok(last);
        }
        tokio::time::sleep(interval).await;
    }
}

/// Python truthiness for the `not_null` operator's flag.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Null => false,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Render a value the way Python's `repr()` would in the failure lines: strings
/// single-quoted, everything else via its compact JSON form.
fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) => format!("'{s}'"),
        other => other.to_string(),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    fn expect(m: Value) -> serde_json::Map<String, Value> {
        m.as_object().unwrap().clone()
    }

    #[test]
    fn dot_path_and_scalar_equality() {
        let actual = json!({"price": {"taxIncludedAmount": {"value": 20.0}}});
        let r = evaluate_expect(
            &expect(json!({"price.taxIncludedAmount.value": 20.0})),
            &actual,
        );
        assert!(r.ok, "{}", r.format());
    }

    #[test]
    fn list_candidate_key_match() {
        let actual = json!({"balances": [{"allowanceType": "data", "remaining": 0}]});
        let r = evaluate_expect(&expect(json!({"balances.data.remaining": 0})), &actual);
        assert!(r.ok, "{}", r.format());
    }

    #[test]
    fn operators_starts_with_and_gte() {
        let actual = json!({"id": "PRICE_PLAN_M_CNY_1", "count": 3});
        let r = evaluate_expect(
            &expect(json!({"id": {"starts_with": "PRICE_PLAN_M_CNY_"}, "count": {"gte": 1}})),
            &actual,
        );
        assert!(r.ok, "{}", r.format());
    }

    #[test]
    fn any_match_over_list() {
        let actual = json!([{"channel": "email"}, {"channel": "sms"}]);
        let r = evaluate_expect(&expect(json!({"any_match": {"channel": "sms"}})), &actual);
        assert!(r.ok, "{}", r.format());
        let bad = evaluate_expect(&expect(json!({"any_match": {"channel": "fax"}})), &actual);
        assert!(!bad.ok);
    }

    #[test]
    fn missing_path_fails() {
        let r = evaluate_expect(&expect(json!({"nope": 1})), &json!({"a": 1}));
        assert!(!r.ok);
        assert!(r.format().contains("did not resolve"));
    }
}
