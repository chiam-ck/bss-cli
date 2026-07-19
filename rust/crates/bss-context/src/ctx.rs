//! The per-request context struct + header extraction.
//!
//! Unifies what the Python codebase split across two ContextVar stores:
//! - each service's `auth_context.AuthContext` (actor/tenant/roles/permissions/
//!   channel/service_identity), read by policies;
//! - `bss_clients.base` (`_actor_var`/`_channel_var`/`_request_id_var`), read at
//!   the outbound S2S chokepoint.
//!
//! In Rust it is one `RequestCtx`, carried explicitly in axum request extensions
//! (the honest port) and mirrored into a task-local ([`crate::scope`]) so the two
//! distant chokepoint readers (bss-clients, bss-events) don't need it threaded.

use axum::http::HeaderMap;

/// Inbound context headers (lowercase — HTTP header names are case-insensitive
/// and `HeaderMap` stores them lowercased).
pub const HDR_REQUEST_ID: &str = "x-request-id";
pub const HDR_ACTOR: &str = "x-bss-actor";
pub const HDR_CHANNEL: &str = "x-bss-channel";
pub const HDR_TENANT: &str = "x-bss-tenant";

/// Outbound propagation header names (mixed-case, as the Python client emits
/// them; HTTP treats them case-insensitively but we keep the wire bytes stable).
pub const OUT_ACTOR: &str = "X-BSS-Actor";
pub const OUT_CHANNEL: &str = "X-BSS-Channel";
pub const OUT_REQUEST_ID: &str = "X-Request-ID";

/// Resolved caller context for one request. Defaults match the Python
/// `AuthContext` dataclass (actor=`system`, tenant=`DEFAULT`, channel=`system`,
/// service_identity=`default`, roles=`[admin]`, permissions=`[*]`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestCtx {
    pub request_id: String,
    pub actor: String,
    pub tenant: String,
    pub channel: String,
    /// Resolved name of the named token that authenticated the inbound request
    /// (v0.9). **Never** read from a client-supplied header — it is set by the
    /// token middleware via [`ServiceIdentity`] in request extensions. This is
    /// the structural form of doctrine guard #6.
    pub service_identity: String,
    pub roles: Vec<String>,
    pub permissions: Vec<String>,
}

impl Default for RequestCtx {
    fn default() -> Self {
        RequestCtx {
            request_id: String::new(),
            actor: "system".to_string(),
            tenant: "DEFAULT".to_string(),
            channel: "system".to_string(),
            service_identity: "default".to_string(),
            roles: vec!["admin".to_string()],
            permissions: vec!["*".to_string()],
        }
    }
}

/// Marker the token middleware (`bss-middleware`, next crate) inserts into
/// request extensions after validating `X-BSS-API-Token`. [`RequestCtx::from_headers`]
/// reads `service_identity` from here, never from a header — so a client cannot
/// spoof it (doctrine guard #6, made structural).
#[derive(Clone, Debug)]
pub struct ServiceIdentity(pub String);

impl RequestCtx {
    /// Build the context from inbound headers, taking `service_identity` from the
    /// token layer's [`ServiceIdentity`] (or `"default"` when the perimeter
    /// middleware didn't run — e.g. in-process tests). Mirrors
    /// `RequestIdMiddleware.__call__`.
    pub fn from_headers(headers: &HeaderMap, service_identity: Option<String>) -> Self {
        let get = |name: &str| {
            headers
                .get(name)
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned)
        };
        // `x-request-id or uuid4()` — Python's `or` also replaces an empty string.
        let request_id = get(HDR_REQUEST_ID)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(new_request_id);
        RequestCtx {
            request_id,
            actor: get(HDR_ACTOR).unwrap_or_else(|| "system".to_string()),
            channel: get(HDR_CHANNEL).unwrap_or_else(|| "system".to_string()),
            tenant: get(HDR_TENANT).unwrap_or_else(|| "DEFAULT".to_string()),
            service_identity: service_identity.unwrap_or_else(|| "default".to_string()),
            ..Default::default()
        }
    }

    /// `"*" in permissions or permission in permissions` — mirrors
    /// `auth_context.has_permission`.
    pub fn has_permission(&self, permission: &str) -> bool {
        self.permissions.iter().any(|p| p == "*" || p == permission)
    }

    /// The three propagation headers for an outbound S2S call, mirroring
    /// `bss_clients.base._request`: actor, channel, and a request id (freshly
    /// generated when the current context has none).
    pub fn outbound_headers(&self) -> [(&'static str, String); 3] {
        let request_id = if self.request_id.is_empty() {
            new_request_id()
        } else {
            self.request_id.clone()
        };
        [
            (OUT_ACTOR, self.actor.clone()),
            (OUT_CHANNEL, self.channel.clone()),
            (OUT_REQUEST_ID, request_id),
        ]
    }
}

/// A fresh request id (UUID v4 string), matching Python's `str(uuid.uuid4())`.
pub fn new_request_id() -> String {
    uuid::Uuid::new_v4().to_string()
}
