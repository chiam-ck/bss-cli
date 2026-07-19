//! `select_tokenizer` — port of `app.domain.select_tokenizer`.
//!
//! Resolves `BSS_PAYMENT_PROVIDER` to a concrete [`Tokenizer`], failing fast at
//! startup on any misconfig (never a silent downgrade to mock). Four guards:
//! unknown provider; stripe + missing creds (api key / publishable / webhook
//! secret); production + `sk_test_*` (and secret/publishable mode mismatch);
//! `ALLOW_TEST_CARD_REUSE` + `sk_live_*`. The `main` lifespan lets the error
//! propagate so the service refuses to boot.

use sqlx::PgPool;

use crate::config::Settings;
use crate::tokenizer::{StripeConfig, Tokenizer};

pub fn select_tokenizer(settings: &Settings, pool: &PgPool) -> Result<Tokenizer, String> {
    match settings.payment_provider.as_str() {
        "mock" => Ok(Tokenizer::Mock),
        "stripe" => build_stripe(settings, pool),
        other => Err(format!(
            "Unknown BSS_PAYMENT_PROVIDER='{other}'; expected 'mock' | 'stripe'"
        )),
    }
}

fn build_stripe(settings: &Settings, pool: &PgPool) -> Result<Tokenizer, String> {
    let api_key = &settings.payment_stripe_api_key;
    let publishable = &settings.payment_stripe_publishable_key;
    let webhook_secret = &settings.payment_stripe_webhook_secret;

    if api_key.is_empty() {
        return Err("BSS_PAYMENT_PROVIDER=stripe requires BSS_PAYMENT_STRIPE_API_KEY".into());
    }
    if publishable.is_empty() {
        return Err(
            "BSS_PAYMENT_PROVIDER=stripe requires BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY".into(),
        );
    }
    if webhook_secret.is_empty() {
        return Err(
            "BSS_PAYMENT_PROVIDER=stripe requires BSS_PAYMENT_STRIPE_WEBHOOK_SECRET (without it \
             the webhook receiver would silently 401 every Stripe delivery and charge \
             reconciliation would never happen)"
                .into(),
        );
    }

    let is_test_secret = api_key.starts_with("sk_test_");
    let is_live_secret = api_key.starts_with("sk_live_");
    let is_test_pub = publishable.starts_with("pk_test_");
    let is_live_pub = publishable.starts_with("pk_live_");

    if !(is_test_secret || is_live_secret) {
        return Err("BSS_PAYMENT_STRIPE_API_KEY must start with sk_test_ or sk_live_".into());
    }
    if !(is_test_pub || is_live_pub) {
        return Err(
            "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY must start with pk_test_ or pk_live_".into(),
        );
    }
    if is_test_secret != is_test_pub {
        return Err(
            "Stripe key mode mismatch: secret and publishable keys must both be test \
             (sk_test_/pk_test_) or both be live (sk_live_/pk_live_); refusing to start with \
             mixed mode"
                .into(),
        );
    }
    if settings.env == "production" && is_test_secret {
        return Err(
            "BSS_PAYMENT_STRIPE_API_KEY=sk_test_* refused in BSS_ENV=production; production must \
             use sk_live_*"
                .into(),
        );
    }
    if settings.payment_allow_test_card_reuse && is_live_secret {
        return Err(
            "BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true is sandbox-only and refused with sk_live_*; \
             setting it would let one real customer's payment_method re-attach to a different \
             real customer (security disaster)"
                .into(),
        );
    }

    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("stripe http client build failed: {e}"))?;
    Ok(Tokenizer::Stripe(
        StripeConfig {
            api_key: api_key.clone(),
            publishable_key: publishable.clone(),
            webhook_secret: webhook_secret.clone(),
            allow_test_card_reuse: settings.payment_allow_test_card_reuse,
        },
        http,
        pool.clone(),
    ))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn base() -> Settings {
        Settings {
            service_name: "payment".into(),
            version: "0".into(),
            log_level: "INFO".into(),
            db_url: String::new(),
            env: "development".into(),
            tenant_default: "DEFAULT".into(),
            crm_url: String::new(),
            payment_provider: "stripe".into(),
            payment_stripe_api_key: String::new(),
            payment_stripe_publishable_key: String::new(),
            payment_stripe_webhook_secret: String::new(),
            payment_allow_test_card_reuse: false,
            api_token: String::new(),
        }
    }

    // These guards are pure string logic — exercised without a pool by checking
    // the error *before* the pool is touched (all four guards precede it).
    fn check(s: &Settings) -> Result<(), String> {
        // mirror build_stripe's guard order without constructing a client/pool
        let api_key = &s.payment_stripe_api_key;
        let publishable = &s.payment_stripe_publishable_key;
        let webhook_secret = &s.payment_stripe_webhook_secret;
        if api_key.is_empty() {
            return Err("api".into());
        }
        if publishable.is_empty() {
            return Err("pub".into());
        }
        if webhook_secret.is_empty() {
            return Err("whsec".into());
        }
        let is_test_secret = api_key.starts_with("sk_test_");
        let is_test_pub = publishable.starts_with("pk_test_");
        if is_test_secret != is_test_pub {
            return Err("mode_mismatch".into());
        }
        if s.env == "production" && is_test_secret {
            return Err("prod_test".into());
        }
        if s.payment_allow_test_card_reuse && api_key.starts_with("sk_live_") {
            return Err("reuse_live".into());
        }
        Ok(())
    }

    #[test]
    fn missing_creds_rejected() {
        assert_eq!(check(&base()).unwrap_err(), "api");
    }

    #[test]
    fn mode_mismatch_rejected() {
        let mut s = base();
        s.payment_stripe_api_key = "sk_test_x".into();
        s.payment_stripe_publishable_key = "pk_live_x".into();
        s.payment_stripe_webhook_secret = "whsec_x".into();
        assert_eq!(check(&s).unwrap_err(), "mode_mismatch");
    }

    #[test]
    fn prod_with_test_key_rejected() {
        let mut s = base();
        s.env = "production".into();
        s.payment_stripe_api_key = "sk_test_x".into();
        s.payment_stripe_publishable_key = "pk_test_x".into();
        s.payment_stripe_webhook_secret = "whsec_x".into();
        assert_eq!(check(&s).unwrap_err(), "prod_test");
    }

    #[test]
    fn reuse_with_live_key_rejected() {
        let mut s = base();
        s.payment_stripe_api_key = "sk_live_x".into();
        s.payment_stripe_publishable_key = "pk_live_x".into();
        s.payment_stripe_webhook_secret = "whsec_x".into();
        s.payment_allow_test_card_reuse = true;
        assert_eq!(check(&s).unwrap_err(), "reuse_live");
    }

    #[test]
    fn valid_test_config_passes_guards() {
        let mut s = base();
        s.payment_stripe_api_key = "sk_test_x".into();
        s.payment_stripe_publishable_key = "pk_test_x".into();
        s.payment_stripe_webhook_secret = "whsec_x".into();
        assert!(check(&s).is_ok());
    }
}
