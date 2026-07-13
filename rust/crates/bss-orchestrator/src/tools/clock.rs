//! `clock.*` tools. Port of the clock slice of
//! `orchestrator/bss_orchestrator/tools/ops.py`.
//!
//! `clock.now` reads the authoritative system clock; the advance/freeze/unfreeze
//! tools are the v0.1 `NOT_IMPLEMENTED` stubs (virtual-clock control is scenario-
//! runner territory). Dependency-free, so they are the P5c pilot tool family.
//!
//! Descriptions are the LLM-facing semantic contract (R2) — embedded byte-for-byte
//! from the Python docstrings via `include_str!` and pinned by the golden test.

use std::sync::Arc;

use chrono::Timelike;
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{RegisteredTool, ToolCtx, ToolError, ToolRegistry};

const DESC_NOW: &str = include_str!("desc/clock_now.txt");
const DESC_ADVANCE: &str = include_str!("desc/clock_advance.txt");
const DESC_FREEZE: &str = include_str!("desc/clock_freeze.txt");
const DESC_UNFREEZE: &str = include_str!("desc/clock_unfreeze.txt");

fn clock_not_implemented() -> Value {
    json!({
        "error": "NOT_IMPLEMENTED",
        "message": "Virtual clock control ships in Phase 11 (scenario runner). In v0.1 \
                    the system clock is authoritative — use clock.now to read it.",
    })
}

/// Register the four `clock.*` tools into `registry`.
pub fn register_clock_tools(registry: &mut ToolRegistry) {
    registry.register(RegisteredTool {
        name: "clock.now".to_string(),
        description: DESC_NOW.to_string(),
        func: Arc::new(|_args: Value, _ctx: ToolCtx| {
            async move {
                // `.replace(microsecond=0).isoformat()` → whole-second `+00:00`.
                let now = bss_clock::now()
                    .with_nanosecond(0)
                    .unwrap_or_else(bss_clock::now);
                Ok(json!({ "now": bss_clock::isoformat(now), "source": "system" }))
            }
            .boxed()
        }),
    });

    registry.register(RegisteredTool {
        name: "clock.advance".to_string(),
        description: DESC_ADVANCE.to_string(),
        func: Arc::new(|args: Value, _ctx: ToolCtx| {
            async move {
                let duration = args.get("duration").cloned().unwrap_or(Value::Null);
                let mut out = clock_not_implemented();
                if let Value::Object(map) = &mut out {
                    map.insert("duration".to_string(), duration);
                }
                Ok(out)
            }
            .boxed()
        }),
    });

    registry.register(RegisteredTool {
        name: "clock.freeze".to_string(),
        description: DESC_FREEZE.to_string(),
        func: Arc::new(|args: Value, _ctx: ToolCtx| {
            async move {
                let at = args.get("at").cloned().unwrap_or(Value::Null);
                let mut out = clock_not_implemented();
                if let Value::Object(map) = &mut out {
                    map.insert("requestedAt".to_string(), at);
                }
                Ok(out)
            }
            .boxed()
        }),
    });

    registry.register(RegisteredTool {
        name: "clock.unfreeze".to_string(),
        description: DESC_UNFREEZE.to_string(),
        func: Arc::new(|_args: Value, _ctx: ToolCtx| {
            async move { Ok::<Value, ToolError>(clock_not_implemented()) }.boxed()
        }),
    });
}
