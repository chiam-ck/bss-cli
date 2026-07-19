//! HTTP step runner — port of `cli/bss_cli/scenarios/http_step.py`.
//!
//! The `http:` step drives a service (usually a portal) through its public HTTP
//! surface: `"GET /url"` / `"POST /url"`, form or JSON body, expect (status / body
//! contains / header / body-json), optional poll, and capture (JSONPath over the
//! synthetic result `{status, headers, cookies, body, body_text}` + regex over a named
//! source). reqwest reads the whole body via `.text()`, which drains an SSE stream to
//! EOF — so `drain_stream` needs no special path here.

use std::time::{Duration, Instant};

use fancy_regex::Regex;
use indexmap::IndexMap;
use reqwest::redirect::Policy;
use serde_json::{Map, Value};

use super::context::{jsonpath_first, ScenarioContext};
use super::runner::StepResult;
use super::schema::{HttpExpect, HttpRegexCapture, HttpStep, StatusSpec};

/// Execute an HTTP step, returning a runner-compatible [`StepResult`].
pub async fn run_http_step(step: &HttpStep, ctx: &mut ScenarioContext) -> StepResult {
    let t0 = Instant::now();
    let fail = |t0: Instant, e: String| StepResult {
        name: step.name.clone(),
        kind: "http",
        ok: false,
        duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
        captured: IndexMap::new(),
        error: Some(e),
    };

    let deadline = step
        .poll
        .as_ref()
        .map(|p| Instant::now() + Duration::from_secs_f64(p.timeout_seconds));
    let interval = Duration::from_secs_f64(
        step.poll
            .as_ref()
            .map_or(0.05, |p| (p.interval_ms as f64 / 1000.0).max(0.05)),
    );

    let mut result;
    let mut last_fails;
    loop {
        result = match do_request(step, ctx).await {
            Ok(r) => r,
            Err(e) => return fail(t0, e),
        };
        last_fails = check_expect(&step.expect, &result, ctx);
        if last_fails.is_empty() {
            break;
        }
        match deadline {
            Some(d) if Instant::now() < d => tokio::time::sleep(interval).await,
            _ => break,
        }
    }

    if !last_fails.is_empty() {
        return fail(t0, last_fails.join("; "));
    }

    let mut captured: IndexMap<String, Value> = IndexMap::new();
    for (var, path) in &step.capture {
        let Some(path) = path.as_str() else {
            return fail(t0, format!("capture {var:?}: path must be a string"));
        };
        match jsonpath_first(&result, path) {
            Some(v) => {
                captured.insert(var.clone(), v.clone());
            }
            None => {
                return fail(
                    t0,
                    format!("capture {var:?}: jsonpath {path:?} matched nothing in HTTP result"),
                )
            }
        }
    }
    match capture_regex(&result, &step.capture_regex, false) {
        Ok(more) => captured.extend(more),
        Err(e) => return fail(t0, e),
    }

    for (name, value) in &captured {
        ctx.variables.insert(name.clone(), value.clone());
    }
    StepResult {
        name: step.name.clone(),
        kind: "http",
        ok: true,
        duration_ms: t0.elapsed().as_secs_f64() * 1000.0,
        captured,
        error: None,
    }
}

/// The synthetic result shape captures + expects resolve against.
struct HttpResult {
    value: Value,
    body_text: String,
    headers: IndexMap<String, String>,
}

impl std::ops::Deref for HttpResult {
    type Target = Value;
    fn deref(&self) -> &Value {
        &self.value
    }
}

