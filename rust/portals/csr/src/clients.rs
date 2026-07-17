//! The cockpit's typed `bss-clients` bundle. Port of `bss_csr.clients`.
//!
//! **v0.9 named token:** outbound calls carry the cockpit's own identity
//! (`operator_cockpit` ← `BSS_OPERATOR_COCKPIT_API_TOKEN`), so
//! `audit.domain_event.service_identity` distinguishes cockpit traffic from the
//! self-serve portal's. `BSS_API_TOKEN` is the documented fallback.
//!
//! Note the cockpit is **not** behind `BSSApiTokenMiddleware` on its *inbound*
//! HTTP — single-operator-by-design behind a secure perimeter (DECISIONS
//! 2026-05-01). This bundle is the *outbound* side only.

use std::sync::Arc;

use bss_clients::{
    AuthProvider, CatalogClient, ComClient, CrmClient, InventoryClient, MediationClient,
    NamedTokenAuthProvider, PaymentClient, ProvisioningClient, SomClient, SubscriptionClient,
};

use crate::config::Settings;

const COCKPIT_IDENTITY: &str = "operator_cockpit";
const COCKPIT_TOKEN_ENV: &str = "BSS_OPERATOR_COCKPIT_API_TOKEN";
const FALLBACK_TOKEN_ENV: &str = "BSS_API_TOKEN";

/// Every downstream client the CRM screens read through.
pub struct CockpitClients {
    pub catalog: CatalogClient,
    pub crm: CrmClient,
    pub inventory: InventoryClient,
    pub com: ComClient,
    pub som: SomClient,
    pub subscription: SubscriptionClient,
    pub payment: PaymentClient,
    pub provisioning: ProvisioningClient,
    pub mediation: MediationClient,
}

impl CockpitClients {
    /// Build the bundle from env. Errors if neither the named token nor the
    /// fallback is set.
    pub fn from_env(settings: &Settings) -> Result<Self, String> {
        let auth = cockpit_auth()?;
        let mk = |e: bss_clients::ClientError| e.to_string();
        Ok(Self {
            catalog: CatalogClient::new(settings.catalog_url.clone(), auth.clone()).map_err(mk)?,
            crm: CrmClient::new(settings.crm_url.clone(), auth.clone()).map_err(mk)?,
            // Inventory lives inside CRM (same base URL).
            inventory: InventoryClient::new(settings.crm_url.clone(), auth.clone()).map_err(mk)?,
            com: ComClient::new(settings.com_url.clone(), auth.clone()).map_err(mk)?,
            som: SomClient::new(settings.som_url.clone(), auth.clone()).map_err(mk)?,
            subscription: SubscriptionClient::new(settings.subscription_url.clone(), auth.clone())
                .map_err(mk)?,
            payment: PaymentClient::new(settings.payment_url.clone(), auth.clone()).map_err(mk)?,
            provisioning: ProvisioningClient::new(settings.provisioning_url.clone(), auth.clone())
                .map_err(mk)?,
            mediation: MediationClient::new(settings.mediation_url.clone(), auth.clone())
                .map_err(mk)?,
        })
    }
}

/// The cockpit's named-token auth provider (`operator_cockpit`).
pub fn cockpit_auth() -> Result<Arc<dyn AuthProvider>, String> {
    Ok(Arc::new(
        NamedTokenAuthProvider::from_env(
            COCKPIT_IDENTITY,
            COCKPIT_TOKEN_ENV,
            Some(FALLBACK_TOKEN_ENV),
        )
        .map_err(|e| e.to_string())?,
    ))
}
