//! Pass/fail rendering for scenario results. Port of
//! `cli/bss_cli/scenarios/reporting.py`.
//!
//! `render_result` prints one scenario (header + per-step table + failure context);
//! `render_summary` prints a multi-scenario roll-up. Python draws Rich tables/panels;
//! the box-drawing chrome is a documented CLI seam — the header text, the per-step
//! cells, and the PASS/FAIL / ✓✗ markers match.

use super::runner::{ScenarioResult, StepResult};

fn kind_icon(kind: &str) -> &'static str {
    match kind {
        "action" => "▶",
        "assert" => "=",
        "ask" => "💬",
        "http" => "🌐",
        "file" => "📄",
        _ => "?",
    }
}

/// Print one scenario result — header, per-step table, and (on failure) the failing
/// step's detail. Returns nothing; the caller decides the exit code from `result.ok`.
pub fn render_result(result: &ScenarioResult) {
    let status = if result.ok { "PASS" } else { "FAIL" };
    println!(
        "{status} — {}  ({:.0} ms)",
        result.scenario, result.duration_ms
    );

    if let Some(err) = &result.setup_error {
        println!("  setup error: {err}");
        return;
    }

    if !result.steps.is_empty() {
        println!(
            "  {:>3}  {:<8} {:<40} {:>7}  st  detail",
            "#", "kind", "step", "ms"
        );
        for (i, step) in result.steps.iter().enumerate() {
            println!(
                "  {:>3}  {:<8} {:<40} {:>7.0}  {}   {}",
                i + 1,
                format!("{} {}", kind_icon(step.kind), step.kind),
                truncate(&step.name, 40),
                step.duration_ms,
                if step.ok { "✓" } else { "✗" },
                step_detail(step),
            );
        }
    }

    if let Some(err) = &result.teardown_error {
        println!("  teardown error: {err}");
    }

    if !result.ok {
        if let Some(last) = result.steps.iter().rev().find(|s| !s.ok) {
            let body = last.error.as_deref().unwrap_or("(no detail captured)");
            println!("  failure in step {:?}:", last.name);
            for line in body.lines() {
                println!("    {line}");
            }
        }
    }
}

fn step_detail(step: &StepResult) -> String {
    if step.ok {
        if step.captured.is_empty() {
            String::new()
        } else {
            step.captured
                .iter()
                .map(|(k, v)| format!("{k}={}", short(v)))
                .collect::<Vec<_>>()
                .join(", ")
        }
    } else {
        step.error
            .as_deref()
            .and_then(|e| e.trim().lines().next())
            .unwrap_or("failed")
            .to_string()
    }
}

/// Print the multi-scenario summary table (used by `run-all`).
pub fn render_summary(results: &[ScenarioResult]) {
    println!("scenario summary");
    println!("  {:<44} status  steps    duration", "scenario");
    for r in results {
        let passed = r.steps.iter().filter(|s| s.ok).count();
        println!(
            "  {:<44} {:<6}  {:>3}/{:<3}  {:.0} ms",
            truncate(&r.scenario, 44),
            if r.ok { "PASS" } else { "FAIL" },
            passed,
            r.steps.len(),
            r.duration_ms,
        );
    }
}

fn short(v: &serde_json::Value) -> String {
    let s = match v {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    if s.chars().count() <= 40 {
        s
    } else {
        let head: String = s.chars().take(39).collect();
        format!("{head}…")
    }
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        s.chars().take(n).collect()
    }
}