async fn do_request(step: &HttpStep, ctx: &ScenarioContext) -> Result<HttpResult, String> {
    let (method, url) = parse_method_url(&step.http, &step.base_url, ctx)?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(step.timeout_seconds))
        .redirect(if step.follow_redirects {
            Policy::default()
        } else {
            Policy::none()
        })
        .build()
        .map_err(|e| e.to_string())?;

    let method = reqwest::Method::from_bytes(method.as_bytes()).map_err(|e| e.to_string())?;
    let mut req = client.request(method, &url);
    for (k, v) in &step.headers {
        req = req.header(k, interp_to_string(ctx, v)?);
    }
    if !step.cookies.is_empty() {
        let jar: Vec<String> = step
            .cookies
            .iter()
            .map(|(k, v)| interp_to_string(ctx, v).map(|val| format!("{k}={val}")))
            .collect::<Result<_, _>>()?;
        req = req.header("Cookie", jar.join("; "));
    }
    if !step.form.is_empty() {
        let pairs: Vec<(String, String)> = step
            .form
            .iter()
            .map(|(k, v)| interp_to_string(ctx, v).map(|val| (k.clone(), val)))
            .collect::<Result<_, _>>()?;
        req = req.form(&pairs);
    } else if let Some(body) = &step.json_body {
        req = req.json(&ctx.interpolate(&Value::Object(body.clone()))?);
    }

    let resp = req.send().await.map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let mut headers: IndexMap<String, String> = IndexMap::new();
    let mut cookies: Map<String, Value> = Map::new();
    for (name, value) in resp.headers().iter() {
        let val = value.to_str().unwrap_or("").to_string();
        if name.as_str().eq_ignore_ascii_case("set-cookie") {
            if let Some((k, v)) = parse_set_cookie(&val) {
                cookies.insert(k, Value::String(v));
            }
        }
        headers.insert(name.as_str().to_lowercase(), val);
    }
    let ctype = headers.get("content-type").cloned().unwrap_or_default();
    let body_text = resp.text().await.map_err(|e| e.to_string())?;
    let body = if ctype.contains("application/json") && !body_text.is_empty() {
        serde_json::from_str(&body_text).unwrap_or(Value::Null)
    } else {
        Value::Null
    };

    let value = serde_json::json!({
        "status": status,
        "headers": Value::Object(headers.iter().map(|(k, v)| (k.clone(), Value::String(v.clone()))).collect()),
        "cookies": Value::Object(cookies),
        "body": body,
        "body_text": body_text,
    });
    Ok(HttpResult {
        value,
        body_text,
        headers,
    })
}

/// `"GET /url"` → `(method, absolute-url)`. URLs may be absolute or relative to the
/// interpolated `base_url`.
fn parse_method_url(
    http: &str,
    base_url: &str,
    ctx: &ScenarioContext,
) -> Result<(String, String), String> {
    let (method, url) = http.trim().split_once(' ').unwrap_or((http.trim(), ""));
    let method = method.to_ascii_uppercase();
    let url = interp_to_string(ctx, &Value::String(url.trim().to_string()))?;
    if url.starts_with("http://") || url.starts_with("https://") {
        return Ok((method, url));
    }
    let base = interp_to_string(ctx, &Value::String(base_url.to_string()))?;
    Ok((
        method,
        format!(
            "{}/{}",
            base.trim_end_matches('/'),
            url.trim_start_matches('/')
        ),
    ))
}

/// Interpolate a value and render it as a request string (Python `str(...)`).
fn interp_to_string(ctx: &ScenarioContext, v: &Value) -> Result<String, String> {
    match ctx.interpolate(v)? {
        Value::String(s) => Ok(s),
        Value::Null => Ok("null".to_string()),
        Value::Bool(b) => Ok(b.to_string()),
        Value::Number(n) => Ok(n.to_string()),
        other => Ok(other.to_string()),
    }
}

/// `name=value; Path=/; …` → `(name, value)` (the first pair only).
fn parse_set_cookie(raw: &str) -> Option<(String, String)> {
    let first = raw.split(';').next()?;
    let (k, v) = first.split_once('=')?;
    Some((k.trim().to_string(), v.trim().to_string()))
}

/// Human-readable failure reasons (empty = pass) for an [`HttpExpect`].
fn check_expect(expect: &HttpExpect, result: &HttpResult, ctx: &ScenarioContext) -> Vec<String> {
    let mut fails = Vec::new();
    let status = result.get("status").and_then(Value::as_u64).unwrap_or(0);
    if let Some(spec) = &expect.status {
        let want: Vec<i64> = match spec {
            StatusSpec::One(n) => vec![*n],
            StatusSpec::Many(v) => v.clone(),
        };
        if !want.contains(&(status as i64)) {
            fails.push(format!("status {status} not in {want:?}"));
        }
    }
    for raw in &expect.body_contains {
        if let Ok(needle) = interp_to_string(ctx, &Value::String(raw.clone())) {
            if !result.body_text.contains(&needle) {
                fails.push(format!("body_contains: {needle:?} not found"));
            }
        }
    }
    for raw in &expect.body_not_contains {
        if let Ok(needle) = interp_to_string(ctx, &Value::String(raw.clone())) {
            if result.body_text.contains(&needle) {
                fails.push(format!("body_not_contains: {needle:?} present"));
            }
        }
    }
    for (key, want) in &expect.headers_match {
        let got = result.headers.get(&key.to_lowercase()).cloned();
        let want_resolved = interp_to_string(ctx, want).unwrap_or_default();
        if got.as_deref() != Some(want_resolved.as_str()) {
            fails.push(format!("header {key}={got:?}, expected {want_resolved:?}"));
        }
    }
    for (key, want) in &expect.body_json_equals {
        let got = result.get("body").and_then(|b| b.get(key));
        let want_resolved = ctx.interpolate(want).unwrap_or(Value::Null);
        if got != Some(&want_resolved) {
            fails.push(format!(
                "body_json.{key}={got:?}, expected {want_resolved:?}"
            ));
        }
    }
    fails
}

