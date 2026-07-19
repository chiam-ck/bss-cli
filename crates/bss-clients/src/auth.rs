//! Pluggable outbound auth — port of `bss_clients.auth`.
//!
//! Every client takes an `Arc<dyn AuthProvider>` and applies its headers to
//! every request. All built-ins are synchronous (they cache a fixed header set);
//! a future OAuth2 provider (Phase 12, retired) would be the first async one.

/// Returns the auth headers to inject on every outgoing request.
pub trait AuthProvider: Send + Sync {
    fn headers(&self) -> Vec<(String, String)>;
}

/// Construction-time failure (empty token / unset env). Fail-fast, like Python.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthError(pub String);

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for AuthError {}

/// No headers — tests and pre-v0.3 paths.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoAuthProvider;

impl AuthProvider for NoAuthProvider {
    fn headers(&self) -> Vec<(String, String)> {
        Vec::new()
    }
}

/// Injects `X-BSS-API-Token` (the shared internal perimeter token). Empty token
/// is rejected — a valid client cannot exist without one.
#[derive(Debug, Clone)]
pub struct TokenAuthProvider {
    token: String,
}

impl TokenAuthProvider {
    pub fn new(token: impl Into<String>) -> Result<Self, AuthError> {
        let token = token.into();
        if token.is_empty() {
            return Err(AuthError(
                "TokenAuthProvider requires a non-empty token".to_string(),
            ));
        }
        Ok(TokenAuthProvider { token })
    }
}

impl AuthProvider for TokenAuthProvider {
    fn headers(&self) -> Vec<(String, String)> {
        vec![("X-BSS-API-Token".to_string(), self.token.clone())]
    }
}

/// Injects `Authorization: Bearer <token>` — for external services (loyalty-cli)
/// that expect a bearer token rather than `X-BSS-API-Token`.
#[derive(Debug, Clone)]
pub struct BearerAuthProvider {
    value: String,
}

impl BearerAuthProvider {
    pub fn new(token: impl Into<String>) -> Result<Self, AuthError> {
        let token = token.into();
        if token.is_empty() {
            return Err(AuthError(
                "BearerAuthProvider requires a non-empty token".to_string(),
            ));
        }
        Ok(BearerAuthProvider {
            value: format!("Bearer {token}"),
        })
    }
}

impl AuthProvider for BearerAuthProvider {
    fn headers(&self) -> Vec<(String, String)> {
        vec![("Authorization".to_string(), self.value.clone())]
    }
}

/// Outbound provider for an external-facing surface (self-serve portal, partners).
/// Loads its token from `env_var` (or `fallback_env_var`) once at construction.
/// `identity` is an informational label for caller-side logs only — it is **not**
/// sent as a header; the receiver derives the authoritative `service_identity`
/// from token validation.
#[derive(Debug, Clone)]
pub struct NamedTokenAuthProvider {
    identity: String,
    source_env: String,
    token: String,
}

impl NamedTokenAuthProvider {
    /// Read the token from process env, mirroring the Python constructor
    /// (primary → fallback → fail-fast).
    pub fn from_env(
        identity: impl Into<String>,
        env_var: &str,
        fallback_env_var: Option<&str>,
    ) -> Result<Self, AuthError> {
        let identity = identity.into();
        if identity.is_empty() {
            return Err(AuthError(
                "NamedTokenAuthProvider requires a non-empty identity".to_string(),
            ));
        }
        if env_var.is_empty() {
            return Err(AuthError(
                "NamedTokenAuthProvider requires an env_var name".to_string(),
            ));
        }
        let primary = std::env::var(env_var).unwrap_or_default();
        if !primary.is_empty() {
            return Ok(NamedTokenAuthProvider {
                identity,
                source_env: env_var.to_string(),
                token: primary,
            });
        }
        if let Some(fb) = fallback_env_var {
            let fb_val = std::env::var(fb).unwrap_or_default();
            if !fb_val.is_empty() {
                // (Python logs a one-time fallback warning here — lands with
                // bss-telemetry.)
                return Ok(NamedTokenAuthProvider {
                    identity,
                    source_env: fb.to_string(),
                    token: fb_val,
                });
            }
        }
        Err(AuthError(format!(
            "NamedTokenAuthProvider({identity:?}): {env_var} is unset{}. \
             Generate via: openssl rand -hex 32",
            fallback_env_var
                .map(|fb| format!(" and fallback {fb} is also unset"))
                .unwrap_or_default()
        )))
    }

    /// The informational identity label (caller-side logs only).
    pub fn identity(&self) -> &str {
        &self.identity
    }

    /// The env var the token was actually loaded from (primary or fallback).
    pub fn source_env(&self) -> &str {
        &self.source_env
    }
}

impl AuthProvider for NamedTokenAuthProvider {
    fn headers(&self) -> Vec<(String, String)> {
        vec![("X-BSS-API-Token".to_string(), self.token.clone())]
    }
}
