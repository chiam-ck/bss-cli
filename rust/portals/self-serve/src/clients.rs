//! bss-clients factory for the self-serve portal. Port of `bss_self_serve.clients`.
//!
//! The portal has its own perimeter identity (`portal_self_serve`) via
//! `BSS_PORTAL_SELF_SERVE_API_TOKEN`, falling back to `BSS_API_TOKEN` during the
//! named-token rollout. Inventory lives inside CRM (same base URL).

use std::sync::Arc;

use bss_clients::{
    AuthProvider, CatalogClient, ComClient, CrmClient, InventoryClient, NamedTokenAuthProvider,
    PaymentClient, ProvisioningClient, SubscriptionClient,
};

use crate::config::Settings;

const PORTAL_IDENTITY: &str = "portal_self_serve";
const PORTAL_TOKEN_ENV: &str = "BSS_PORTAL_SELF_SERVE_API_TOKEN";
const FALLBACK_TOKEN_ENV: &str = "BSS_API_TOKEN";

/// Downstream clients the self-serve portal calls.
pub struct PortalClients {
    pub catalog: CatalogClient,
    pub crm: CrmClient,
    pub inventory: InventoryClient,
    pub com: ComClient,
    pub subscription: SubscriptionClient,
    pub payment: PaymentClient,
    pub provisioning: ProvisioningClient,
}

impl PortalClients {
    /// Build the bundle from env. Errors if neither the named token nor the
    /// fallback is set (the portal can't talk to the perimeter without one).
    pub fn from_env(settings: &Settings) -> Result<Self, String> {
        let auth: Arc<dyn AuthProvider> = Arc::new(
            NamedTokenAuthProvider::from_env(
                PORTAL_IDENTITY,
                PORTAL_TOKEN_ENV,
                Some(FALLBACK_TOKEN_ENV),
            )
            .map_err(|e| e.to_string())?,
        );
        let mk = |e: bss_clients::ClientError| e.to_string();
        Ok(Self {
            catalog: CatalogClient::new(settings.catalog_url.clone(), auth.clone()).map_err(mk)?,
            crm: CrmClient::new(settings.crm_url.clone(), auth.clone()).map_err(mk)?,
            // Inventory lives inside CRM (same base URL).
            inventory: InventoryClient::new(settings.crm_url.clone(), auth.clone()).map_err(mk)?,
            com: ComClient::new(settings.com_url.clone(), auth.clone()).map_err(mk)?,
            subscription: SubscriptionClient::new(settings.subscription_url.clone(), auth.clone())
                .map_err(mk)?,
            payment: PaymentClient::new(settings.payment_url.clone(), auth.clone()).map_err(mk)?,
            provisioning: ProvisioningClient::new(settings.provisioning_url.clone(), auth)
                .map_err(mk)?,
        })
    }
}