/// Regex capture over a named source in the result. `last_match` (file step) takes the
/// final match; otherwise (HTTP step) the first — matching Python's `findall[-1]` vs
/// `search`. `group` is 1-based.
pub(super) fn capture_regex(
    result: &Value,
    captures: &IndexMap<String, HttpRegexCapture>,
    last_match: bool,
) -> Result<IndexMap<String, Value>, String> {
    let mut newly = IndexMap::new();
    for (var, cfg) in captures {
        let source = resolve_source(result, &cfg.source);
        let source = source
            .and_then(|v| v.as_str().map(str::to_string))
            .ok_or_else(|| {
                format!(
                    "capture_regex {var:?}: source {:?} did not resolve to a string",
                    cfg.source
                )
            })?;
        let re = Regex::new(&cfg.pattern).map_err(|e| format!("capture_regex {var:?}: {e}"))?;
        let caps = if last_match {
            re.captures_iter(&source).flatten().last()
        } else {
            re.captures(&source).ok().flatten()
        };
        let grp = caps
            .as_ref()
            .and_then(|c| c.get(cfg.group.max(0) as usize))
            .ok_or_else(|| {
                format!(
                    "capture_regex {var:?}: pattern {:?} did not match source",
                    cfg.pattern
                )
            })?;
        newly.insert(var.clone(), Value::String(grp.as_str().to_string()));
    }
    Ok(newly)
}

/// Dot-path over the synthetic result (`headers.location`, `body_text`, …).
fn resolve_source<'a>(result: &'a Value, path: &str) -> Option<&'a Value> {
    let mut node = result;
    for part in path.split('.') {
        node = node.get(part)?;
    }
    Some(node)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ctx() -> ScenarioContext {
        let mut c = ScenarioContext::default();
        c.variables
            .insert("base".into(), Value::String("http://host:9001".into()));
        c
    }

    #[test]
    fn method_url_relative_and_absolute() {
        let c = ctx();
        assert_eq!(
            parse_method_url("GET /plans", "http://host:9001", &c).unwrap(),
            ("GET".to_string(), "http://host:9001/plans".to_string())
        );
        assert_eq!(
            parse_method_url("POST http://x/y", "http://ignored", &c).unwrap(),
            ("POST".to_string(), "http://x/y".to_string())
        );
    }

    #[test]
    fn regex_capture_first_and_last() {
        let result = json!({"headers": {"location": "/signup?session=abc123&x=1"}});
        let mut caps = IndexMap::new();
        caps.insert(
            "sid".to_string(),
            HttpRegexCapture {
                source: "headers.location".into(),
                pattern: r"session=([0-9a-z]+)".into(),
                group: 1,
            },
        );
        let got = capture_regex(&result, &caps, false).unwrap();
        assert_eq!(got.get("sid").unwrap(), &json!("abc123"));

        // last-match (file-step semantics) picks the most recent OTP.
        let body = json!({"body_text": "OTP 111111\nOTP 222222\n"});
        let mut otp = IndexMap::new();
        otp.insert(
            "code".to_string(),
            HttpRegexCapture {
                source: "body_text".into(),
                pattern: r"OTP (\d{6})".into(),
                group: 1,
            },
        );
        assert_eq!(
            capture_regex(&body, &otp, true)
                .unwrap()
                .get("code")
                .unwrap(),
            &json!("222222")
        );
    }

    #[test]
    fn expect_status_and_body_contains() {
        let result = HttpResult {
            value: json!({"status": 303, "body": Value::Null}),
            body_text: "Welcome CUST-1".to_string(),
            headers: IndexMap::from([("location".to_string(), "/next".to_string())]),
        };
        let expect = HttpExpect {
            status: Some(StatusSpec::Many(vec![301, 303])),
            body_contains: vec!["CUST-1".to_string()],
            headers_match: serde_json::Map::from_iter([(
                "location".to_string(),
                Value::String("/next".to_string()),
            )]),
            ..Default::default()
        };
        assert!(check_expect(&expect, &result, &ctx()).is_empty());

        let bad = HttpExpect {
            status: Some(StatusSpec::One(200)),
            ..Default::default()
        };
        assert!(!check_expect(&bad, &result, &ctx()).is_empty());
    }
}
