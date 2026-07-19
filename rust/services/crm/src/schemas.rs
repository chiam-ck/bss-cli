//! Request bodies + response projections — port of `app.schemas.*`.
//!
//! TMF surfaces (customer/ticket/interaction) render camelCase + `@type` and `Z`
//! datetimes (Pydantic v2). Internal DTOs (case/kyc/agent/inventory) are
//! snake_case; port-request is camelCase. `date` fields render ISO `YYYY-MM-DD`.

use chrono::{DateTime, NaiveDate, Utc};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::repo::{
    AgentRow, CaseFull, CaseNoteRow, ChatTranscriptRow, ContactMediumRow, CustomerFull, EsimRow,
    InteractionRow, MsisdnRow, PortRequestRow, TicketRow,
};

/// Pydantic-v2 datetime: RFC3339 with `Z`, micros only when nonzero.
pub fn tmf_datetime(dt: DateTime<Utc>) -> String {
    if dt.timestamp_subsec_micros() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

fn dt(v: Option<DateTime<Utc>>) -> Value {
    v.map(tmf_datetime).map(Value::from).unwrap_or(Value::Null)
}

fn date_str(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

// ── TMF629 customer ─────────────────────────────────────────────────────────

pub const CUSTOMER_PATH: &str = "/tmf-api/customerManagement/v4/customer";
pub const TICKET_PATH: &str = "/tmf-api/troubleTicket/v4/troubleTicket";

fn contact_medium_value(cm: &ContactMediumRow) -> Value {
    json!({
        "id": cm.id,
        "mediumType": cm.medium_type,
        "value": cm.value,
        "isPrimary": cm.is_primary,
        "validFrom": dt(cm.valid_from),
        "validTo": dt(cm.valid_to),
    })
}

pub fn to_tmf629_customer(full: &CustomerFull) -> Value {
    let c = &full.customer;
    let individual = full.individual.as_ref().map(|i| {
        json!({
            "givenName": i.given_name,
            "familyName": i.family_name,
            "dateOfBirth": i.date_of_birth.map(date_str),
        })
    });
    json!({
        "id": c.id,
        "href": format!("{CUSTOMER_PATH}/{}", c.id),
        "status": c.status,
        "kycStatus": c.kyc_status,
        "customerSince": dt(c.customer_since),
        "individual": individual,
        "contactMedium": full.contact_mediums.iter().map(contact_medium_value).collect::<Vec<_>>(),
        "@type": "Customer",
    })
}

/// A single contact-medium body (the POST/PATCH contactMedium responses).
pub fn to_contact_medium(cm: &ContactMediumRow) -> Value {
    contact_medium_value(cm)
}

// ── TMF621 ticket ───────────────────────────────────────────────────────────

pub fn to_tmf621_ticket(t: &TicketRow) -> Value {
    json!({
        "id": t.id,
        "href": format!("{TICKET_PATH}/{}", t.id),
        "ticketType": t.ticket_type,
        "subject": t.subject,
        "description": t.description,
        "state": t.state,
        "priority": t.priority,
        "customerId": t.customer_id,
        "caseId": t.case_id,
        "assignedToAgentId": t.assigned_to_agent_id,
        "resolutionNotes": t.resolution_notes,
        "openedAt": tmf_datetime(t.opened_at),
        "resolvedAt": dt(t.resolved_at),
        "closedAt": dt(t.closed_at),
        "@type": "TroubleTicket",
    })
}

// ── TMF683 interaction ──────────────────────────────────────────────────────

pub fn to_tmf683_interaction(i: &InteractionRow) -> Value {
    json!({
        "id": i.id,
        "customerId": i.customer_id,
        "channel": i.channel,
        "direction": i.direction,
        "summary": i.summary,
        "body": i.body,
        "agentId": i.agent_id,
        "relatedCaseId": i.related_case_id,
        "relatedTicketId": i.related_ticket_id,
        "occurredAt": tmf_datetime(i.occurred_at),
        "@type": "Interaction",
    })
}

// ── internal case (snake_case) ──────────────────────────────────────────────

fn note_value(n: &CaseNoteRow) -> Value {
    json!({
        "id": n.id,
        "case_id": n.case_id,
        "author_agent_id": n.author_agent_id,
        "body": n.body,
        "created_at": tmf_datetime(n.created_at),
    })
}

pub fn to_case_note_response(n: &CaseNoteRow) -> Value {
    note_value(n)
}

pub fn to_case_response(full: &CaseFull) -> Value {
    let c = &full.case;
    json!({
        "id": c.id,
        "customer_id": c.customer_id,
        "subject": c.subject,
        "description": c.description,
        "state": c.state,
        "priority": c.priority,
        "category": c.category,
        "resolution_code": c.resolution_code,
        "opened_by_agent_id": c.opened_by_agent_id,
        "opened_at": tmf_datetime(c.opened_at),
        "closed_at": dt(c.closed_at),
        "notes": full.notes.iter().map(note_value).collect::<Vec<_>>(),
        "ticket_ids": full.ticket_ids,
        "chat_transcript_hash": c.chat_transcript_hash,
    })
}

// ── internal agent / inventory / port / transcript ──────────────────────────

pub fn to_agent_response(a: &AgentRow) -> Value {
    json!({ "id": a.id, "name": a.name, "email": a.email, "role": a.role, "status": a.status })
}

pub fn to_msisdn_response(m: &MsisdnRow) -> Value {
    json!({
        "msisdn": m.msisdn,
        "status": m.status,
        "reserved_at": dt(m.reserved_at),
        "assigned_to_subscription_id": m.assigned_to_subscription_id,
    })
}

pub fn to_esim_response(e: &EsimRow) -> Value {
    json!({
        "iccid": e.iccid,
        "imsi": e.imsi,
        "profile_state": e.profile_state,
        "smdp_server": e.smdp_server,
        "assigned_msisdn": e.assigned_msisdn,
        "assigned_to_subscription_id": e.assigned_to_subscription_id,
        "reserved_at": dt(e.reserved_at),
        "downloaded_at": dt(e.downloaded_at),
        "activated_at": dt(e.activated_at),
    })
}

pub fn to_esim_activation(e: &EsimRow) -> Value {
    json!({
        "iccid": e.iccid,
        "activation_code": e.activation_code,
        "smdp_server": e.smdp_server,
        "matching_id": e.matching_id,
    })
}

pub fn to_port_request_response(p: &PortRequestRow) -> Value {
    json!({
        "id": p.id,
        "direction": p.direction,
        "donorCarrier": p.donor_carrier,
        "donorMsisdn": p.donor_msisdn,
        "targetSubscriptionId": p.target_subscription_id,
        "requestedPortDate": date_str(p.requested_port_date),
        "state": p.state,
        "rejectionReason": p.rejection_reason,
        "createdAt": tmf_datetime(p.created_at),
        "updatedAt": tmf_datetime(p.updated_at),
    })
}

pub fn to_chat_transcript_response(t: &ChatTranscriptRow) -> Value {
    json!({
        "hash": t.hash,
        "customer_id": t.customer_id,
        "body": t.body,
        "recorded_at": tmf_datetime(t.recorded_at),
    })
}

// ── request bodies ──────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ContactMediumInput {
    #[serde(alias = "medium_type")]
    pub medium_type: String,
    pub value: String,
    #[serde(alias = "is_primary", default)]
    pub is_primary: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateCustomerRequest {
    #[serde(alias = "given_name")]
    pub given_name: String,
    #[serde(alias = "family_name")]
    pub family_name: String,
    #[serde(alias = "date_of_birth", default)]
    pub date_of_birth: Option<String>,
    #[serde(alias = "contact_medium")]
    pub contact_medium: Vec<ContactMediumInput>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateCustomerRequest {
    #[serde(default)]
    pub status: Option<String>,
    #[serde(alias = "status_reason", default)]
    pub status_reason: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AddContactMediumRequest {
    #[serde(alias = "medium_type")]
    pub medium_type: String,
    pub value: String,
    #[serde(alias = "is_primary", default)]
    pub is_primary: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateContactMediumRequest {
    pub value: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateIndividualRequest {
    #[serde(alias = "given_name", default)]
    pub given_name: Option<String>,
    #[serde(alias = "family_name", default)]
    pub family_name: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateTicketRequest {
    #[serde(alias = "customer_id")]
    pub customer_id: String,
    pub subject: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(alias = "ticket_type", default = "default_info_request")]
    pub ticket_type: String,
    #[serde(default = "default_normal")]
    pub priority: String,
    #[serde(alias = "case_id", default)]
    pub case_id: Option<String>,
    #[serde(alias = "assigned_to_agent_id", default)]
    pub assigned_to_agent_id: Option<String>,
}

fn default_info_request() -> String {
    "information_request".to_string()
}
fn default_normal() -> String {
    "normal".to_string()
}
fn default_general() -> String {
    "general".to_string()
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateTicketRequest {
    #[serde(default)]
    pub priority: Option<String>,
    #[serde(alias = "assigned_to_agent_id", default)]
    pub assigned_to_agent_id: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TransitionTicketRequest {
    pub trigger: String,
    #[serde(alias = "assigned_to_agent_id", default)]
    pub assigned_to_agent_id: Option<String>,
    #[serde(alias = "resolution_notes", default)]
    pub resolution_notes: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolveTicketRequest {
    #[serde(alias = "resolution_notes")]
    pub resolution_notes: String,
}

#[derive(Debug, Deserialize)]
pub struct OpenCaseRequest {
    pub customer_id: String,
    pub subject: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default = "default_normal")]
    pub priority: String,
    #[serde(default = "default_general")]
    pub category: String,
    #[serde(default)]
    pub opened_by_agent_id: Option<String>,
    #[serde(default)]
    pub chat_transcript_hash: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct PatchCaseRequest {
    #[serde(default)]
    pub trigger: Option<String>,
    #[serde(default)]
    pub resolution_code: Option<String>,
    #[serde(default)]
    pub priority: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct CloseCaseRequest {
    pub resolution_code: String,
}

#[derive(Debug, Deserialize)]
pub struct AddNoteRequest {
    pub body: String,
    #[serde(default)]
    pub author_agent_id: Option<String>,
}

// The oracle's `CreateInteractionRequest` extends `TmfBase` (camelCase alias +
// populate_by_name), so it accepts BOTH `customerId` and `customer_id`. Mirror
// that: rename_all camelCase + a snake_case alias per field.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateInteractionRequest {
    #[serde(alias = "customer_id")]
    pub customer_id: String,
    #[serde(default)]
    pub channel: Option<String>,
    #[serde(default = "default_inbound")]
    pub direction: String,
    pub summary: String,
    #[serde(default)]
    pub body: Option<String>,
    #[serde(alias = "agent_id", default)]
    pub agent_id: Option<String>,
    #[serde(alias = "related_case_id", default)]
    pub related_case_id: Option<String>,
    #[serde(alias = "related_ticket_id", default)]
    pub related_ticket_id: Option<String>,
}

fn default_inbound() -> String {
    "inbound".to_string()
}

#[derive(Debug, Deserialize)]
pub struct KycAttestationRequest {
    pub provider: String,
    pub provider_reference: String,
    pub document_type: String,
    #[serde(default)]
    pub document_number: Option<String>,
    #[serde(default)]
    pub document_number_last4: Option<String>,
    #[serde(default)]
    pub document_number_hash: Option<String>,
    pub document_country: String,
    pub date_of_birth: String,
    #[serde(default)]
    pub nationality: Option<String>,
    pub verified_at: String,
    pub attestation_payload: Value,
    #[serde(default)]
    pub corroboration_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AssignMsisdnBody {
    pub msisdn: String,
}

#[derive(Debug, Deserialize)]
pub struct AddRangeRequest {
    pub prefix: String,
    pub count: i64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreatePortRequest {
    pub direction: String,
    #[serde(alias = "donor_carrier")]
    pub donor_carrier: String,
    #[serde(alias = "donor_msisdn")]
    pub donor_msisdn: String,
    #[serde(alias = "target_subscription_id", default)]
    pub target_subscription_id: Option<String>,
    #[serde(alias = "requested_port_date")]
    pub requested_port_date: NaiveDate,
}

#[derive(Debug, Deserialize)]
pub struct RejectPortRequest {
    pub reason: String,
}

#[derive(Debug, Deserialize)]
pub struct StoreChatTranscriptRequest {
    pub hash: String,
    pub customer_id: String,
    pub body: String,
}
