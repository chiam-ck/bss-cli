//! Scenario YAML schema — port of `cli/bss_cli/scenarios/schema.py`.
//!
//! Intentionally narrow (v0.1): variables, setup, steps (action/ask/assert/http/file),
//! teardown. No conditionals, loops, or composition. Every struct is
//! `deny_unknown_fields` — a typo in a scenario file fails loud rather than silently
//! parsing, mirroring Pydantic's `extra="forbid"`. The post-parse [`Scenario::validate`]
//! reproduces the two Pydantic `model_validator`s (HTTP method / form-vs-json, and the
//! "assert steps cannot capture" rule).

// Many fields are consumed only once the runner + executors land in the following
// slices (same slice-by-slice posture as `runtime::Clients`); the parse/validate
// surface this slice ships reads only a subset. Lifted per-struct as they're wired.
#![allow(dead_code)]

use indexmap::IndexMap;
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Value};

/// A JSON object (scenario args / expect / variables are JSON-shaped).
type JsonMap = Map<String, Value>;

fn default_poll_interval_ms() -> i64 {
    200
}
fn default_poll_timeout_seconds() -> f64 {
    5.0
}
fn default_ask_timeout_seconds() -> f64 {
    60.0
}
fn default_http_timeout_seconds() -> f64 {
    30.0
}
fn default_base_url() -> String {
    "http://portal-self-serve:8000".to_string()
}
fn default_encoding() -> String {
    "utf-8".to_string()
}
fn default_regex_group() -> i64 {
    1
}

// ── setup / teardown ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Setup {
    #[serde(default)]
    pub reset_operational_data: bool,
    #[serde(default)]
    pub reset_sequences: bool,
    #[serde(default)]
    pub freeze_clock_at: Option<String>,
    #[serde(default)]
    pub variables: JsonMap,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Teardown {
    #[serde(default)]
    pub unfreeze_clock: bool,
}

/// Polling config for assertions against eventually-consistent state.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Poll {
    #[serde(default = "default_poll_interval_ms")]
    pub interval_ms: i64,
    #[serde(default = "default_poll_timeout_seconds")]
    pub timeout_seconds: f64,
}

// ── step types ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActionStep {
    pub name: String,
    #[serde(default)]
    pub capture: Map<String, Value>,
    pub action: String,
    #[serde(default)]
    pub args: JsonMap,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AskStep {
    pub name: String,
    #[serde(default)]
    pub capture: Map<String, Value>,
    pub ask: String,
    #[serde(default = "default_ask_timeout_seconds")]
    pub timeout_seconds: f64,
    #[serde(default)]
    pub expect_tools_called_include: Vec<String>,
    #[serde(default)]
    pub expect_tools_not_called: Vec<String>,
    #[serde(default)]
    pub expect_final_state: JsonMap,
    #[serde(default)]
    pub expect_event_sequence: Vec<String>,
    #[serde(default)]
    pub allow_clarification: bool,
}

/// The `assert:` block — call a read tool and check the shape.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssertCall {
    pub tool: String,
    #[serde(default)]
    pub args: JsonMap,
    /// Dot-path key → expected scalar or operator dict.
    #[serde(default)]
    pub expect: JsonMap,
    #[serde(default)]
    pub poll: Option<Poll>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssertStep {
    pub name: String,
    #[serde(default)]
    pub capture: Map<String, Value>,
    #[serde(rename = "assert")]
    pub assert_call: AssertCall,
}

// ── HTTP step (v0.4) ─────────────────────────────────────────────────────────

/// An expected HTTP status: a single code or a list of acceptable codes.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum StatusSpec {
    One(i64),
    Many(Vec<i64>),
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct HttpExpect {
    #[serde(default)]
    pub status: Option<StatusSpec>,
    #[serde(default)]
    pub body_contains: Vec<String>,
    #[serde(default)]
    pub body_not_contains: Vec<String>,
    #[serde(default)]
    pub headers_match: Map<String, Value>,
    #[serde(default)]
    pub body_json_equals: JsonMap,
}

/// Capture a regex group out of a header or body field.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HttpRegexCapture {
    pub source: String,
    pub pattern: String,
    #[serde(default = "default_regex_group")]
    pub group: i64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HttpStep {
    pub name: String,
    #[serde(default)]
    pub capture: Map<String, Value>,
    pub http: String,
    #[serde(default = "default_base_url")]
    pub base_url: String,
    #[serde(default)]
    pub headers: Map<String, Value>,
    #[serde(default)]
    pub cookies: Map<String, Value>,
    #[serde(default)]
    pub form: JsonMap,
    #[serde(default, rename = "json")]
    pub json_body: Option<JsonMap>,
    #[serde(default)]
    pub expect: HttpExpect,
    #[serde(default)]
    pub poll: Option<Poll>,
    #[serde(default)]
    pub follow_redirects: bool,
    #[serde(default)]
    pub drain_stream: bool,
    #[serde(default = "default_http_timeout_seconds")]
    pub timeout_seconds: f64,
    #[serde(default)]
    pub capture_regex: IndexMap<String, HttpRegexCapture>,
}

/// Read a local file and capture substrings from it (v0.8 auth-flow scenarios).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileReadStep {
    pub name: String,
    #[serde(default)]
    pub capture: Map<String, Value>,
    pub file: String,
    #[serde(default)]
    pub capture_regex: IndexMap<String, HttpRegexCapture>,
    #[serde(default)]
    pub poll: Option<Poll>,
    #[serde(default = "default_encoding")]
    pub encoding: String,
}

