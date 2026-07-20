//! HTTP surface — port of every router in `app.api.*`. axum 0.7 path params `:name`.
//! Only `/health` is perimeter-exempt (`/ready` requires a token, like the oracle).

use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, patch, post},
    Extension, Json, Router,
};
use bss_context::RequestCtx;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::schemas as sc;
use crate::service as svc;
use crate::state::AppState;
use crate::{repo, schemas};

const TMF_CUST: &str = "/tmf-api/customerManagement/v4";
const TMF_INT: &str = "/tmf-api/customerInteractionManagement/v1";
const TMF_TKT: &str = "/tmf-api/troubleTicket/v4";
const CRM: &str = "/crm-api/v1";
const INV: &str = "/inventory-api/v1";

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

async fn health(State(s): State<AppState>) -> Json<Value> {
    Json(
        json!({ "status": "ok", "service": s.settings.service_name, "version": s.settings.version }),
    )
}
async fn ready(State(s): State<AppState>) -> Json<Value> {
    match sqlx::query("SELECT 1").execute(&s.pool).await {
        Ok(_) => Json(json!({ "status": "ready", "service": s.settings.service_name })),
        Err(_) => Json(json!({ "status": "unavailable", "service": s.settings.service_name })),
    }
}

fn def_20() -> i64 {
    20
}
fn def_50() -> i64 {
    50
}

// ── TMF629 customer ─────────────────────────────────────────────────────────

pub fn customer_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{TMF_CUST}/customer"),
            post(create_customer).get(list_customers),
        )
        .route(
            &format!("{TMF_CUST}/customer/by-msisdn/:msisdn"),
            get(customer_by_msisdn),
        )
        .route(
            &format!("{TMF_CUST}/customer/by-email"),
            get(customer_by_email),
        )
        .route(
            &format!("{TMF_CUST}/customer/:id"),
            get(get_customer).patch(patch_customer),
        )
        .route(
            &format!("{TMF_CUST}/customer/:id/contactMedium"),
            post(add_contact_medium),
        )
        .route(
            &format!("{TMF_CUST}/customer/:id/contactMedium/:cm_id"),
            delete(remove_contact_medium).patch(update_contact_medium),
        )
        .route(
            &format!("{TMF_CUST}/customer/:id/individual"),
            patch(update_individual),
        )
}

async fn create_customer(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::CreateCustomerRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let mediums = b
        .contact_medium
        .into_iter()
        .map(|cm| svc::NewContactMedium {
            medium_type: cm.medium_type,
            value: cm.value,
            is_primary: cm.is_primary,
        })
        .collect();
    let full = svc::create_customer(
        &s,
        &ctx,
        &b.given_name,
        &b.family_name,
        b.date_of_birth.as_deref(),
        mediums,
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(schemas::to_tmf629_customer(&full)),
    ))
}

#[derive(Deserialize)]
struct CustListQuery {
    status: Option<String>,
    name: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_customers(
    State(s): State<AppState>,
    Query(q): Query<CustListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_customers(
        &s.pool,
        q.status.as_deref(),
        q.name.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_tmf629_customer).collect(),
    )))
}

async fn customer_by_msisdn(
    State(s): State<AppState>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match svc::find_by_msisdn(&s, &msisdn).await? {
        Some(f) => Ok(Json(schemas::to_tmf629_customer(&f))),
        None => Err(ApiError::NotFound(format!(
            "No customer owns MSISDN {msisdn}"
        ))),
    }
}

#[derive(Deserialize)]
struct EmailQuery {
    email: String,
}

async fn customer_by_email(
    State(s): State<AppState>,
    Query(q): Query<EmailQuery>,
) -> Result<Json<Value>, ApiError> {
    match svc::find_by_email(&s, &q.email).await? {
        Some(f) => Ok(Json(schemas::to_tmf629_customer(&f))),
        None => Err(ApiError::NotFound(format!(
            "No customer has email {}",
            q.email
        ))),
    }
}

async fn get_customer(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_customer_full(&s.pool, &id).await? {
        Some(f) => Ok(Json(schemas::to_tmf629_customer(&f))),
        None => Err(ApiError::NotFound(format!("Customer {id} not found"))),
    }
}

