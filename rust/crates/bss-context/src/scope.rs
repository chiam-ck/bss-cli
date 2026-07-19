//! Task-local scope for the current [`RequestCtx`].
//!
//! This is the §2.1 translation of the ambient ContextVar: middleware sets the
//! scope for the duration of a request, and the two distant chokepoint readers
//! (bss-clients on outbound calls, bss-events when stamping audit rows) read it
//! without every intermediate signature carrying a `&RequestCtx`.
//!
//! **Doctrine rule:** task-locals live *only* in this crate and are read *only*
//! by those two chokepoints. Everything else takes an explicit `&RequestCtx`
//! (available as `Extension<RequestCtx>` on any handler). A future Rust doctrine
//! guard greps for `task_local!` outside `crates/bss-context`.

use std::future::Future;

use crate::ctx::RequestCtx;

tokio::task_local! {
    static CURRENT: RequestCtx;
}

/// Run `f` with `ctx` installed as the current context. Nested scopes shadow;
/// concurrent tasks are isolated (each `scope` is its own task-local frame).
pub async fn scope<F>(ctx: RequestCtx, f: F) -> F::Output
where
    F: Future,
{
    CURRENT.scope(ctx, f).await
}

/// The current context, or [`RequestCtx::default`] when called outside any
/// [`scope`] — mirroring the Python ContextVar's default value.
pub fn current() -> RequestCtx {
    CURRENT.try_with(RequestCtx::clone).unwrap_or_default()
}

/// The current context if one is installed, else `None`. Use this when "no
/// active request" must be distinguished from "a request with default values".
pub fn try_current() -> Option<RequestCtx> {
    CURRENT.try_with(RequestCtx::clone).ok()
}
