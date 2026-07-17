//! `CrmClient` — typed client for the CRM service.
//!
//! Port of `bss_clients.crm.CRMClient`. Only the surface Phase 3 (com) needs is
//! ported: [`CrmClient::get_customer`] (order-create existence check). The rest
//! lands when CRM itself is ported (P4) or when a consumer first needs it.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the CRM service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct CrmClient {
    inner: BssClient,
}

/// Optional fields for [`CrmClient::attest_kyc_full`]. Each `None` takes the same
/// default as the Python `attest_kyc` keyword arg, so a `default()` call produces
/// the 3-arg stub body and a fully-populated one produces the reduced-PII body.
#[derive(Debug, Default, Clone)]
pub struct AttestKycOpts<'a> {
    pub provider_reference: Option<&'a str>,
    pub document_type: Option<&'a str>,
    /// Explicit document number; when `None` a per-customer stub is derived.
    pub document_number: Option<&'a str>,
    pub document_number_last4: Option<&'a str>,
    pub document_number_hash: Option<&'a str>,
    pub document_country: Option<&'a str>,
    pub date_of_birth: Option<&'a str>,
    pub nationality: Option<&'a str>,
    pub verified_at: Option<&'a str>,
    pub corroboration_id: Option<&'a str>,
}

impl CrmClient {
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Self::with_timeout(base_url, auth, DEFAULT_TIMEOUT)
    }

    pub fn with_timeout(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        Ok(CrmClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/customerManagement/v4/customer/{id}`. A 404 maps to
    /// [`ClientError::NotFound`] (com turns it into `order.create.customer_not_found`).
    pub async fn get_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer/by-msisdn/{msisdn}` — resolve
    /// MSISDN → subscription → customer in one hop. A 404 (number unassigned or
    /// owning customer deleted) maps to [`ClientError::NotFound`]. Backs
    /// `customer.find_by_msisdn`.
    pub async fn find_customer_by_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/by-msisdn/{}",
            encode(msisdn)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer/by-email?email={email}` — exact
    /// match on a live email contact medium (query param so `+` addressing survives
    /// encoding). A 404 maps to [`ClientError::NotFound`]. Backs
    /// `customer.find_by_email`.
    pub async fn find_customer_by_email(&self, email: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/by-email?email={}",
            encode(email)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer` with optional `status` /
    /// `name` filters (Python maps `state`→`status`, `name_contains`→`name`; each
    /// sent only when present). Returns a JSON array. Backs `customer.list`.
    ///
    /// Unpaged: `limit`/`offset` are omitted, so the server's own default page
    /// size applies — exactly what Python does when the kwargs are left unset.
    /// The cockpit's customer list needs paging; see [`Self::list_customers_paged`].
    pub async fn list_customers(
        &self,
        state: Option<&str>,
        name_contains: Option<&str>,
    ) -> Result<Value, ClientError> {
        self.list_customers_paged(state, name_contains, None, None)
            .await
    }

    /// [`Self::list_customers`] with explicit paging. Each of `limit`/`offset` is
    /// sent only when `Some`, mirroring Python's `if limit is not None` — passing
    /// `None` is *not* the same as passing the server default, since omitting
    /// `offset` and sending `offset=0` differ for any future server that treats
    /// the absent case specially.
    pub async fn list_customers_paged(
        &self,
        state: Option<&str>,
        name_contains: Option<&str>,
        limit: Option<i64>,
        offset: Option<i64>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("status={}", encode(s)));
        }
        if let Some(n) = name_contains.filter(|s| !s.is_empty()) {
            params.push(format!("name={}", encode(n)));
        }
        if let Some(l) = limit {
            params.push(format!("limit={l}"));
        }
        if let Some(o) = offset {
            params.push(format!("offset={o}"));
        }
        let mut path = "/tmf-api/customerManagement/v4/customer".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// The customer's active contact mediums (the `contactMedium` array off the
    /// TMF629 `get_customer` response). Backs the profile-contact view.
    pub async fn list_contact_mediums(&self, customer_id: &str) -> Result<Value, ClientError> {
        let cust = self.get_customer(customer_id).await?;
        Ok(cust
            .get("contactMedium")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())))
    }

    /// `PATCH …/customer/{id}/individual` — partial display-name update
    /// (`givenName`/`familyName` sent only when present). Backs
    /// `/profile/contact/name/update`.
    pub async fn update_individual(
        &self,
        customer_id: &str,
        given_name: Option<&str>,
        family_name: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut map = serde_json::Map::new();
        if let Some(g) = given_name {
            map.insert("givenName".to_string(), json!(g));
        }
        if let Some(f) = family_name {
            map.insert("familyName".to_string(), json!(f));
        }
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}/individual");
        let resp = self
            .inner
            .request(Method::PATCH, &path, Some(&Value::Object(map)), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `PATCH …/customer/{id}/contactMedium/{cm}` — phone/address value update.
    /// Email uses the cross-schema change flow. Backs the profile phone/address
    /// updates.
    pub async fn update_contact_medium(
        &self,
        customer_id: &str,
        medium_id: &str,
        value: &str,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium/{medium_id}"
        );
        let resp = self
            .inner
            .request(Method::PATCH, &path, Some(&json!({ "value": value })), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/customer/{id}/kyc-status`. Backs `customer.get_kyc_status`.
    pub async fn get_kyc_status(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/customer/{customer_id}/kyc-status");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/case` filtered by optional `customerId` / `state` /
    /// `assignedAgentId` (from `agent_id`; sent only when present). Returns a JSON
    /// array. Backs the `customer.get` 360 composite, `case.list`, and (later)
    /// `case.list_for_me`.
    pub async fn list_cases(
        &self,
        customer_id: Option<&str>,
        state: Option<&str>,
        agent_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            params.push(format!("customerId={}", encode(c)));
        }
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("state={}", encode(s)));
        }
        if let Some(a) = agent_id.filter(|s| !s.is_empty()) {
            params.push(format!("assignedAgentId={}", encode(a)));
        }
        let mut path = "/crm-api/v1/case".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/case/{id}` — a single case. Backs `case.get` +
    /// `case.show_transcript_for`.
    pub async fn get_case(&self, case_id: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/case/{case_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/chat-transcript/{hash}` — the stored chat transcript (CSR
    /// side). Backs `case.show_transcript_for`.
    pub async fn get_chat_transcript(&self, hash: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/chat-transcript/{hash}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/troubleTicket/v4/troubleTicket/{id}` — a single trouble ticket
    /// (TMF621). Backs `ticket.get`.
    pub async fn get_ticket(&self, ticket_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/troubleTicket/v4/troubleTicket` filtered by optional
    /// `customerId` / `caseId` / `state` / `agentId` (sent only when present).
    /// Returns a JSON array. Backs `ticket.list`.
    pub async fn list_tickets(
        &self,
        customer_id: Option<&str>,
        case_id: Option<&str>,
        state: Option<&str>,
        agent_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            params.push(format!("customerId={}", encode(c)));
        }
        if let Some(c) = case_id.filter(|s| !s.is_empty()) {
            params.push(format!("caseId={}", encode(c)));
        }
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("state={}", encode(s)));
        }
        if let Some(a) = agent_id.filter(|s| !s.is_empty()) {
            params.push(format!("agentId={}", encode(a)));
        }
        let mut path = "/tmf-api/troubleTicket/v4/troubleTicket".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/port-requests` — MNP port requests. `limit`/`offset` always
    /// sent first (Python seeds them), then optional `state` / `direction`. Returns
    /// a JSON array. Backs `port_request.list`.
    pub async fn list_port_requests(
        &self,
        state: Option<&str>,
        direction: Option<&str>,
        limit: i64,
        offset: i64,
    ) -> Result<Value, ClientError> {
        let mut params = vec![format!("limit={limit}"), format!("offset={offset}")];
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("state={}", encode(s)));
        }
        if let Some(d) = direction.filter(|s| !s.is_empty()) {
            params.push(format!("direction={}", encode(d)));
        }
        let path = format!("/crm-api/v1/port-requests?{}", params.join("&"));
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/port-requests/{id}` — a single port request. Backs
    /// `port_request.get`.
    pub async fn get_port_request(&self, port_id: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/port-requests/{port_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/agent` — CSR/support agents, optional `state` filter (sent
    /// only when present). Returns a JSON array. Backs `agents.list`.
    pub async fn list_agents(&self, state: Option<&str>) -> Result<Value, ClientError> {
        let mut path = "/crm-api/v1/agent".to_string();
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            path.push_str(&format!("?state={}", encode(s)));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerInteractionManagement/v1/interaction?customerId&limit`
    /// — a customer's interaction log, newest first. Backs `interaction.list` and
    /// the `customer.get` 360 composite.
    pub async fn list_interactions(
        &self,
        customer_id: &str,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerInteractionManagement/v1/interaction?customerId={}&limit={limit}",
            encode(customer_id)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    // ── writes ───────────────────────────────────────────────────────────

    /// `POST /tmf-api/customerManagement/v4/customer`. `name` is split on the first
    /// whitespace into `givenName` + `familyName` (CRM requires both); at least one
    /// contact medium is required, defaulting to a `{given}@local` placeholder email
    /// when neither email nor phone is given. Backs `customer.create`.
    pub async fn create_customer(
        &self,
        name: &str,
        email: Option<&str>,
        phone: Option<&str>,
    ) -> Result<Value, ClientError> {
        let trimmed = name.trim();
        let (given_name, family_name) = match trimmed.split_once(char::is_whitespace) {
            Some((g, rest)) => (g.to_string(), rest.trim_start().to_string()),
            None if !trimmed.is_empty() => (trimmed.to_string(), trimmed.to_string()),
            None => (name.to_string(), name.to_string()),
        };
        let email = email.filter(|s| !s.is_empty());
        let phone = phone.filter(|s| !s.is_empty());
        let mut mediums: Vec<Value> = Vec::new();
        if let Some(e) = email {
            mediums.push(json!({"mediumType": "email", "value": e, "isPrimary": true}));
        }
        if let Some(p) = phone {
            mediums.push(json!({"mediumType": "mobile", "value": p, "isPrimary": email.is_none()}));
        }
        if mediums.is_empty() {
            let placeholder = format!("{}@local", given_name.to_lowercase());
            mediums.push(json!({"mediumType": "email", "value": placeholder, "isPrimary": true}));
        }
        let body = json!({
            "givenName": given_name,
            "familyName": family_name,
            "contactMedium": mediums,
        });
        self.post("/tmf-api/customerManagement/v4/customer", &body)
            .await
    }

    /// `PATCH /tmf-api/customerManagement/v4/customer/{id}` with a raw patch body.
    /// Backs `customer.update_contact` (patch of `email`/`phone`) and
    /// `customer.close` (`{"status": "closed"}`).
    pub async fn update_customer(
        &self,
        customer_id: &str,
        patch: &Value,
    ) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}");
        let resp = self
            .inner
            .request(Method::PATCH, &path, Some(patch), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `PATCH …/customer/{id}` with `{"status": "closed"}`. Backs `customer.close`.
    pub async fn close_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        self.update_customer(customer_id, &json!({"status": "closed"}))
            .await
    }

    /// `POST …/customer/{id}/contactMedium` — shape the `characteristic` per medium
    /// type (`email`→`emailAddress`, `mobile`→`phoneNumber`, else `value`). Backs
    /// `customer.add_contact_medium`.
    ///
    /// **Known pre-existing mismatch (reproduced faithfully):** the CRM service route
    /// binds `AddContactMediumRequest` which requires a **top-level `value`**, but this
    /// (matching the Python client) sends only `characteristic` — so the call 422s on
    /// both the Python and Rust services. The fix belongs in the Python oracle first
    /// (R5 / behaviour-frozen); the port does not silently "correct" it. See the
    /// P5c-writes note in PROGRESS.
    pub async fn add_contact_medium(
        &self,
        customer_id: &str,
        medium_type: &str,
        value: &str,
    ) -> Result<Value, ClientError> {
        let characteristic = match medium_type {
            "email" => json!({"emailAddress": value}),
            "mobile" => json!({"phoneNumber": value}),
            _ => json!({"value": value}),
        };
        let body = json!({"mediumType": medium_type, "characteristic": characteristic});
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium");
        self.post(&path, &body).await
    }

    /// `DELETE …/customer/{id}/contactMedium/{cm}` — returns the server body when
    /// present, else `{"id": <cm>, "removed": true}` (empty-body case). Backs
    /// `customer.remove_contact_medium`. DESTRUCTIVE (safety-gated at the tool).
    pub async fn remove_contact_medium(
        &self,
        customer_id: &str,
        medium_id: &str,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium/{medium_id}"
        );
        let resp = self
            .inner
            .request(Method::DELETE, &path, None, None)
            .await?;
        let bytes = resp
            .bytes()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))?;
        if bytes.is_empty() {
            return Ok(json!({"id": medium_id, "removed": true}));
        }
        serde_json::from_slice(&bytes).map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /crm-api/v1/customer/{id}/kyc-attestation` — record a signed KYC
    /// attestation. Ports the stub defaults the tool relies on (it passes only
    /// `provider` + `attestation_token`): a per-customer `document_number` derived
    /// from the id's digits (so portal signups don't collide on the hash-unique
    /// policy), a `provider_reference`, a stub `attestation_payload`, and a
    /// `verified_at` of now (non-deterministic — services that need freezable time
    /// pass their own). Backs `customer.attest_kyc`.
    pub async fn attest_kyc(
        &self,
        customer_id: &str,
        provider: &str,
        attestation_token: &str,
    ) -> Result<Value, ClientError> {
        self.attest_kyc_full(
            customer_id,
            provider,
            attestation_token,
            AttestKycOpts::default(),
        )
        .await
    }

    /// Full channel-layer attestation — the signup funnel's prebaked/Didit path
    /// fills every field; [`attest_kyc`] is the 3-arg stub-defaults wrapper. Both
    /// build a byte-identical body to the Python `attest_kyc` for the same inputs:
    /// `document_number` is always the caller's value or a deterministic
    /// per-customer stub, and `document_number_last4`/`_hash`/`corroboration_id`
    /// are sent only when supplied (the v0.15 reduced-PII form).
    pub async fn attest_kyc_full(
        &self,
        customer_id: &str,
        provider: &str,
        attestation_token: &str,
        opts: AttestKycOpts<'_>,
    ) -> Result<Value, ClientError> {
        let verified_at = match opts.verified_at {
            Some(v) => v.to_string(),
            None => chrono::Utc::now()
                .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
                .to_string(),
        };
        let body = build_attest_body(
            customer_id,
            provider,
            attestation_token,
            &verified_at,
            &opts,
        );
        let path = format!("/crm-api/v1/customer/{customer_id}/kyc-attestation");
        self.post(&path, &body).await
    }

    /// `POST /tmf-api/customerInteractionManagement/v1/interaction` — log a TMF683
    /// interaction with the server defaults: `direction=inbound`, `channel` filled
    /// from the request context server-side. Backs `interaction.log` (the only
    /// caller that wants the defaults).
    pub async fn log_interaction(
        &self,
        customer_id: &str,
        summary: &str,
        body_text: Option<&str>,
    ) -> Result<Value, ClientError> {
        self.log_interaction_full(customer_id, summary, None, None, body_text)
            .await
    }

    /// `POST /tmf-api/customerInteractionManagement/v1/interaction` — the full
    /// TMF683 surface. `direction` defaults to `inbound` when `None`; `channel` is
    /// omitted when `None` (the server fills it from the caller's `X-BSS-Channel`);
    /// `body_text` maps to the optional `body` free-text field.
    ///
    /// The ownership trip-wire's audit record needs `direction="outbound"`, which
    /// the 3-arg form can't express.
    pub async fn log_interaction_full(
        &self,
        customer_id: &str,
        summary: &str,
        channel: Option<&str>,
        direction: Option<&str>,
        body_text: Option<&str>,
    ) -> Result<Value, ClientError> {
        self.post(
            "/tmf-api/customerInteractionManagement/v1/interaction",
            &build_interaction_body(customer_id, summary, channel, direction, body_text),
        )
        .await
    }

    // ── case writes (/crm-api/v1/case) ───────────────────────────────────

    /// `POST /crm-api/v1/case` — open a case (snake_case body). Optional
    /// `description` / `opened_by_agent_id` / `chat_transcript_hash` are sent only
    /// when present. Backs `case.open` (+ later `case.open_for_me`).
    #[allow(clippy::too_many_arguments)]
    pub async fn open_case(
        &self,
        customer_id: &str,
        subject: &str,
        category: &str,
        priority: &str,
        description: Option<&str>,
        opened_by_agent_id: Option<&str>,
        chat_transcript_hash: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut map = serde_json::Map::new();
        map.insert("customer_id".to_string(), json!(customer_id));
        map.insert("subject".to_string(), json!(subject));
        map.insert("category".to_string(), json!(category));
        map.insert("priority".to_string(), json!(priority));
        if let Some(d) = description {
            map.insert("description".to_string(), json!(d));
        }
        if let Some(a) = opened_by_agent_id {
            map.insert("opened_by_agent_id".to_string(), json!(a));
        }
        if let Some(h) = chat_transcript_hash {
            map.insert("chat_transcript_hash".to_string(), json!(h));
        }
        self.post("/crm-api/v1/case", &Value::Object(map)).await
    }

    /// `POST /crm-api/v1/chat-transcript` — idempotent transcript store (hash PK).
    /// Backs `case.open_for_me`'s transcript persistence.
    pub async fn store_chat_transcript(
        &self,
        hash: &str,
        customer_id: &str,
        body: &str,
    ) -> Result<Value, ClientError> {
        let payload = json!({"hash": hash, "customer_id": customer_id, "body": body});
        self.post("/crm-api/v1/chat-transcript", &payload).await
    }

    /// `POST /crm-api/v1/case/{id}/note`. Backs `case.add_note`.
    pub async fn add_case_note(&self, case_id: &str, body: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/case/{case_id}/note");
        self.post(&path, &json!({"body": body})).await
    }

    /// `PATCH /crm-api/v1/case/{id}` with a raw patch. Backs `case.update_priority`
    /// (`{"priority": …}`) and the trigger transition (`{"trigger": …}`).
    pub async fn patch_case(&self, case_id: &str, patch: &Value) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/case/{case_id}");
        let resp = self
            .inner
            .request(Method::PATCH, &path, Some(patch), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /crm-api/v1/case/{id}/close` with snake_case `{resolution_code}`. Backs
    /// `case.close`.
    pub async fn close_case(
        &self,
        case_id: &str,
        resolution_code: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/case/{case_id}/close");
        self.post(&path, &json!({"resolution_code": resolution_code}))
            .await
    }

    // ── ticket writes (TMF621) ────────────────────────────────────────────

    /// `POST /tmf-api/troubleTicket/v4/troubleTicket` — open a ticket. `customerId`
    /// / `caseId` are direct fields; order/subscription/service refs attach as
    /// `relatedEntity`. Backs `ticket.open`.
    #[allow(clippy::too_many_arguments)]
    pub async fn open_ticket(
        &self,
        ticket_type: &str,
        subject: &str,
        case_id: Option<&str>,
        customer_id: Option<&str>,
        order_id: Option<&str>,
        subscription_id: Option<&str>,
        service_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut map = serde_json::Map::new();
        map.insert("ticketType".to_string(), json!(ticket_type));
        map.insert("subject".to_string(), json!(subject));
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            map.insert("customerId".to_string(), json!(c));
        }
        if let Some(c) = case_id.filter(|s| !s.is_empty()) {
            map.insert("caseId".to_string(), json!(c));
        }
        let mut relates: Vec<Value> = Vec::new();
        if let Some(o) = order_id.filter(|s| !s.is_empty()) {
            relates.push(json!({"entityType": "order", "id": o}));
        }
        if let Some(s) = subscription_id.filter(|s| !s.is_empty()) {
            relates.push(json!({"entityType": "subscription", "id": s}));
        }
        if let Some(s) = service_id.filter(|s| !s.is_empty()) {
            relates.push(json!({"entityType": "service", "id": s}));
        }
        if !relates.is_empty() {
            map.insert("relatedEntity".to_string(), Value::Array(relates));
        }
        self.post(
            "/tmf-api/troubleTicket/v4/troubleTicket",
            &Value::Object(map),
        )
        .await
    }

    /// `PATCH …/troubleTicket/{id}` with `{assignedToAgentId}`. Backs `ticket.assign`.
    pub async fn assign_ticket(
        &self,
        ticket_id: &str,
        agent_id: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}");
        let resp = self
            .inner
            .request(
                Method::PATCH,
                &path,
                Some(&json!({"assignedToAgentId": agent_id})),
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST …/troubleTicket/{id}/transition` with `{trigger}` (the caller resolves
    /// target-state → trigger). Backs `ticket.transition` / `ticket.close`.
    pub async fn transition_ticket(
        &self,
        ticket_id: &str,
        trigger: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/transition");
        self.post(&path, &json!({"trigger": trigger})).await
    }

    /// `POST …/troubleTicket/{id}/resolve` with `{resolutionNotes}`. Backs
    /// `ticket.resolve`.
    pub async fn resolve_ticket(
        &self,
        ticket_id: &str,
        resolution_notes: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/resolve");
        self.post(&path, &json!({"resolutionNotes": resolution_notes}))
            .await
    }

    /// `POST …/troubleTicket/{id}/cancel` (no body). Backs `ticket.cancel`
    /// (DESTRUCTIVE — safety-gated at the tool).
    pub async fn cancel_ticket(&self, ticket_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/troubleTicket/v4/troubleTicket/{ticket_id}/cancel");
        let resp = self.inner.request(Method::POST, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    // ── port-request writes (v0.17 MNP) ──────────────────────────────────

    /// `POST /crm-api/v1/port-requests` — open a port request. `targetSubscriptionId`
    /// is sent only when present (required for port_out). Backs `port_request.create`.
    pub async fn create_port_request(
        &self,
        direction: &str,
        donor_carrier: &str,
        donor_msisdn: &str,
        requested_port_date: &str,
        target_subscription_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut map = serde_json::Map::new();
        map.insert("direction".to_string(), json!(direction));
        map.insert("donorCarrier".to_string(), json!(donor_carrier));
        map.insert("donorMsisdn".to_string(), json!(donor_msisdn));
        map.insert("requestedPortDate".to_string(), json!(requested_port_date));
        if let Some(t) = target_subscription_id.filter(|s| !s.is_empty()) {
            map.insert("targetSubscriptionId".to_string(), json!(t));
        }
        self.post("/crm-api/v1/port-requests", &Value::Object(map))
            .await
    }

    /// `POST /crm-api/v1/port-requests/{id}/approve` (no body). Backs
    /// `port_request.approve`.
    pub async fn approve_port_request(&self, port_id: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/port-requests/{port_id}/approve");
        let resp = self.inner.request(Method::POST, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /crm-api/v1/port-requests/{id}/reject` with `{reason}`. Backs
    /// `port_request.reject`.
    pub async fn reject_port_request(
        &self,
        port_id: &str,
        reason: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/port-requests/{port_id}/reject");
        self.post(&path, &json!({"reason": reason})).await
    }

    async fn post(&self, path: &str, body: &Value) -> Result<Value, ClientError> {
        let resp = self
            .inner
            .request(Method::POST, path, Some(body), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// Build the TMF683 `interaction` request body. Pure so it can be golden-tested
/// against the Python oracle. Key order mirrors Python's dict literal exactly
/// (`customerId`, `summary`, `direction`, then the conditional `channel` /
/// `body`) — D9 has `preserve_order` on workspace-wide, so order is wire-visible.
fn build_interaction_body(
    customer_id: &str,
    summary: &str,
    channel: Option<&str>,
    direction: Option<&str>,
    body_text: Option<&str>,
) -> Value {
    let mut map = serde_json::Map::new();
    map.insert("customerId".to_string(), json!(customer_id));
    map.insert("summary".to_string(), json!(summary));
    map.insert(
        "direction".to_string(),
        json!(direction.unwrap_or("inbound")),
    );
    if let Some(c) = channel {
        map.insert("channel".to_string(), json!(c));
    }
    if let Some(b) = body_text {
        map.insert("body".to_string(), json!(b));
    }
    Value::Object(map)
}

/// Build the `kyc-attestation` request body. Pure so it can be golden-tested
/// against the Python oracle. `verified_at` is resolved by the caller (opts or
/// `now()`); every other field mirrors the Python `attest_kyc` key order + defaults.
fn build_attest_body(
    customer_id: &str,
    provider: &str,
    attestation_token: &str,
    verified_at: &str,
    opts: &AttestKycOpts<'_>,
) -> Value {
    // Stub document_number: 7 digits from the id's digit tail, zero-padded.
    let document_number = match opts.document_number {
        Some(dn) => dn.to_string(),
        None => {
            let tail: String = customer_id.chars().filter(char::is_ascii_digit).collect();
            let digits: String = if tail.is_empty() {
                // Unreachable for prefixed (CUST-…) ids, which always carry digits;
                // a deterministic placeholder replaces Python's randomized fallback.
                "0000001".to_string()
            } else {
                format!("{tail}0000000").chars().take(7).collect()
            };
            format!("S{digits}D")
        }
    };
    let provider_reference = match opts.provider_reference {
        Some(r) => r.to_string(),
        None => format!("{provider}-{}", last_chars(attestation_token, 8)),
    };
    let sig_tail = last_chars(attestation_token, 16);
    let mut map = serde_json::Map::new();
    map.insert("provider".to_string(), json!(provider));
    map.insert("provider_reference".to_string(), json!(provider_reference));
    map.insert(
        "document_type".to_string(),
        json!(opts.document_type.unwrap_or("nric")),
    );
    map.insert(
        "document_country".to_string(),
        json!(opts.document_country.unwrap_or("SG")),
    );
    map.insert(
        "date_of_birth".to_string(),
        json!(opts.date_of_birth.unwrap_or("1990-01-01")),
    );
    // Python default `nationality="SG"`; callers may not override it.
    map.insert(
        "nationality".to_string(),
        json!(opts.nationality.unwrap_or("SG")),
    );
    map.insert("verified_at".to_string(), json!(verified_at));
    map.insert(
        "attestation_payload".to_string(),
        json!({
            "token": attestation_token,
            "signature": format!("stub-sig-{sig_tail}"),
        }),
    );
    map.insert("document_number".to_string(), json!(document_number));
    if let Some(l4) = opts.document_number_last4 {
        map.insert("document_number_last4".to_string(), json!(l4));
    }
    if let Some(h) = opts.document_number_hash {
        map.insert("document_number_hash".to_string(), json!(h));
    }
    if let Some(c) = opts.corroboration_id {
        map.insert("corroboration_id".to_string(), json!(c));
    }
    Value::Object(map)
}

/// Last `n` characters of `s` (Python's `s[-n:]`, char-wise; whole string when
/// shorter than `n`).
fn last_chars(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    let start = chars.len().saturating_sub(n);
    chars[start..].iter().collect()
}

/// Minimal query-value encoding for the characters that appear in ids/emails
/// (space + `&`/`+`/`=`/`%`/`#`). Mirrors `catalog::encode`; ids are
/// `CUST-001`-shaped so this is a safety net, not a general URL encoder — except
/// email `+` addressing, which it does cover.
fn encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'%' => out.push_str("%25"),
            b'&' => out.push_str("%26"),
            b'+' => out.push_str("%2B"),
            b'=' => out.push_str("%3D"),
            b'#' => out.push_str("%23"),
            b' ' => out.push_str("%20"),
            _ => out.push(b as char),
        }
    }
    out
}

