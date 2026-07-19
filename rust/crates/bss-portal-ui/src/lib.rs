//! bss-portal-ui — shared UI rendering for BSS-CLI portals. Rust port of
//! `packages/bss-portal-ui`.
//!
//! **This sub-slice (P6a) ports the pure rendering core:**
//! * [`chat_html`] — chat-bubble HTML for the v0.12 customer chat + v0.13
//!   operator cockpit thread. Both surfaces stream the same shape, so the
//!   renderer is shared and cannot drift. HTML-escape-first, whitelisted
//!   markdown, no raw pass-through (the XSS boundary).
//! * [`sse`] — SSE frame encoding + the status-dot fragment.
//!
//! **Deferred to the P6b portal consumer** (land-with-first-consumer, needs the
//! orchestrator `AgentEvent` projection + a MiniJinja template + the bundled
//! static assets): `agent_log::{project, render_html}` and `paths`
//! (`TEMPLATE_DIR`/`STATIC_DIR` + the `partials/*.html` + `portal_base.css` +
//! vendored htmx assets).
#![forbid(unsafe_code)]

pub mod chat_html;
pub mod sse;

pub use chat_html::{
    render_assistant_bubble, render_chat_markdown, render_tool_pill, strip_reasoning_leakage,
};
pub use sse::{format_frame, status_html};
