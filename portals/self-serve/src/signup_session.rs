//! In-memory signup session store. Port of `bss_self_serve.session`.
//!
//! A signup spans many requests (the POST that runs `customer.create`, the
//! progress GET, and the HTMX-triggered step routes). They share the form input
//! plus the per-step results (CUST-id, payment method id, order id, subscription
//! id, activation code). Production would use Redis; the demo invariant is one
//! process, one map, TTL-bounded — a mid-signup restart loses the session, which
//! is acceptable (V0_4_0.md §3).
//!
//! `card_pan` is held in memory only for the short window between form submit and
//! `payment.add_card`; it is `#[serde(skip)]` so it never reaches a template, and
//! cleared the moment tokenisation succeeds. Templates see only `card_pan_last4`.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use uuid::Uuid;

/// Explicit step states for the direct-write chain. Serialises to the exact
/// Python `Literal` strings the templates compare against.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SignupStep {
    /// before POST /signup runs customer.create
    PendingCustomer,
    /// CUST-id known; next call is customer.attest_kyc
    PendingKyc,
    /// Didit hosted UI active; polling for the corroborating webhook
    PendingKycHandoff,
    /// KYC done; next call is payment.add_card (mock auto-tokenize OR Stripe)
    PendingCof,
    /// Stripe.js + Elements iframe mounted, waiting for card entry
    PendingCofElements,
    /// COF added; next call is com.create_order + submit
    PendingOrder,
    /// order placed; polling com.get_order until completed
    PendingActivation,
    /// subscription active; activation code known
    Completed,
    /// any step raised a structured error; see step_error
    Failed,
}

/// All state for one in-flight signup. `Serialize` so it can be handed to a
/// template as `signup`.
#[derive(Debug, Clone, Serialize)]
pub struct SignupSession {
    pub session_id: String,
    pub plan: String,
    pub name: String,
    pub email: String,
    pub phone: String,
    /// chosen on the picker page; passed to order.create as msisdn_preference
    pub msisdn: String,
    /// in-memory only, cleared once the chain finishes — never serialised
    #[serde(skip)]
    pub card_pan: String,
    pub card_pan_last4: String,
    /// optional typed promo code carried to com.create_order(discount_code=)
    pub promo_code: String,
    /// customer unticked their auto-applied assigned offer in the funnel
    pub skip_assigned_offer: bool,
    /// portal-auth identity that owns this signup
    pub identity_id: Option<String>,
    #[serde(skip)]
    created_at: Instant,

    pub step: SignupStep,
    /// PolicyViolation rule string, when step == Failed
    pub step_error: Option<String>,
    /// armed on first poll-detected completion; the next poll emits HX-Redirect
    pub redirect_armed: bool,

    // Populated as each step completes:
    pub customer_id: Option<String>,
    pub payment_method_id: Option<String>,
    pub order_id: Option<String>,
    pub subscription_id: Option<String>,
    pub activation_code: Option<String>,
    pub error: Option<String>,
    pub done: bool,

    // Didit cross-device KYC handoff (populated by POST /signup/step/kyc):
    pub kyc_provider_session_id: Option<String>,
    pub kyc_verify_url: Option<String>,
    pub kyc_verify_qr: Option<String>,
}

impl SignupSession {
    fn new(session_id: String, args: CreateArgs) -> Self {
        let card_pan = args.card_pan;
        let last4 = if card_pan.len() >= 4 {
            card_pan[card_pan.len() - 4..].to_string()
        } else {
            card_pan.clone()
        };
        SignupSession {
            session_id,
            plan: args.plan,
            name: args.name,
            email: args.email,
            phone: args.phone,
            msisdn: args.msisdn,
            card_pan,
            card_pan_last4: last4,
            promo_code: args.promo_code.trim().to_string(),
            skip_assigned_offer: args.skip_assigned_offer,
            identity_id: args.identity_id,
            created_at: Instant::now(),
            step: SignupStep::PendingCustomer,
            step_error: None,
            redirect_armed: false,
            customer_id: None,
            payment_method_id: None,
            order_id: None,
            subscription_id: None,
            activation_code: None,
            error: None,
            done: false,
            kyc_provider_session_id: None,
            kyc_verify_url: None,
            kyc_verify_qr: None,
        }
    }
}