async fn patch_customer(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::UpdateCustomerRequest>,
) -> Result<Json<Value>, ApiError> {
    let full = svc::update_customer(
        &s,
        &ctx,
        &id,
        b.status.as_deref(),
        b.status_reason.as_deref(),
    )
    .await?;
    Ok(Json(schemas::to_tmf629_customer(&full)))
}

async fn add_contact_medium(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::AddContactMediumRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let cm = svc::add_contact_medium(&s, &ctx, &id, &b.medium_type, &b.value, b.is_primary).await?;
    Ok((StatusCode::CREATED, Json(schemas::to_contact_medium(&cm))))
}

async fn remove_contact_medium(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path((id, cm_id)): Path<(String, String)>,
) -> Result<StatusCode, ApiError> {
    svc::remove_contact_medium(&s, &ctx, &id, &cm_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn update_contact_medium(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path((id, cm_id)): Path<(String, String)>,
    Json(b): Json<sc::UpdateContactMediumRequest>,
) -> Result<Json<Value>, ApiError> {
    let cm = svc::update_contact_medium(&s, &ctx, &id, &cm_id, &b.value).await?;
    Ok(Json(schemas::to_contact_medium(&cm)))
}

async fn update_individual(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::UpdateIndividualRequest>,
) -> Result<Json<Value>, ApiError> {
    svc::update_individual_name(
        &s,
        &ctx,
        &id,
        b.given_name.as_deref(),
        b.family_name.as_deref(),
    )
    .await?;
    let full = repo::get_customer_full(&s.pool, &id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Customer {id} not found")))?;
    Ok(Json(schemas::to_tmf629_customer(&full)))
}

// ── TMF621 ticket ───────────────────────────────────────────────────────────

pub fn ticket_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{TMF_TKT}/troubleTicket"),
            post(create_ticket).get(list_tickets),
        )
        .route(
            &format!("{TMF_TKT}/troubleTicket/:id"),
            get(get_ticket).patch(patch_ticket),
        )
        .route(
            &format!("{TMF_TKT}/troubleTicket/:id/transition"),
            post(transition_ticket),
        )
        .route(
            &format!("{TMF_TKT}/troubleTicket/:id/resolve"),
            post(resolve_ticket),
        )
        .route(
            &format!("{TMF_TKT}/troubleTicket/:id/cancel"),
            post(cancel_ticket),
        )
}

async fn create_ticket(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::CreateTicketRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let t = svc::open_ticket(
        &s,
        &ctx,
        &b.customer_id,
        &b.subject,
        b.description.as_deref(),
        &b.ticket_type,
        &b.priority,
        b.case_id.as_deref(),
        b.assigned_to_agent_id.as_deref(),
    )
    .await?;
    Ok((StatusCode::CREATED, Json(schemas::to_tmf621_ticket(&t))))
}

#[derive(Deserialize)]
struct TicketListQuery {
    #[serde(rename = "customerId")]
    customer_id: Option<String>,
    #[serde(rename = "caseId")]
    case_id: Option<String>,
    state: Option<String>,
    #[serde(rename = "assignedToAgentId")]
    assigned_to_agent_id: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_tickets(
    State(s): State<AppState>,
    Query(q): Query<TicketListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_tickets(
        &s.pool,
        q.customer_id.as_deref(),
        q.case_id.as_deref(),
        q.state.as_deref(),
        q.assigned_to_agent_id.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_tmf621_ticket).collect(),
    )))
}

async fn get_ticket(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_ticket(&s.pool, &id).await? {
        Some(t) => Ok(Json(schemas::to_tmf621_ticket(&t))),
        None => Err(ApiError::NotFound(format!("Ticket {id} not found"))),
    }
}

async fn patch_ticket(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::UpdateTicketRequest>,
) -> Result<Json<Value>, ApiError> {
    let t = svc::update_ticket(
        &s,
        &ctx,
        &id,
        b.priority.as_deref(),
        b.assigned_to_agent_id.as_deref(),
        b.description.as_deref(),
    )
    .await?;
    Ok(Json(schemas::to_tmf621_ticket(&t)))
}

