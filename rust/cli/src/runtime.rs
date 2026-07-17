//! Shared CLI runtime — the client bundle, the CLI request context, and the
//! policy-aware command runner. Port of `bss_cli._runtime` + the orchestrator's
//! `get_clients()` (default-token bundle) as the CLI consumes it.
//!
//! Every command runs its work inside [`run_safely`], which installs the CLI
//! context ([`bss_context::scope`] with `channel="cli"`) so every outbound
//! `bss-clients` call carries `X-BSS-Channel: cli` / `X-BSS-Actor: cli-user`, and
//! maps a `PolicyViolation` to the same red banner + exit-2 the Python `_run_safely`
//! produces. Other client errors exit 1.

use std::future::Future;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::{
    CatalogClient, ClientError, ComClient, CrmClient, InventoryClient, MediationClient,
    PaymentClient, ProvisioningClient, SomClient, SubscriptionClient, TokenAuthProvider,
};
use bss_context::{new_request_id, RequestCtx};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// The direct CLI's downstream client bundle. Mirrors the orchestrator's
/// `get_clients()`: every client carries `X-BSS-API-Token` via a
/// [`TokenAuthProvider`] over the shared `BSS_API_TOKEN` (the CLI is `default`
/// identity, not a named surface).
///
/// The whole bundle is built once per command; individual command groups reach for
/// their own client(s). Fields are `allow(dead_code)` while groups land slice by
/// slice — every one is consumed once its command group is ported.
#[allow(dead_code)]
pub struct Clients {
    pub catalog: CatalogClient,
    pub crm: CrmClient,
    pub inventory: InventoryClient,
    pub payment: PaymentClient,
    pub com: ComClient,
    pub som: SomClient,
    pub subscription: SubscriptionClient,
    pub mediation: MediationClient,
    pub provisioning: ProvisioningClient,
}

impl Clients {
    /// Build the bundle from env. Errors if `BSS_API_TOKEN` is unset or a base URL
    /// is malformed.
    pub fn from_env() -> Result<Self, String> {
        let token = env_or("BSS_API_TOKEN", "");
        let auth = Arc::new(TokenAuthProvider::new(token).map_err(|e| e.to_string())?);
        let mk = |e: ClientError| e.to_string();
        // Inventory lives inside CRM (same base URL), matching get_clients().
        let crm_url = env_or("BSS_CRM_URL", "http://crm:8000");
        Ok(Self {
            catalog: CatalogClient::new(
                env_or("BSS_CATALOG_URL", "http://catalog:8000"),
                auth.clone(),
            )
            .map_err(mk)?,
            crm: CrmClient::new(crm_url.clone(), auth.clone()).map_err(mk)?,
            inventory: InventoryClient::new(crm_url, auth.clone()).map_err(mk)?,
            payment: PaymentClient::new(
                env_or("BSS_PAYMENT_URL", "http://payment:8000"),
                auth.clone(),
            )
            .map_err(mk)?,
            com: ComClient::new(env_or("BSS_COM_URL", "http://com:8000"), auth.clone())
                .map_err(mk)?,
            som: SomClient::new(env_or("BSS_SOM_URL", "http://som:8000"), auth.clone())
                .map_err(mk)?,
            subscription: SubscriptionClient::new(
                env_or("BSS_SUBSCRIPTION_URL", "http://subscription:8000"),
                auth.clone(),
            )
            .map_err(mk)?,
            mediation: MediationClient::new(
                env_or("BSS_MEDIATION_URL", "http://mediation:8000"),
                auth.clone(),
            )
            .map_err(mk)?,
            provisioning: ProvisioningClient::new(
                env_or("BSS_PROVISIONING_URL", "http://provisioning:8000"),
                auth.clone(),
            )
            .map_err(mk)?,
        })
    }
}

/// The CLI request context: `actor="cli-user"`, `channel="cli"`, a fresh
/// request id. Mirrors `use_cli_context()`.
fn cli_ctx() -> RequestCtx {
    RequestCtx {
        request_id: new_request_id(),
        actor: "cli-user".to_string(),
        channel: "cli".to_string(),
        ..Default::default()
    }
}

/// Run a command body inside the CLI context, mapping errors to exit codes and
/// operator-facing banners.
///
/// The body receives the freshly-built [`Clients`] bundle. A `PolicyViolation`
/// prints `POLICY_VIOLATION <rule>  <message>` and exits 2 (Python's
/// `_run_safely`); any other client error prints a red error and exits 1; a bundle
/// that can't be built (missing token / bad URL) also exits 1.
pub async fn run_safely<F, Fut>(body: F) -> ExitCode
where
    F: FnOnce(Arc<Clients>) -> Fut,
    Fut: Future<Output = Result<(), ClientError>>,
{
    run_safely_code(move |c| async move { body(c).await.map(|()| ExitCode::SUCCESS) }).await
}

/// Like [`run_safely`] but the body returns its own [`ExitCode`] — for commands
/// that exit non-zero on a *non-error* condition (e.g. `bss prov fault` when no
/// matching injector exists). The `PolicyViolation` / other-error mapping is
/// identical.
pub async fn run_safely_code<F, Fut>(body: F) -> ExitCode
where
    F: FnOnce(Arc<Clients>) -> Fut,
    Fut: Future<Output = Result<ExitCode, ClientError>>,
{
    let clients = match Clients::from_env() {
        Ok(c) => Arc::new(c),
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };
    match bss_context::scope(cli_ctx(), body(clients)).await {
        Ok(code) => code,
        Err(ClientError::Policy(p)) => {
            // Matches Python: `[red]POLICY_VIOLATION[/] [bold]{rule}[/]  {detail}`.
            eprintln!("POLICY_VIOLATION {}  {}", p.rule, p.message);
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("error ({}): {e}", e.status_code());
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_ctx_marks_channel_and_actor() {
        let ctx = cli_ctx();
        assert_eq!(ctx.channel, "cli");
        assert_eq!(ctx.actor, "cli-user");
        assert!(!ctx.request_id.is_empty());
        // Defaults for the rest match the Python AuthContext.
        assert_eq!(ctx.tenant, "DEFAULT");
    }
}
