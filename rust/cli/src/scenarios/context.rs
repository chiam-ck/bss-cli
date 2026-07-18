//! Variable interpolation + capture for scenarios. Port of
//! `cli/bss_cli/scenarios/context.py`.
//!
//! Scenarios reference earlier values via `{{ var }}` anywhere in step args or `ask:`
//! prompt strings. The runner seeds a [`ScenarioContext`] with `setup.variables` +
//! `variables` + a synthetic `run_id` (short hex), and every successful step's
//! `capture` entries merge back in via [`ScenarioContext::apply_captures`].
//!
//! Interpolation is plain — no filters, no Jinja. Capture uses a minimal JSONPath
//! (`$`, `.field`, `[index]` chains) — the only shapes the real corpus uses
//! (`$.id`, `$[0].id`, `$[0].msisdn`); anything richer errors loud.

use fancy_regex::Regex;
use indexmap::IndexMap;
use serde_json::{Map, Value};

/// Runtime variable bag threaded through every step.
#[derive(Debug, Default, Clone)]
pub struct ScenarioContext {
    pub variables: IndexMap<String, Value>,
}

// Same as Python's `_VAR_RE`: `{{ name }}` with optional surrounding whitespace. The
// pattern is a compile-time constant, so the build cannot fail at runtime.
#[allow(clippy::expect_used)]
fn var_re() -> Regex {
    Regex::new(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}").expect("valid var regex")
}

impl ScenarioContext {
    /// Seed the bag with a fresh `run_id` then the resolved seed values (each
    /// interpolated against already-defined vars, so a seed can reference `run_id`).
    pub fn new(seed: &Map<String, Value>) -> Result<Self, String> {
        let mut ctx = ScenarioContext::default();
        ctx.variables
            .insert("run_id".to_string(), Value::String(run_id()));
        for (k, v) in seed {
            let resolved = ctx.interpolate(v)?;
            ctx.variables.insert(k.clone(), resolved);
        }
        Ok(ctx)
    }

    // ── interpolation ───────────────────────────────────────────────────────

    /// Recursively substitute `{{ name }}` in strings, lists, and objects. Non-string
    /// leaves pass through. A whole-string placeholder preserves the captured value's
    /// original type (int/list round-trip); a mixed string coerces to text.
    pub fn interpolate(&self, value: &Value) -> Result<Value, String> {
        match value {
            Value::String(s) => self.interpolate_str(s),
            Value::Array(a) => {
                let mut out = Vec::with_capacity(a.len());
                for v in a {
                    out.push(self.interpolate(v)?);
                }
                Ok(Value::Array(out))
            }
            Value::Object(m) => {
                let mut out = Map::new();
                for (k, v) in m {
                    out.insert(k.clone(), self.interpolate(v)?);
                }
                Ok(Value::Object(out))
            }
            other => Ok(other.clone()),
        }
    }

    fn interpolate_str(&self, s: &str) -> Result<Value, String> {
        let re = var_re();
        // Whole string is one placeholder → preserve the value's type.
        if let Ok(Some(caps)) = re.captures(s.trim()) {
            if let Some(m) = caps.get(0) {
                if m.start() == 0 && m.end() == s.trim().len() {
                    let name = &caps[1];
                    return self.resolve(name).cloned();
                }
            }
        }
        // Otherwise stringify each match in place.
        let mut out = String::new();
        let mut last = 0usize;
        for caps in re.captures_iter(s).flatten() {
            let Some(whole) = caps.get(0) else { continue };
            out.push_str(&s[last..whole.start()]);
            let name = &caps[1];
            out.push_str(&value_to_string(self.resolve(name)?));
            last = whole.end();
        }
        out.push_str(&s[last..]);
        Ok(Value::String(out))
    }

    fn resolve(&self, name: &str) -> Result<&Value, String> {
        self.variables
            .get(name)
            .ok_or_else(|| format!("undefined scenario variable: {name:?}"))
    }

    // ── capture ─────────────────────────────────────────────────────────────