async fn transition_ticket(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::TransitionTicketRequest>,
) -> Result<Json<Value>, ApiError> {
    let t = svc::transition_ticket(
        &s,
        &ctx,
        &id,
        &b.trigger,
        b.assigned_to_agent_id.as_deref(),
        b.resolution_notes.as_deref(),
        b.reason.as_deref(),
    )
    .await?;
    Ok(Json(schemas::to_tmf621_ticket(&t)))
}

async fn resolve_ticket(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::ResolveTicketRequest>,
) -> Result<Json<Value>, ApiError> {
    let t = svc::transition_ticket(
        &s,
        &ctx,
        &id,
        "resolve",
        None,
        Some(&b.resolution_notes),
        None,
    )
    .await?;
    Ok(Json(schemas::to_tmf621_ticket(&t)))
}

async fn cancel_ticket(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let t = svc::transition_ticket(&s, &ctx, &id, "cancel", None, None, None).await?;
    Ok(Json(schemas::to_tmf621_ticket(&t)))
}

// ── TMF683 interaction ──────────────────────────────────────────────────────

pub fn interaction_router() -> Router<AppState> {
    Router::new().route(
        &format!("{TMF_INT}/interaction"),
        post(create_interaction).get(list_interactions),
    )
}

async fn create_interaction(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::CreateInteractionRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let v = svc::create_interaction(
        &s,
        &ctx,
        &b.customer_id,
        b.channel.as_deref(),
        &b.direction,
        &b.summary,
        b.body.as_deref(),
        b.agent_id.as_deref(),
        b.related_case_id.as_deref(),
        b.related_ticket_id.as_deref(),
    )
    .await?;
    Ok((StatusCode::CREATED, Json(v)))
}

#[derive(Deserialize)]
struct IntListQuery {
    #[serde(rename = "customerId")]
    customer_id: String,
    #[serde(default = "def_50")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_interactions(
    State(s): State<AppState>,
    Query(q): Query<IntListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_interactions(&s.pool, &q.customer_id, q.limit, q.offset).await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_tmf683_interaction).collect(),
    )))
}

// ── crm-api: case / kyc / agent / chat-transcript / port-request ────────────

pub fn crm_router() -> Router<AppState> {
    Router::new()
        .route(&format!("{CRM}/case"), post(open_case).get(list_cases))
        .route(&format!("{CRM}/case/:id"), get(get_case).patch(patch_case))
        .route(&format!("{CRM}/case/:id/close"), post(close_case))
        .route(&format!("{CRM}/case/:id/note"), post(add_note))
        .route(
            &format!("{CRM}/customer/:id/kyc-attestation"),
            post(attest_kyc),
        )
        .route(&format!("{CRM}/customer/:id/kyc-status"), get(kyc_status))
        .route(&format!("{CRM}/agent"), get(list_agents))
        .route(&format!("{CRM}/agent/:id"), get(get_agent))
        .route(&format!("{CRM}/chat-transcript"), post(store_transcript))
        .route(&format!("{CRM}/chat-transcript/:hash"), get(get_transcript))
        .route(
            &format!("{CRM}/port-requests"),
            post(create_port).get(list_ports),
        )
        .route(&format!("{CRM}/port-requests/:id"), get(get_port))
        .route(
            &format!("{CRM}/port-requests/:id/approve"),
            post(approve_port),
        )
        .route(
            &format!("{CRM}/port-requests/:id/reject"),
            post(reject_port),
        )
}

async fn open_case(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::OpenCaseRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let full = svc::open_case(
        &s,
        &ctx,
        &b.customer_id,
        &b.subject,
        b.description.as_deref(),
        &b.priority,
        &b.category,
        b.opened_by_agent_id.as_deref(),
        b.chat_transcript_hash.as_deref(),
    )
    .await?;
    Ok((StatusCode::CREATED, Json(schemas::to_case_response(&full))))
}

#[derive(Deserialize)]
struct CaseListQuery {
    #[serde(rename = "customerId")]
    customer_id: Option<String>,
    state: Option<String>,
    #[serde(rename = "assignedAgentId")]
    assigned_agent_id: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_cases(
    State(s): State<AppState>,
    Query(q): Query<CaseListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_cases(
        &s.pool,
        q.customer_id.as_deref(),
        q.state.as_deref(),
        q.assigned_agent_id.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_case_response).collect(),
    )))
}

