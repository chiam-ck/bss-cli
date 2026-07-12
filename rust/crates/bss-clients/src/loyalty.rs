//! `LoyaltyClient` — bss-clients adapter for the external loyalty-cli (v1.1).
//!
//! Port of `bss_clients.loyalty.LoyaltyClient`. loyalty-cli is the entitlement
//! engine behind BSS promotions; it ships **unmodified** and BSS composes over
//! its `POST /v1/tools/<name>` surface. Only catalog and COM construct one — the
//! bearer token never leaves a BSS process.
//!
//! Unlike the sibling BSS clients this one does **not** send the BSS perimeter
//! headers (`X-BSS-API-Token` / `X-BSS-Actor` / `X-BSS-Channel`): loyalty has its
//! own auth + actor model (`Authorization: Bearer …` + `X-Actor-Id` /
//! `X-Actor-Roles` / `Idempotency-Key`). It also understands loyalty's refusal
//! envelope — HTTP 422 `{"detail": {"refused": true, "code", "detail"}}` maps to
//! [`ClientError::Policy`] so callers branch on the same type as a native BSS
//! policy violation. So it wraps its own reqwest client rather than [`BssClient`].

use std::sync::Arc;
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use serde_json::{json, Value};

use crate::auth::AuthProvider;
use crate::base::DEFAULT_TIMEOUT;
use crate::errors::ClientError;
use bss_db::PolicyViolation;

// loyalty enum values, surfaced as constants so BSS callers don't hardcode
// strings loyalty would 422 on (mirrors the Python module constants).
pub const REVOKE_ORDER_CANCELLED: &str = "order_cancelled";
pub const REVOKE_OPERATOR_ACTION: &str = "operator_action";
pub const REVOKE_CUSTOMER_CHANGED_MIND: &str = "customer_changed_mind";

pub const PROMO_KIND_SINGLE_USE_SHARED: &str = "single_use_shared";
pub const PROMO_KIND_MULTI_USE: &str = "multi_use";
pub const PROMO_KIND_SINGLE_USE_UNIQUE: &str = "single_use_unique_per_customer";

pub const OFFER_DEF_KIND_REGULAR: &str = "regular";

const DEFAULT_ACTOR_ROLES: &str = "author,reviewer,publisher";

/// Client for the loyalty-cli HTTP tool surface (external; v1.1). Cheap to clone.
#[derive(Clone)]
pub struct LoyaltyClient {
    base_url: String,
    http: reqwest::Client,
    auth: Arc<dyn AuthProvider>,
    timeout: Duration,
    actor_roles: String,
}