/// One scenario step — an untagged union discriminated by which verb key is present
/// (`action` / `ask` / `assert` / `http` / `file`), matching Pydantic's try-each-in-
/// order union. Dispatch-by-key gives a precise error instead of serde's opaque
/// "did not match any variant".
// The variants differ in size (HttpStep is the largest); boxing would fight the
// borrow ergonomics in the runner for a struct parsed once per scenario file.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone)]
pub enum Step {
    Action(ActionStep),
    Ask(AskStep),
    Assert(AssertStep),
    Http(HttpStep),
    File(FileReadStep),
}

impl Step {
    pub fn name(&self) -> &str {
        match self {
            Step::Action(s) => &s.name,
            Step::Ask(s) => &s.name,
            Step::Assert(s) => &s.name,
            Step::Http(s) => &s.name,
            Step::File(s) => &s.name,
        }
    }

    /// The step kind label used in reporting (`action | ask | assert | http | file`).
    pub fn kind(&self) -> &'static str {
        match self {
            Step::Action(_) => "action",
            Step::Ask(_) => "ask",
            Step::Assert(_) => "assert",
            Step::Http(_) => "http",
            Step::File(_) => "file",
        }
    }
}

impl<'de> Deserialize<'de> for Step {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        use serde::de::Error;
        let value = serde_yaml::Value::deserialize(deserializer)?;
        let map = value
            .as_mapping()
            .ok_or_else(|| D::Error::custom("scenario step must be a mapping"))?;
        let has = |k: &str| map.contains_key(serde_yaml::Value::String(k.to_string()));
        // A single closure can't be generic over the target type, so dispatch each
        // branch directly (from_value is monomorphized per variant).
        if has("action") {
            serde_yaml::from_value(value)
                .map(Step::Action)
                .map_err(D::Error::custom)
        } else if has("ask") {
            serde_yaml::from_value(value)
                .map(Step::Ask)
                .map_err(D::Error::custom)
        } else if has("assert") {
            serde_yaml::from_value(value)
                .map(Step::Assert)
                .map_err(D::Error::custom)
        } else if has("http") {
            serde_yaml::from_value(value)
                .map(Step::Http)
                .map_err(D::Error::custom)
        } else if has("file") {
            serde_yaml::from_value(value)
                .map(Step::File)
                .map_err(D::Error::custom)
        } else {
            Err(D::Error::custom(
                "scenario step must have one of: action, ask, assert, http, file",
            ))
        }
    }
}

// ── top-level scenario ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub setup: Setup,
    #[serde(default)]
    pub variables: JsonMap,
    #[serde(default)]
    pub steps: Vec<Step>,
    #[serde(default)]
    pub teardown: Teardown,
}

impl Scenario {
    /// The two Pydantic `model_validator`s that serde can't express: `assert:` steps
    /// cannot `capture`, and each `http:` step must be GET/POST with a URL and not
    /// set both `form` and `json`.
    pub fn validate(&self) -> Result<(), String> {
        for step in &self.steps {
            match step {
                Step::Assert(s) if !s.capture.is_empty() => {
                    return Err("assert: steps cannot use capture".to_string());
                }
                Step::Http(s) => {
                    if !s.form.is_empty() && s.json_body.is_some() {
                        return Err("http: step cannot set both `form` and `json`".to_string());
                    }
                    let (method, rest) =
                        s.http.trim().split_once(' ').unwrap_or((s.http.trim(), ""));
                    let m = method.to_ascii_uppercase();
                    if m != "GET" && m != "POST" {
                        return Err(format!(
                            "http: step method must be GET or POST, got {method:?}"
                        ));
                    }
                    if rest.trim().is_empty() {
                        return Err("http: step must include a URL after the method".to_string());
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }
}

/// LLM-mode hint used by the runner + CLI flags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LlmMode {
    Auto,
    Disabled,
    Forced,
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn parses_action_and_assert_steps() {
        let yaml = r#"
name: demo
tags: [smoke]
steps:
  - name: create customer
    action: customer.create
    args: {name: Ck, email: ck@x.io}
    capture: {customer_id: "$.id"}
  - name: verify
    assert:
      tool: customer.get
      args: {customer_id: "{{ customer_id }}"}
      expect: {id: "{{ customer_id }}"}
"#;
        let s: Scenario = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(s.name, "demo");
        assert_eq!(s.steps.len(), 2);
        assert_eq!(s.steps[0].kind(), "action");
        assert_eq!(s.steps[1].kind(), "assert");
        s.validate().unwrap();
    }

    #[test]
    fn unknown_field_is_rejected() {
        let yaml = "name: x\nsteps:\n  - name: s\n    action: a\n    bogus: 1\n";
        assert!(serde_yaml::from_str::<Scenario>(yaml).is_err());
    }

    #[test]
    fn assert_with_capture_fails_validate() {
        let yaml = r#"
name: x
steps:
  - name: bad
    capture: {v: "$.id"}
    assert:
      tool: customer.get
      expect: {}
"#;
        let s: Scenario = serde_yaml::from_str(yaml).unwrap();
        assert!(s.validate().is_err());
    }

    #[test]
    fn http_method_and_form_json_exclusivity_validated() {
        let bad_method = "name: x\nsteps:\n  - name: s\n    http: PUT /x\n";
        let s: Scenario = serde_yaml::from_str(bad_method).unwrap();
        assert!(s.validate().is_err());

        let both =
            "name: x\nsteps:\n  - name: s\n    http: POST /x\n    form: {a: 1}\n    json: {b: 2}\n";
        let s: Scenario = serde_yaml::from_str(both).unwrap();
        assert!(s.validate().is_err());
    }
}