async fn get_case(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_case_full(&s.pool, &id).await? {
        Some(f) => Ok(Json(schemas::to_case_response(&f))),
        None => Err(ApiError::NotFound(format!("Case {id} not found"))),
    }
}

async fn patch_case(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::PatchCaseRequest>,
) -> Result<Json<Value>, ApiError> {
    let has_fields = b.priority.is_some() || b.category.is_some();
    if b.trigger.is_none() && !has_fields {
        return Err(ApiError::BadRequest(
            "PATCH body must carry a trigger and/or priority/category".into(),
        ));
    }
    if let Some(trigger) = &b.trigger {
        svc::transition_case(&s, &ctx, &id, trigger, b.resolution_code.as_deref()).await?;
    }
    if has_fields {
        svc::update_case_fields(&s, &ctx, &id, b.priority.as_deref(), b.category.as_deref())
            .await?;
    }
    let full = repo::get_case_full(&s.pool, &id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Case {id} not found")))?;
    Ok(Json(schemas::to_case_response(&full)))
}

async fn close_case(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::CloseCaseRequest>,
) -> Result<Json<Value>, ApiError> {
    svc::close_case(&s, &ctx, &id, &b.resolution_code).await?;
    let full = repo::get_case_full(&s.pool, &id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Case {id} not found")))?;
    Ok(Json(schemas::to_case_response(&full)))
}

async fn add_note(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::AddNoteRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let v = svc::add_note(&s, &ctx, &id, &b.body, b.author_agent_id.as_deref()).await?;
    Ok((StatusCode::CREATED, Json(v)))
}

async fn attest_kyc(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::KycAttestationRequest>,
) -> Result<Json<Value>, ApiError> {
    tracing::info!(
        customer_id = id,
        provider = b.provider,
        document_type = b.document_type,
        has_corroboration = b.corroboration_id.is_some(),
        "kyc.attestation.received"
    );
    let v = svc::attest_kyc(
        &s,
        &ctx,
        &id,
        &b.provider,
        &b.provider_reference,
        &b.document_type,
        b.document_number.as_deref(),
        b.document_number_last4.as_deref(),
        b.document_number_hash.as_deref(),
        &b.document_country,
        &b.date_of_birth,
        b.nationality.as_deref(),
        &b.verified_at,
        &b.attestation_payload,
        b.corroboration_id.as_deref(),
    )
    .await?;
    Ok(Json(v))
}

async fn kyc_status(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(svc::kyc_status(&s, &id).await?))
}

#[derive(Deserialize)]
struct AgentListQuery {
    status: Option<String>,
    #[serde(default = "def_50")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_agents(
    State(s): State<AppState>,
    Query(q): Query<AgentListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_agents(&s.pool, q.status.as_deref(), q.limit, q.offset).await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_agent_response).collect(),
    )))
}

async fn get_agent(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_agent(&s.pool, &id).await? {
        Some(a) => Ok(Json(schemas::to_agent_response(&a))),
        None => Err(ApiError::NotFound(format!("Agent {id} not found"))),
    }
}

async fn store_transcript(
    State(s): State<AppState>,
    Json(b): Json<sc::StoreChatTranscriptRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let row = svc::store_transcript(&s, &b.hash, &b.customer_id, &b.body).await?;
    Ok((
        StatusCode::CREATED,
        Json(schemas::to_chat_transcript_response(&row)),
    ))
}

async fn get_transcript(
    State(s): State<AppState>,
    Path(hash): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_transcript(&s.pool, &hash).await? {
        Some(t) => Ok(Json(schemas::to_chat_transcript_response(&t))),
        None => Err(ApiError::NotFound(format!(
            "chat transcript {hash} not found"
        ))),
    }
}

#[derive(Deserialize)]
struct PortListQuery {
    state: Option<String>,
    direction: Option<String>,
    #[serde(default = "def_50")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_ports(
    State(s): State<AppState>,
    Query(q): Query<PortListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_port_requests(
        &s.pool,
        q.state.as_deref(),
        q.direction.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_port_request_response).collect(),
    )))
}