impl LoyaltyClient {
    /// Build a loyalty client for `base_url` with `auth` (a bearer provider) and
    /// the default 5s timeout.
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Self::with_timeout(base_url, auth, DEFAULT_TIMEOUT)
    }

    /// As [`LoyaltyClient::new`] with an explicit per-request timeout.
    pub fn with_timeout(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        let http = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(|e| ClientError::Transport(e.to_string()))?;
        Ok(LoyaltyClient {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
            auth,
            timeout,
            actor_roles: DEFAULT_ACTOR_ROLES.to_string(),
        })
    }

    /// `POST /v1/tools/<tool_name>` with `args` as the JSON body.
    ///
    /// `idempotency_key` is mandatory on loyalty's side; `None` (reads) mints a
    /// uuid4 so the call still satisfies the contract. Write callers pass a stable
    /// key. `X-Actor-Id` defaults to the current propagated actor.
    async fn call(
        &self,
        tool_name: &str,
        args: Value,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ClientError> {
        let actor = bss_context::current().actor;
        let idem = idempotency_key
            .map(str::to_string)
            .unwrap_or_else(bss_context::new_request_id);

        let mut headers = HeaderMap::new();
        insert(&mut headers, "X-Actor-Id", &actor);
        insert(&mut headers, "X-Actor-Roles", &self.actor_roles);
        insert(&mut headers, "Idempotency-Key", &idem);
        for (name, value) in self.auth.headers() {
            insert(&mut headers, &name, &value);
        }

        let url = format!("{}/v1/tools/{tool_name}", self.base_url);
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .json(&args)
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    ClientError::Timeout(format!("POST /v1/tools/{tool_name} timed out"))
                } else {
                    ClientError::Transport(e.to_string())
                }
            })?;
        handle_loyalty_response(resp).await
    }

    /// `GET /healthz` — for caller lifespan readiness. `Ok(true)` iff 200.
    pub async fn healthz(&self) -> Result<bool, ClientError> {
        let url = format!("{}/healthz", self.base_url);
        let resp = self
            .http
            .get(url)
            .timeout(self.timeout)
            .send()
            .await
            .map_err(|e| {
                if e.is_timeout() {
                    ClientError::Timeout("GET /healthz timed out".to_string())
                } else {
                    ClientError::Transport(e.to_string())
                }
            })?;
        Ok(resp.status().as_u16() == 200)
    }

    // ── offer definitions + promo codes (catalog: create saga) ──────────

    /// `customer.register` — mirror a BSS customer into loyalty's registry so its
    /// customer-facing views recognise the id. Idempotent on the customer id
    /// (`cust:{id}`). crm calls this best-effort after create; a failure never
    /// fails customer creation.
    pub async fn register_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        self.call(
            "customer.register",
            json!({ "id": customer_id, "active": true }),
            Some(&format!("cust:{customer_id}")),
        )
        .await
    }

    /// `offer_definition.register` — the OD that promo codes/offers hang off.
    pub async fn register_offer_definition(
        &self,
        definition_id: &str,
        display_name: &str,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer_definition.register",
            json!({
                "id": definition_id,
                "display_name": display_name,
                "kind": OFFER_DEF_KIND_REGULAR,
            }),
            Some(idempotency_key),
        )
        .await
    }

    /// `promo_code.register` — bind a typed code to an OD.
    pub async fn register_promo_code(
        &self,
        code: &str,
        offer_definition_id: &str,
        kind: &str,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "promo_code.register",
            json!({ "code": code, "offer_definition_id": offer_definition_id, "kind": kind }),
            Some(idempotency_key),
        )
        .await
    }

    /// `promo_code.show` — read OD id / state / binding for a code. No consume.
    pub async fn show_promo_code(&self, code: &str) -> Result<Value, ClientError> {
        self.call("promo_code.show", json!({ "code": code }), None)
            .await
    }

    /// `offer.issue` — targeted assignment: leave an `issued` offer on a customer.
    pub async fn issue_offer(
        &self,
        offer_id: &str,
        offer_definition_id: &str,
        customer_id: &str,
        source: Value,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer.issue",
            json!({
                "offer_id": offer_id,
                "offer_definition_id": offer_definition_id,
                "customer_id": customer_id,
                "source": source,
            }),
            Some(idempotency_key),
        )
        .await
    }

    /// `offer.list` — entitlement reads (preview / dashboard). Returns
    /// `{"rows": [...], "limit", "offset", "has_more"}`. Only the args COM/catalog
    /// pass are modelled (`customer_id` + `limit`).
    pub async fn list_offers(
        &self,
        customer_id: &str,
        limit: Option<i64>,
    ) -> Result<Value, ClientError> {
        let mut args = json!({ "customer_id": customer_id });
        if let Some(l) = limit {
            args["limit"] = json!(l);
        }
        self.call("offer.list", args, None).await
    }

    // ── consume lifecycle (COM) ─────────────────────────────────────────

    /// `offer.claim` — consume a non-targeted code at activation (the gate).
    /// `source` = `{"type": "promo_code", "code": <code>}`; key = order id.
    pub async fn claim_offer(
        &self,
        customer_id: &str,
        source: Value,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer.claim",
            json!({ "customer_id": customer_id, "source": source }),
            Some(idempotency_key),
        )
        .await
    }

    /// `offer.advance_to_claimed` — move a targeted `issued` offer to `claimed`.
    pub async fn advance_offer_to_claimed(
        &self,
        offer_id: &str,
        idempotency_key: &str,
        order_ref: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut args = json!({ "offer_id": offer_id });
        if let Some(r) = order_ref {
            args["order_ref"] = json!(r);
        }
        self.call("offer.advance_to_claimed", args, Some(idempotency_key))
            .await
    }

    /// `offer.redeem` — finalize on activation success.
    pub async fn redeem_offer(
        &self,
        offer_id: &str,
        order_ref: &str,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer.redeem",
            json!({ "offer_id": offer_id, "order_ref": order_ref }),
            Some(idempotency_key),
        )
        .await
    }

    /// `offer.expire` — terminal transition for an `issued` offer never claimed
    /// (FSM: `issued → expired`).
    pub async fn expire_offer(
        &self,
        offer_id: &str,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer.expire",
            json!({ "offer_id": offer_id }),
            Some(idempotency_key),
        )
        .await
    }

    /// `offer.revoke` — release the entitlement when the order fails/cancels.
    /// `reason` must be a loyalty `RevokeReason` (use `REVOKE_*`).
    pub async fn revoke_offer(
        &self,
        offer_id: &str,
        reason: &str,
        idempotency_key: &str,
    ) -> Result<Value, ClientError> {
        self.call(
            "offer.revoke",
            json!({ "offer_id": offer_id, "reason": reason }),
            Some(idempotency_key),
        )
        .await
    }
}

fn insert(headers: &mut HeaderMap, name: &str, value: &str) {
    if let (Ok(n), Ok(v)) = (
        HeaderName::from_bytes(name.as_bytes()),
        HeaderValue::from_str(value),
    ) {
        headers.insert(n, v);
    }
}

/// Loyalty's status handling, mirroring `_handle_loyalty_response`:
/// 404 → NotFound; refused-422 → Policy; other 422 → Http{422}; 5xx → Server.
async fn handle_loyalty_response(resp: reqwest::Response) -> Result<Value, ClientError> {
    let status = resp.status().as_u16();
    if status == 404 {
        return Err(ClientError::NotFound(text_of(resp).await));
    }
    if status == 422 {
        let body = text_of(resp).await;
        if let Ok(v) = serde_json::from_str::<Value>(&body) {
            // FastAPI wraps the HTTPException detail under "detail".
            let detail = v.get("detail").unwrap_or(&v);
            if detail.get("refused").and_then(Value::as_bool) == Some(true) {
                let rule = detail
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("loyalty.refused");
                let message = detail
                    .get("detail")
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| body.clone());
                return Err(ClientError::Policy(PolicyViolation::with_context(
                    rule,
                    message,
                    json!({ "source": "loyalty", "refused": true }),
                )));
            }
        }
        return Err(ClientError::Http {
            status: 422,
            detail: body,
        });
    }
    if status >= 500 {
        return Err(ClientError::Server {
            status,
            detail: text_of(resp).await,
        });
    }
    if status >= 400 {
        return Err(ClientError::Http {
            status,
            detail: text_of(resp).await,
        });
    }
    let body = text_of(resp).await;
    serde_json::from_str(&body).map_err(|e| ClientError::Transport(e.to_string()))
}

async fn text_of(resp: reqwest::Response) -> String {
    resp.text().await.unwrap_or_default()
}
