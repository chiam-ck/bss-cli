//! Scenario execution — parse → setup → steps → teardown → report. Port of
//! `cli/bss_cli/scenarios/runner.py`.
//!
//! A straight list-walk: no conditionals, no retries. Each step is timed; failures
//! short-circuit the remaining steps but teardown always runs (its own failure fails
//! the scenario without masking the primary error). Never panics — failures are packed
//! into [`ScenarioResult`].
//!
//! **This slice:** setup (reset/freeze), `action:` / `assert:` / `http:` / `file:`
//! steps, teardown (unfreeze), captures, and the operator-facing report. `ask:` steps
//! report a clear "not wired yet" failure until the LLM-executor slice lands.

use std::time::Instant;

use bss_context::{new_request_id, RequestCtx};
use bss_orchestrator::{Settings, ToolRegistry};
use indexmap::IndexMap;
use serde_json::{Map, Value};

use super::actions::Actions;
use super::assertions::poll_until;
use super::context::ScenarioContext;
use super::schema::{LlmMode, Scenario, Step};

/// One executed step's outcome.
#[derive(Debug, Clone)]
pub struct StepResult {
    pub name: String,
    pub kind: &'static str,
    pub ok: bool,
    pub duration_ms: f64,
    pub captured: IndexMap<String, Value>,
    pub error: Option<String>,
}

/// A whole scenario's outcome — the report surface.
#[derive(Debug, Clone)]
pub struct ScenarioResult {
    pub scenario: String,
    pub ok: bool,
    pub duration_ms: f64,
    pub steps: Vec<StepResult>,
    pub setup_error: Option<String>,
    pub teardown_error: Option<String>,
    pub variables: IndexMap<String, Value>,
}

fn ms_since(t0: Instant) -> f64 {
    t0.elapsed().as_secs_f64() * 1000.0
}

/// Execute `scenario` end-to-end. Never raises — packages failures into the result.
pub async fn run_scenario(
    scenario: &Scenario,
    _mode: LlmMode,
    registry: &ToolRegistry,
) -> ScenarioResult {
    let t0 = Instant::now();

    // Mark every downstream call as scenario-originated (Python's
    // `use_scenario_context`): `channel="scenario"`, `actor="scenario:<name>"`.
    let ctx = RequestCtx {
        request_id: new_request_id(),
        actor: format!("scenario:{}", scenario.name),
        channel: "scenario".to_string(),
        ..Default::default()
    };
    bss_context::scope(ctx, run_inner(scenario, registry, t0)).await
}

async fn run_inner(scenario: &Scenario, registry: &ToolRegistry, t0: Instant) -> ScenarioResult {
    let tenant = Settings::from_env().tenant_default;
    let actions = Actions::new(
        registry.clone(),
        format!("scenario:{}", scenario.name),
        tenant,
    );

    let mut result = ScenarioResult {
        scenario: scenario.name.clone(),
        ok: true,
        duration_ms: 0.0,
        steps: Vec::new(),
        setup_error: None,
        teardown_error: None,
        variables: IndexMap::new(),
    };

    // Seed = setup.variables overlaid by top-level variables (Python merge order).
    let mut seed = scenario.setup.variables.clone();
    for (k, v) in &scenario.variables {
        seed.insert(k.clone(), v.clone());
    }
    let mut context = match ScenarioContext::new(&seed) {
        Ok(c) => c,
        Err(e) => {
            result.ok = false;
            result.setup_error = Some(e);
            result.duration_ms = ms_since(t0);
            return result;
        }
    };

    // Setup.
    if let Err(e) = run_setup(scenario, &actions, &context).await {
        result.ok = false;
        result.setup_error = Some(e);
        result.variables = context.snapshot();
        result.duration_ms = ms_since(t0);
        return result;
    }

    // Steps — short-circuit on first failure, teardown still runs.
    for step in &scenario.steps {
        let step_result = run_step(step, &actions, &mut context).await;
        let failed = !step_result.ok;
        result.steps.push(step_result);
        if failed {
            result.ok = false;
            break;
        }
    }

    // Teardown — always runs; its failure fails the scenario without masking steps.
    if let Err(e) = run_teardown(scenario, &actions, &context).await {
        result.teardown_error = Some(e);
        result.ok = false;
    }

    result.variables = context.snapshot();
    result.duration_ms = ms_since(t0);
    result
}

