//! axum middleware that resolves [`RequestCtx`] and installs it for the request.
//!
//! Port of `RequestIdMiddleware`. Runs *inside* the token middleware
//! (`bss-middleware`, next crate) so it can read the [`ServiceIdentity`] that
//! layer stashed in extensions. It:
//! 1. builds the `RequestCtx` from headers + the token layer's identity,
//! 2. inserts it into request extensions (handlers use `Extension<RequestCtx>`),
//! 3. runs the rest of the stack inside the task-local [`scope`], so the S2S and
//!    audit chokepoints see the caller context,
//! 4. echoes `x-request-id` on the response.

use axum::{extract::Request, http::HeaderValue, middleware::Next, response::Response};

use crate::ctx::{RequestCtx, ServiceIdentity, HDR_REQUEST_ID};
use crate::scope::scope;

/// Middleware fn for `axum::middleware::from_fn`.
pub async fn propagate_context(mut req: Request, next: Next) -> Response {
    let service_identity = req
        .extensions()
        .get::<ServiceIdentity>()
        .map(|s| s.0.clone());
    let ctx = RequestCtx::from_headers(req.headers(), service_identity);
    let request_id = ctx.request_id.clone();
    req.extensions_mut().insert(ctx.clone());

    let mut resp = scope(ctx, next.run(req)).await;

    if let Ok(value) = HeaderValue::from_str(&request_id) {
        resp.headers_mut().insert(HDR_REQUEST_ID, value);
    }
    resp
}
