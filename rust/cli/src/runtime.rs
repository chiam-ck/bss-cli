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
    AuditClient, AuthProvider, CatalogClient, ClientError, ComClient, CrmClient, InventoryClient,
    JaegerClient, MediationClient, PaymentClient, ProvisioningClient, SomClient,
    SubscriptionClient, TokenAuthProvider,
};
use bss_context::{new_request_id, RequestCtx};
use bss_orchestrator::{build_registry, RegistryClients, RegistryExtras, ToolRegistry};

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
    /// is malformed. Service URLs default to `http://localhost:800X` (the host-mapped
    /// compose ports) so `bss` works from a dev shell out of the box — matching
    /// Python's `bss_orchestrator.config` dev defaults, not the compose-network
    /// hostnames the services use to reach each other. Override via `BSS_<SVC>_URL`
    /// when running the CLI inside the container network.
    pub fn from_env() -> Result<Self, String> {
        let token = env_or("BSS_API_TOKEN", "");
        let auth = Arc::new(TokenAuthProvider::new(token).map_err(|e| e.to_string())?);
        let mk = |e: ClientError| e.to_string();
        // Inventory lives inside CRM (same base URL), matching get_clients().
        let crm_url = env_or("BSS_CRM_URL", "http://localhost:8002");
        Ok(Self {
            catalog: CatalogClient::new(
                env_or("BSS_CATALOG_URL", "http://localhost:8001"),
                auth.clone(),
            )
            .map_err(mk)?,
            crm: CrmClient::new(crm_url.clone(), auth.clone()).map_err(mk)?,
            inventory: InventoryClient::new(crm_url, auth.clone()).map_err(mk)?,
            payment: PaymentClient::new(
                env_or("BSS_PAYMENT_URL", "http://localhost:8003"),
                auth.clone(),
            )
            .map_err(mk)?,
            com: ComClient::new(env_or("BSS_COM_URL", "http://localhost:8004"), auth.clone())
                .map_err(mk)?,
            som: SomClient::new(env_or("BSS_SOM_URL", "http://localhost:8005"), auth.clone())
                .map_err(mk)?,
            subscription: SubscriptionClient::new(
                env_or("BSS_SUBSCRIPTION_URL", "http://localhost:8006"),
                auth.clone(),
            )
            .map_err(mk)?,
            mediation: MediationClient::new(
                env_or("BSS_MEDIATION_URL", "http://localhost:8007"),
                auth.clone(),
            )
            .map_err(mk)?,
            provisioning: ProvisioningClient::new(
                env_or("BSS_PROVISIONING_URL", "http://localhost:8010"),
                auth.clone(),
            )
            .map_err(mk)?,
        })
    }
}

/// Build the full LLM tool registry for `bss ask` / the REPL, over the shared
/// `bss_orchestrator::build_registry`. The nine service clients reuse
/// [`Clients::from_env`]; the observability + knowledge extras are built here
/// (Jaeger is unauthenticated, the audit surfaces reuse the default token, the
/// knowledge pool connects only when `BSS_KNOWLEDGE_ENABLED` and `BSS_DB_URL` are
/// both set — mirroring Python's `_maybe_register`).
pub(crate) async fn build_agent_registry() -> Result<ToolRegistry, String> {
    let c = Clients::from_env()?;
    let reg_clients = RegistryClients {
        catalog: c.catalog,
        crm: c.crm,
        inventory: c.inventory,
        payment: c.payment,
        com: c.com,
        som: c.som,
        subscription: c.subscription,
        mediation: c.mediation,
        provisioning: c.provisioning,
    };

    // Extras need the token + COM/subscription URLs again (the bundle consumed them).
    let auth: Arc<dyn AuthProvider> =
        Arc::new(TokenAuthProvider::new(env_or("BSS_API_TOKEN", "")).map_err(|e| e.to_string())?);
    let com_url = env_or("BSS_COM_URL", "http://localhost:8004");
    let sub_url = env_or("BSS_SUBSCRIPTION_URL", "http://localhost:8006");

    let knowledge_pool = if knowledge_enabled() {
        match env_or("BSS_DB_URL", "") {
            db_url if db_url.is_empty() => None,
            db_url => bss_db::connect(&db_url).await.ok(),
        }
    } else {
        None
    };

    let extras = RegistryExtras {
        jaeger: JaegerClient::from_env().ok(),
        audit_com: AuditClient::new(com_url, auth.clone()).ok(),
        audit_sub: AuditClient::new(sub_url, auth).ok(),
        knowledge_pool,
    };

    Ok(build_registry(&reg_clients, extras))
}

/// `BSS_KNOWLEDGE_ENABLED` truthiness — default true, enabled for `{1,true,yes,on}`
/// (lower-cased, trimmed). Port of Python's `_knowledge_enabled`.
fn knowledge_enabled() -> bool {
    let raw = env_or("BSS_KNOWLEDGE_ENABLED", "true")
        .trim()
        .to_lowercase();
    matches!(raw.as_str(), "1" | "true" | "yes" | "on")
}

/// The CLI request context: `actor="cli-user"`, `channel="cli"`, a fresh
/// request id. Mirrors `use_cli_context()`. `pub(crate)` so the `trace` group — which
/// builds its own ad-hoc Jaeger/Audit clients rather than the shared bundle — can scope
/// its audit reads under the same context.
pub(crate) fn cli_ctx() -> RequestCtx {
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

/// Like [`run_safely`] but additionally maps a `NotFound` to `NOT_FOUND  <detail>`
/// plus exit 2. The `bss promo` group is the one command family whose Python
/// `_run_safely` catches `NotFound`; every other group lets it propagate to exit 1.
pub async fn run_safely_promo<F, Fut>(body: F) -> ExitCode
where
    F: FnOnce(Arc<Clients>) -> Fut,
    Fut: Future<Output = Result<(), ClientError>>,
{
    let clients = match Clients::from_env() {
        Ok(c) => Arc::new(c),
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };
    match bss_context::scope(cli_ctx(), body(clients)).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(ClientError::Policy(p)) => {
            eprintln!("POLICY_VIOLATION {}  {}", p.rule, p.message);
            ExitCode::from(2)
        }
        Err(ClientError::NotFound(detail)) => {
            eprintln!("NOT_FOUND  {detail}");
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