async fn run_setup(
    scenario: &Scenario,
    actions: &Actions,
    ctx: &ScenarioContext,
) -> Result<(), String> {
    let setup = &scenario.setup;
    if setup.reset_operational_data {
        let mut args = Map::new();
        args.insert(
            "reset_sequences".to_string(),
            Value::Bool(setup.reset_sequences),
        );
        actions.run("admin.reset_operational_data", &args).await?;
    }
    if let Some(at) = &setup.freeze_clock_at {
        let at = ctx.interpolate(&Value::String(at.clone()))?;
        let mut args = Map::new();
        args.insert("at".to_string(), at);
        actions.run("clock.freeze", &args).await?;
    }
    Ok(())
}

async fn run_teardown(
    scenario: &Scenario,
    actions: &Actions,
    _ctx: &ScenarioContext,
) -> Result<(), String> {
    if scenario.teardown.unfreeze_clock {
        actions.run("clock.unfreeze", &Map::new()).await?;
    }
    Ok(())
}

async fn run_step(step: &Step, actions: &Actions, ctx: &mut ScenarioContext) -> StepResult {
    match step {
        Step::Action(_) => run_action(step, actions, ctx).await,
        Step::Assert(_) => run_assert(step, actions, ctx).await,
        Step::Http(s) => super::http_step::run_http_step(s, ctx).await,
        Step::File(s) => super::file_step::run_file_step(s, ctx).await,
        Step::Ask(_) => StepResult {
            name: step.name().to_string(),
            kind: step.kind(),
            ok: false,
            duration_ms: 0.0,
            captured: IndexMap::new(),
            error: Some(
                "ask: steps are not wired yet — they land with the LLM executor slice".to_string(),
            ),
        },
    }
}

async fn run_action(step: &Step, actions: &Actions, ctx: &mut ScenarioContext) -> StepResult {
    let Step::Action(s) = step else {
        unreachable!("run_action called on non-action step")
    };
    let t0 = Instant::now();
    let fail = |t0: Instant, e: String| StepResult {
        name: s.name.clone(),
        kind: "action",
        ok: false,
        duration_ms: ms_since(t0),
        captured: IndexMap::new(),
        error: Some(e),
    };

    if !actions.is_known(&s.action) {
        return fail(t0, format!("unknown action: {:?}", s.action));
    }
    let args = match ctx.interpolate(&Value::Object(s.args.clone())) {
        Ok(Value::Object(m)) => m,
        Ok(_) => Map::new(),
        Err(e) => return fail(t0, e),
    };
    let result = match actions.run(&s.action, &args).await {
        Ok(r) => r,
        Err(e) => return fail(t0, e),
    };
    let captured = match ctx.apply_captures(&result, &s.capture) {
        Ok(c) => c,
        Err(e) => return fail(t0, e),
    };
    StepResult {
        name: s.name.clone(),
        kind: "action",
        ok: true,
        duration_ms: ms_since(t0),
        captured,
        error: None,
    }
}

async fn run_assert(step: &Step, actions: &Actions, ctx: &mut ScenarioContext) -> StepResult {
    let Step::Assert(s) = step else {
        unreachable!("run_assert called on non-assert step")
    };
    let t0 = Instant::now();
    let call = &s.assert_call;
    let fail = |t0: Instant, e: String| StepResult {
        name: s.name.clone(),
        kind: "assert",
        ok: false,
        duration_ms: ms_since(t0),
        captured: IndexMap::new(),
        error: Some(e),
    };

    if !actions.is_known(&call.tool) {
        return fail(t0, format!("unknown action: {:?}", call.tool));
    }
    let args = match ctx.interpolate(&Value::Object(call.args.clone())) {
        Ok(Value::Object(m)) => m,
        Ok(_) => Map::new(),
        Err(e) => return fail(t0, e),
    };
    let expect = match ctx.interpolate(&Value::Object(call.expect.clone())) {
        Ok(Value::Object(m)) => m,
        Ok(_) => Map::new(),
        Err(e) => return fail(t0, e),
    };

    let assertion = poll_until(
        || actions.run(&call.tool, &args),
        &expect,
        call.poll.as_ref(),
    )
    .await;
    match assertion {
        Ok(a) if a.ok => StepResult {
            name: s.name.clone(),
            kind: "assert",
            ok: true,
            duration_ms: ms_since(t0),
            captured: IndexMap::new(),
            error: None,
        },
        Ok(a) => fail(t0, a.format()),
        Err(e) => fail(t0, e),
    }
}
