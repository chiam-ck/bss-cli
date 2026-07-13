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
    pub async fn list_customers(
        &self,
        state: Option<&str>,
        name_contains: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("status={}", encode(s)));
        }
        if let Some(n) = name_contains.filter(|s| !s.is_empty()) {
            params.push(format!("name={}", encode(n)));
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
        // Stub document_number: 7 digits from the id's digit tail, zero-padded.
        let tail: String = customer_id.chars().filter(char::is_ascii_digit).collect();
        let digits: String = if tail.is_empty() {
            // Unreachable for prefixed (CUST-…) ids, which always carry digits; a
            // deterministic placeholder replaces Python's randomized hash fallback.
            "0000001".to_string()
        } else {
            format!("{tail}0000000").chars().take(7).collect()
        };
        let document_number = format!("S{digits}D");
        let ref_tail = last_chars(attestation_token, 8);
        let sig_tail = last_chars(attestation_token, 16);
        let verified_at = chrono::Utc::now()
            .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
            .to_string();
        let body = json!({
            "provider": provider,
            "provider_reference": format!("{provider}-{ref_tail}"),
            "document_type": "nric",
            "document_country": "SG",
            "date_of_birth": "1990-01-01",
            "nationality": "SG",
            "verified_at": verified_at,
            "attestation_payload": {
                "token": attestation_token,
                "signature": format!("stub-sig-{sig_tail}"),
            },
            "document_number": document_number,
        });
        let path = format!("/crm-api/v1/customer/{customer_id}/kyc-attestation");
        self.post(&path, &body).await
    }

    /// `POST /tmf-api/customerInteractionManagement/v1/interaction` — log a TMF683
    /// interaction. `direction` defaults to `inbound`; `channel` is filled from the
    /// request context server-side (never sent here); `body_text` maps to the
    /// optional `body` field. Backs `interaction.log`.
    pub async fn log_interaction(
        &self,
        customer_id: &str,
        summary: &str,
        body_text: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut map = serde_json::Map::new();
        map.insert("customerId".to_string(), json!(customer_id));
        map.insert("summary".to_string(), json!(summary));
        map.insert("direction".to_string(), json!("inbound"));
        if let Some(b) = body_text {
            map.insert("body".to_string(), json!(b));
        }
        self.post(
            "/tmf-api/customerInteractionManagement/v1/interaction",
            &Value::Object(map),
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