#[cfg(test)]
mod attest_tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    /// Strip the non-deterministic `verified_at` and compare to the Python oracle.
    fn body_without_verified_at(mut v: Value) -> Value {
        v.as_object_mut().unwrap().remove("verified_at");
        v
    }

    /// Golden — serialized byte-for-byte against the Python oracle's payload dict.
    /// Key ORDER is part of the contract (D9 `preserve_order`).
    #[test]
    fn interaction_body_defaults_match_oracle() {
        // What `interaction.log` sends through the 3-arg wrapper.
        let body = build_interaction_body("CUST-001", "Called in", None, None, Some("some notes"));
        assert_eq!(
            serde_json::to_string(&body).unwrap(),
            r#"{"customerId":"CUST-001","summary":"Called in","direction":"inbound","body":"some notes"}"#
        );
    }

    #[test]
    fn interaction_body_outbound_matches_oracle() {
        // The ownership trip-wire's audit record — the case the 3-arg form can't
        // express (direction=outbound).
        let body = build_interaction_body(
            "CUST-002",
            "P0 agent ownership violation on 'x' — output leaked a=1",
            None,
            Some("outbound"),
            Some("Tool: x"),
        );
        assert_eq!(
            serde_json::to_string(&body).unwrap(),
            r#"{"customerId":"CUST-002","summary":"P0 agent ownership violation on 'x' — output leaked a=1","direction":"outbound","body":"Tool: x"}"#
        );
    }

    #[test]
    fn interaction_body_omits_absent_optionals() {
        let body = build_interaction_body("CUST-003", "bare", None, None, None);
        assert_eq!(
            serde_json::to_string(&body).unwrap(),
            r#"{"customerId":"CUST-003","summary":"bare","direction":"inbound"}"#
        );
    }

    #[test]
    fn attest_body_full_matches_oracle() {
        // Signup prebaked case for ada@example.sg / CUST-001.
        let opts = AttestKycOpts {
            provider_reference: Some("KYC-PREBAKED-001"),
            document_type: Some("nric"),
            document_number_last4: Some("943E"),
            document_number_hash: Some(
                "46214a44de9e853364fdda7651b017d3c09d9dfe90c4a08c4d82653eaef0d8d7",
            ),
            document_country: Some("SGP"),
            date_of_birth: Some("1990-01-01"),
            ..Default::default()
        };
        let body = build_attest_body(
            "CUST-001",
            "prebaked",
            "prebaked-simulated-v1::ada@example.sg",
            "IGNORED",
            &opts,
        );
        let expected: Value = serde_json::from_str(
            r#"{"attestation_payload":{"signature":"stub-sig-::ada@example.sg","token":"prebaked-simulated-v1::ada@example.sg"},"date_of_birth":"1990-01-01","document_country":"SGP","document_number":"S0010000D","document_number_hash":"46214a44de9e853364fdda7651b017d3c09d9dfe90c4a08c4d82653eaef0d8d7","document_number_last4":"943E","document_type":"nric","nationality":"SG","provider":"prebaked","provider_reference":"KYC-PREBAKED-001"}"#,
        )
        .unwrap();
        assert_eq!(body_without_verified_at(body), expected);
    }

    #[test]
    fn attest_body_stub_defaults_matches_oracle() {
        // 3-arg (orchestrator) path for CUST-042.
        let body = build_attest_body(
            "CUST-042",
            "prebaked",
            "tok-abc123def456ghi789",
            "IGNORED",
            &AttestKycOpts::default(),
        );
        let expected: Value = serde_json::from_str(
            r#"{"attestation_payload":{"signature":"stub-sig-c123def456ghi789","token":"tok-abc123def456ghi789"},"date_of_birth":"1990-01-01","document_country":"SG","document_number":"S0420000D","document_type":"nric","nationality":"SG","provider":"prebaked","provider_reference":"prebaked-56ghi789"}"#,
        )
        .unwrap();
        assert_eq!(body_without_verified_at(body), expected);
    }
}
