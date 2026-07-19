//! bss-context — per-request caller context and its propagation.
//!
//! Rust port of the Python `auth_context` ContextVars + `bss_clients.base`
//! context vars, unified into one [`RequestCtx`]. See phases/2.0/02-TECH-MAPPING.md
//! §2.1 for the ContextVar→(extensions + task-local) translation.
//!
//! Usage in a service:
//! ```ignore
//! use axum::{Router, routing::get, Extension, middleware::from_fn};
//! use bss_context::{propagate_context, RequestCtx};
//!
//! async fn handler(Extension(ctx): Extension<RequestCtx>) -> String {
//!     ctx.actor.clone()
//! }
//! let app = Router::new().route("/", get(handler)).layer(from_fn(propagate_context));
//! ```
//! Handlers take the context explicitly via `Extension<RequestCtx>`. Only
//! bss-clients / bss-events read [`current`] from the task-local scope.
#![forbid(unsafe_code)]

mod ctx;
mod layer;
mod scope;

pub use ctx::{
    new_request_id, RequestCtx, ServiceIdentity, HDR_ACTOR, HDR_CHANNEL, HDR_REQUEST_ID,
    HDR_TENANT, OUT_ACTOR, OUT_CHANNEL, OUT_REQUEST_ID,
};
pub use layer::propagate_context;
pub use scope::{current, scope, try_current};