async fn get_port(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_port_request(&s.pool, &id).await? {
        Some(p) => Ok(Json(schemas::to_port_request_response(&p))),
        None => Err(ApiError::NotFound(format!("Port request {id} not found"))),
    }
}

async fn create_port(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::CreatePortRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let p = svc::create_port_request(
        &s,
        &ctx,
        &b.direction,
        &b.donor_carrier,
        &b.donor_msisdn,
        b.target_subscription_id.as_deref(),
        b.requested_port_date,
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(schemas::to_port_request_response(&p)),
    ))
}

async fn approve_port(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    svc::approve_port_request(&s, &ctx, &id).await?;
    let p = repo::get_port_request(&s.pool, &id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Port request {id} not found")))?;
    Ok(Json(schemas::to_port_request_response(&p)))
}

async fn reject_port(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
    Json(b): Json<sc::RejectPortRequest>,
) -> Result<Json<Value>, ApiError> {
    svc::reject_port_request(&s, &ctx, &id, &b.reason).await?;
    let p = repo::get_port_request(&s.pool, &id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Port request {id} not found")))?;
    Ok(Json(schemas::to_port_request_response(&p)))
}

// ── inventory ───────────────────────────────────────────────────────────────

pub fn inventory_router() -> Router<AppState> {
    Router::new()
        .route(&format!("{INV}/msisdn"), get(list_msisdns))
        .route(&format!("{INV}/msisdn/count"), get(count_msisdns))
        .route(
            &format!("{INV}/msisdn/reserve-next"),
            post(reserve_next_msisdn),
        )
        .route(&format!("{INV}/msisdn/add-range"), post(add_range))
        .route(&format!("{INV}/msisdn/:msisdn"), get(get_msisdn))
        .route(
            &format!("{INV}/msisdn/:msisdn/reserve"),
            post(reserve_msisdn),
        )
        .route(&format!("{INV}/msisdn/:msisdn/assign"), post(assign_msisdn))
        .route(
            &format!("{INV}/msisdn/:msisdn/release"),
            post(release_msisdn),
        )
        .route(&format!("{INV}/msisdn/:msisdn/hold"), post(hold_msisdn))
        .route(
            &format!("{INV}/msisdn/release-hold"),
            post(release_msisdn_hold),
        )
        .route(&format!("{INV}/esim"), get(list_esims))
        .route(&format!("{INV}/esim/reserve"), post(reserve_esim))
        .route(&format!("{INV}/esim/:iccid"), get(get_esim))
        .route(
            &format!("{INV}/esim/:iccid/assign-msisdn"),
            post(assign_msisdn_to_esim),
        )
        .route(
            &format!("{INV}/esim/:iccid/mark-downloaded"),
            post(mark_downloaded),
        )
        .route(
            &format!("{INV}/esim/:iccid/mark-activated"),
            post(mark_activated),
        )
        .route(&format!("{INV}/esim/:iccid/recycle"), post(recycle_esim))
        .route(&format!("{INV}/esim/:iccid/release"), post(release_esim))
        .route(
            &format!("{INV}/esim/:iccid/activation"),
            get(esim_activation),
        )
}

#[derive(Deserialize)]
struct MsisdnListQuery {
    status: Option<String>,
    prefix: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_msisdns(
    State(s): State<AppState>,
    Query(q): Query<MsisdnListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_msisdns(
        &s.pool,
        q.status.as_deref(),
        q.prefix.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_msisdn_response).collect(),
    )))
}

#[derive(Deserialize)]
struct PrefixQuery {
    prefix: Option<String>,
}

async fn count_msisdns(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Query(q): Query<PrefixQuery>,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(
        svc::count_msisdns(&s, &ctx, q.prefix.as_deref()).await?,
    ))
}

async fn get_msisdn(
    State(s): State<AppState>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_msisdn(&s.pool, &msisdn).await? {
        Some(m) => Ok(Json(schemas::to_msisdn_response(&m))),
        None => Err(ApiError::NotFound(format!("MSISDN {msisdn} not found"))),
    }
}

async fn reserve_msisdn(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let m = svc::reserve_msisdn(&s, &ctx, &msisdn).await?;
    Ok(Json(schemas::to_msisdn_response(&m)))
}