    /// Evaluate each JSONPath against `result` and merge into the bag; returns the
    /// newly-captured keys for reporting. A miss errors loud (selector drift).
    pub fn apply_captures(
        &mut self,
        result: &Value,
        captures: &Map<String, Value>,
    ) -> Result<IndexMap<String, Value>, String> {
        let mut newly = IndexMap::new();
        for (var_name, path_expr) in captures {
            let path = path_expr
                .as_str()
                .ok_or_else(|| format!("capture {var_name:?}: path must be a string"))?;
            let found = jsonpath_first(result, path).ok_or_else(|| {
                format!("capture {var_name:?}: jsonpath {path:?} matched nothing in tool result")
            })?;
            self.variables.insert(var_name.clone(), found.clone());
            newly.insert(var_name.clone(), found.clone());
        }
        Ok(newly)
    }

    pub fn snapshot(&self) -> IndexMap<String, Value> {
        self.variables.clone()
    }
}

/// Short random hex run id (Python's `secrets.token_hex(4)` → 8 hex chars).
fn run_id() -> String {
    format!("{:08x}", rand::random::<u32>())
}

/// A JSON value rendered as Python's `str(...)` would for interpolation: bare scalars
/// without quotes, `true`/`false`/`null` lower-cased, containers via compact JSON.
fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "null".to_string(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Minimal JSONPath: `$` root, `.field` object access, `[index]` array index, chained.
/// Returns the first match. `None` on any miss (matching `jsonpath.find()` == empty).
fn jsonpath_first<'a>(root: &'a Value, expr: &str) -> Option<&'a Value> {
    let rest = expr.strip_prefix('$')?;
    let mut cur = root;
    let mut chars = rest.chars().peekable();
    while let Some(&c) = chars.peek() {
        match c {
            '.' => {
                chars.next();
                let mut key = String::new();
                while let Some(&nc) = chars.peek() {
                    if nc == '.' || nc == '[' {
                        break;
                    }
                    key.push(nc);
                    chars.next();
                }
                cur = cur.get(&key)?;
            }
            '[' => {
                chars.next();
                let mut idx = String::new();
                for nc in chars.by_ref() {
                    if nc == ']' {
                        break;
                    }
                    idx.push(nc);
                }
                let i: usize = idx.trim_matches(['\'', '"']).parse().ok()?;
                cur = cur.get(i)?;
            }
            _ => return None,
        }
    }
    Some(cur)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn whole_placeholder_preserves_type() {
        let mut ctx = ScenarioContext::default();
        ctx.variables.insert("n".into(), json!(7));
        assert_eq!(ctx.interpolate(&json!("{{ n }}")).unwrap(), json!(7));
    }

    #[test]
    fn mixed_placeholder_stringifies() {
        let mut ctx = ScenarioContext::default();
        ctx.variables.insert("id".into(), json!("CUST-001"));
        assert_eq!(
            ctx.interpolate(&json!("hi {{ id }}!")).unwrap(),
            json!("hi CUST-001!")
        );
    }

    #[test]
    fn undefined_var_errors() {
        let ctx = ScenarioContext::default();
        assert!(ctx.interpolate(&json!("{{ nope }}")).is_err());
    }

    #[test]
    fn jsonpath_covers_corpus_shapes() {
        let v = json!({"id": "CUST-1", "list": [{"id": "SUB-1", "msisdn": "6591"}]});
        assert_eq!(jsonpath_first(&v, "$.id").unwrap(), &json!("CUST-1"));
        assert_eq!(jsonpath_first(&v, "$.list[0].id").unwrap(), &json!("SUB-1"));
        let arr = json!([{"id": "SUB-1", "msisdn": "6591"}]);
        assert_eq!(jsonpath_first(&arr, "$[0].msisdn").unwrap(), &json!("6591"));
        assert!(jsonpath_first(&v, "$.missing").is_none());
    }

    #[test]
    fn apply_captures_merges_and_reports() {
        let mut ctx = ScenarioContext::default();
        let result = json!({"id": "ORD-9"});
        let mut caps = Map::new();
        caps.insert("order_id".into(), json!("$.id"));
        let newly = ctx.apply_captures(&result, &caps).unwrap();
        assert_eq!(newly.get("order_id").unwrap(), &json!("ORD-9"));
        assert_eq!(ctx.variables.get("order_id").unwrap(), &json!("ORD-9"));
    }
}