/// Arguments for [`SessionStore::create`] — mirrors the Python keyword args.
pub struct CreateArgs {
    pub plan: String,
    pub name: String,
    pub email: String,
    pub phone: String,
    pub msisdn: String,
    pub card_pan: String,
    pub identity_id: Option<String>,
    pub promo_code: String,
    pub skip_assigned_offer: bool,
}

/// TTL-bounded in-memory map of [`SignupSession`] keyed by `session_id`.
pub struct SessionStore {
    ttl: Duration,
    items: Mutex<HashMap<String, SignupSession>>,
}

impl SessionStore {
    pub fn new(ttl_seconds: u64) -> Self {
        SessionStore {
            ttl: Duration::from_secs(ttl_seconds),
            items: Mutex::new(HashMap::new()),
        }
    }

    /// Create + insert a fresh session, returning a clone.
    pub fn create(&self, args: CreateArgs) -> SignupSession {
        let session_id = Uuid::new_v4().simple().to_string();
        let session = SignupSession::new(session_id.clone(), args);
        // Recover the guard even if a prior panic poisoned the lock — the map is
        // just a cache, so a partially-updated entry is harmless.
        let mut items = self.items.lock().unwrap_or_else(|e| e.into_inner());
        self.prune_locked(&mut items);
        items.insert(session_id, session.clone());
        session
    }

    /// Fetch a clone of the session by id (after pruning expired entries).
    pub fn get(&self, session_id: &str) -> Option<SignupSession> {
        // Recover the guard even if a prior panic poisoned the lock — the map is
        // just a cache, so a partially-updated entry is harmless.
        let mut items = self.items.lock().unwrap_or_else(|e| e.into_inner());
        self.prune_locked(&mut items);
        items.get(session_id).cloned()
    }

    /// Overwrite the stored session with `session` (upsert by id).
    pub fn update(&self, session: &SignupSession) {
        // Recover the guard even if a prior panic poisoned the lock — the map is
        // just a cache, so a partially-updated entry is harmless.
        let mut items = self.items.lock().unwrap_or_else(|e| e.into_inner());
        items.insert(session.session_id.clone(), session.clone());
    }

    fn prune_locked(&self, items: &mut HashMap<String, SignupSession>) {
        let ttl = self.ttl;
        items.retain(|_, s| s.created_at.elapsed() < ttl);
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn args() -> CreateArgs {
        CreateArgs {
            plan: "PLAN_M".into(),
            name: "Ada Lovelace".into(),
            email: "ada@example.sg".into(),
            phone: "80000000".into(),
            msisdn: "80001111".into(),
            card_pan: "4111111111111111".into(),
            identity_id: Some("ID-1".into()),
            promo_code: "  SAVE10 ".into(),
            skip_assigned_offer: false,
        }
    }

    #[test]
    fn create_get_roundtrip_and_last4() {
        let store = SessionStore::new(300);
        let sig = store.create(args());
        assert_eq!(sig.card_pan_last4, "1111");
        assert_eq!(sig.promo_code, "SAVE10"); // trimmed
        assert_eq!(sig.step, SignupStep::PendingCustomer);
        let got = store.get(&sig.session_id).unwrap();
        assert_eq!(got.session_id, sig.session_id);
        assert_eq!(got.email, "ada@example.sg");
    }

    #[test]
    fn update_advances_step() {
        let store = SessionStore::new(300);
        let mut sig = store.create(args());
        sig.step = SignupStep::PendingKyc;
        sig.customer_id = Some("CUST-001".into());
        store.update(&sig);
        let got = store.get(&sig.session_id).unwrap();
        assert_eq!(got.step, SignupStep::PendingKyc);
        assert_eq!(got.customer_id.as_deref(), Some("CUST-001"));
    }

    #[test]
    fn step_serialises_to_python_literal() {
        assert_eq!(
            serde_json::to_string(&SignupStep::PendingKycHandoff).unwrap(),
            "\"pending_kyc_handoff\""
        );
        assert_eq!(
            serde_json::to_string(&SignupStep::PendingCof).unwrap(),
            "\"pending_cof\""
        );
    }

    #[test]
    fn expired_entries_pruned() {
        let store = SessionStore::new(0); // everything immediately expired
        let sig = store.create(args());
        std::thread::sleep(Duration::from_millis(2));
        assert!(store.get(&sig.session_id).is_none());
    }
}
