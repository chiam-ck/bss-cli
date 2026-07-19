//! File-read step (v0.8) — port of `cli/bss_cli/scenarios/file_step.py`.
//!
//! Reads a local file and captures substrings via regex. Used by the auth-flow hero
//! scenarios to fetch the OTP / magic-link `LoggingEmailAdapter` writes to the dev
//! mailbox (compose volume-mounts it onto the host). Not a shell-out: a plain path
//! read, hard-failing if the file is missing / patternless past the poll deadline.
//! `capture_regex` takes the **last** match (the most recent OTP).

use std::path::Path;
use std::time::{Duration, Instant};

use indexmap::IndexMap;
use serde_json::{json, Value};

use super::context::ScenarioContext;
use super::http_step::capture_regex;
use super::runner::StepResult;
use super::schema::FileReadStep;

/// Execute a file-read step, returning a runner-compatible [`StepResult`].
pub async fn run_file_step(step: &FileReadStep, ctx: &mut ScenarioContext) -> StepResult {
    let t0 = Instant::now();
    let fail = |t0: Instant, e: String| StepResult {
        name: step.name.clone(),
        kind: "file",
        ok: false,
        duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
        captured: IndexMap::new(),
        error: Some(e),
    };

    let path_str = match ctx.interpolate(&Value::String(step.file.clone())) {
        Ok(Value::String(s)) => s,
        Ok(other) => other.to_string(),
        Err(e) => return fail(t0, e),
    };
    let path = Path::new(&path_str);

    let deadline = step
        .poll
        .as_ref()
        .map(|p| Instant::now() + Duration::from_secs_f64(p.timeout_seconds));
    let interval = Duration::from_secs_f64(
        step.poll
            .as_ref()
            .map_or(0.05, |p| (p.interval_ms as f64 / 1000.0).max(0.05)),
    );

    let mut captured: IndexMap<String, Value> = IndexMap::new();
    let mut last_err = String::new();
    let mut got_body = false;
    loop {
        match std::fs::read_to_string(path) {
            Ok(body_text) => {
                let result = json!({
                    "path": path_str,
                    "body_text": body_text,
                    "size": body_text.len(),
                });
                got_body = true;
                if step.capture_regex.is_empty() {
                    break;
                }
                match capture_regex(&result, &step.capture_regex, true) {
                    Ok(c) => {
                        captured = c;
                        break;
                    }
                    Err(e) => last_err = e,
                }
            }
            Err(_) => last_err = format!("file not found: {}", path.display()),
        }
        match deadline {
            Some(d) if Instant::now() < d => tokio::time::sleep(interval).await,
            _ => break,
        }
    }

    if !got_body || (!step.capture_regex.is_empty() && captured.is_empty()) {
        return fail(
            t0,
            if last_err.is_empty() {
                "file read produced no captures".to_string()
            } else {
                last_err
            },
        );
    }

    for (name, value) in &captured {
        ctx.variables.insert(name.clone(), value.clone());
    }
    StepResult {
        name: step.name.clone(),
        kind: "file",
        ok: true,
        duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
        captured,
        error: None,
    }
}