async fn reserve_next_msisdn(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    body: Bytes,
) -> Result<impl IntoResponse, ApiError> {
    let preference = if body.is_empty() {
        None
    } else {
        serde_json::from_slice::<Value>(&body).ok().and_then(|v| {
            v.get("preference")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
    };
    let m = svc::reserve_next_msisdn(&s, &ctx, preference.as_deref()).await?;
    Ok((StatusCode::CREATED, Json(schemas::to_msisdn_response(&m))))
}

async fn assign_msisdn(
    State(s): State<AppState>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let m = svc::assign_msisdn(&s, &msisdn).await?;
    Ok(Json(schemas::to_msisdn_response(&m)))
}

async fn release_msisdn(
    State(s): State<AppState>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let m = svc::release_msisdn(&s, &msisdn).await?;
    Ok(Json(schemas::to_msisdn_response(&m)))
}

async fn add_range(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<sc::AddRangeRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let v = svc::add_msisdn_range(&s, &ctx, &b.prefix, b.count).await?;
    Ok((StatusCode::CREATED, Json(v)))
}

/// Default soft-hold TTL — 24h (the reservation window). Overridable per request.
const DEFAULT_HOLD_TTL_SECS: i64 = 24 * 60 * 60;

#[derive(Deserialize)]
struct HoldRequest {
    #[serde(alias = "reservedFor")]
    reserved_for: String,
    #[serde(default, alias = "ttlSecs")]
    ttl_secs: Option<i64>,
}

async fn hold_msisdn(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(msisdn): Path<String>,
    Json(b): Json<HoldRequest>,
) -> Result<Json<Value>, ApiError> {
    let ttl = b.ttl_secs.unwrap_or(DEFAULT_HOLD_TTL_SECS);
    let m = svc::hold_msisdn(&s, &ctx, &msisdn, &b.reserved_for, ttl).await?;
    Ok(Json(schemas::to_msisdn_response(&m)))
}

#[derive(Deserialize)]
struct ReleaseHoldRequest {
    #[serde(alias = "reservedFor")]
    reserved_for: String,
}

async fn release_msisdn_hold(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<ReleaseHoldRequest>,
) -> Result<Json<Value>, ApiError> {
    let v = svc::release_msisdn_hold(&s, &ctx, &b.reserved_for).await?;
    Ok(Json(v))
}

#[derive(Deserialize)]
struct EsimListQuery {
    status: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_esims(
    State(s): State<AppState>,
    Query(q): Query<EsimListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_esims(&s.pool, q.status.as_deref(), q.limit, q.offset).await?;
    Ok(Json(Value::Array(
        rows.iter().map(schemas::to_esim_response).collect(),
    )))
}

async fn get_esim(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_esim(&s.pool, &iccid).await? {
        Some(e) => Ok(Json(schemas::to_esim_response(&e))),
        None => Err(ApiError::NotFound(format!("eSIM {iccid} not found"))),
    }
}

async fn reserve_esim(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
) -> Result<impl IntoResponse, ApiError> {
    let e = svc::reserve_esim(&s, &ctx).await?;
    Ok((StatusCode::CREATED, Json(schemas::to_esim_response(&e))))
}

async fn assign_msisdn_to_esim(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
    Json(b): Json<sc::AssignMsisdnBody>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::assign_msisdn_to_esim(&s, &iccid, &b.msisdn).await?;
    Ok(Json(schemas::to_esim_response(&e)))
}

async fn mark_downloaded(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::transition_esim(&s, &iccid, "download").await?;
    Ok(Json(schemas::to_esim_response(&e)))
}
async fn mark_activated(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::transition_esim(&s, &iccid, "activate").await?;
    Ok(Json(schemas::to_esim_response(&e)))
}
async fn recycle_esim(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::transition_esim(&s, &iccid, "recycle").await?;
    Ok(Json(schemas::to_esim_response(&e)))
}
async fn release_esim(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::transition_esim(&s, &iccid, "release").await?;
    Ok(Json(schemas::to_esim_response(&e)))
}

async fn esim_activation(
    State(s): State<AppState>,
    Path(iccid): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let e = svc::get_activation_code(&s, &iccid).await?;
    Ok(Json(schemas::to_esim_activation(&e)))
}
