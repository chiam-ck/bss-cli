//! CRM orchestration — port of the seven service classes. One module, sectioned by
//! domain. HTTP write paths run in a `pool.begin()` transaction, stage events on the
//! connection, commit, and re-read the aggregate for the response. IDs mirror the
//! oracle's `f"{PREFIX}-{uuid4().hex[:8]}"`. Interaction auto-logs and event stages
//! match the oracle call-for-call.

use bss_clients::{ClientError, SubscriptionClient};
use bss_context::RequestCtx;
use bss_db::PolicyViolation;
use chrono::NaiveDate;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::postgres::PgConnection;
use sqlx::{PgPool, Row};

use crate::error::ApiError;
use crate::events::stage;
use crate::policies as pol;
use crate::repo::{
    self, CaseFull, ContactMediumRow, CustomerFull, EsimRow, MsisdnRow, PortRequestRow, TicketRow,
};
use crate::state::AppState;

fn next_id(prefix: &str) -> String {
    format!(
        "{prefix}-{}",
        &uuid::Uuid::new_v4().simple().to_string()[..8]
    )
}

fn next_port_id() -> String {
    format!(
        "PORT-{}",
        uuid::Uuid::new_v4().simple().to_string()[..8].to_uppercase()
    )
}

fn upstream(e: ClientError) -> ApiError {
    ApiError::Internal(format!("upstream error: {e}"))
}

/// Insert an interaction row (the auto-log on customer/case/ticket writes).
#[allow(clippy::too_many_arguments)]
async fn log_interaction(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    customer_id: &str,
    summary: &str,
    related_case_id: Option<&str>,
    related_ticket_id: Option<&str>,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO crm.interaction \
         (id, customer_id, channel, direction, summary, related_case_id, related_ticket_id, occurred_at, tenant_id) \
         VALUES ($1,$2,$3,'inbound',$4,$5,$6,$7,$8)",
    )
    .bind(next_id("INT"))
    .bind(customer_id)
    .bind(&ctx.channel)
    .bind(summary)
    .bind(related_case_id)
    .bind(related_ticket_id)
    .bind(bss_clock::now())
    .bind(&ctx.tenant)
    .execute(conn)
    .await?;
    Ok(())
}

// ── customer ────────────────────────────────────────────────────────────────

pub struct NewContactMedium {
    pub medium_type: String,
    pub value: String,
    pub is_primary: bool,
}

#[allow(clippy::too_many_arguments)]
pub async fn create_customer(
    st: &AppState,
    ctx: &RequestCtx,
    given_name: &str,
    family_name: &str,
    date_of_birth: Option<&str>,
    contact_mediums: Vec<NewContactMedium>,
) -> Result<CustomerFull, ApiError> {
    pol::check_requires_contact_medium(contact_mediums.len())?;
    for cm in contact_mediums.iter().filter(|c| c.medium_type == "email") {
        if repo::find_customer_by_email(&st.pool, &cm.value)
            .await?
            .is_some()
        {
            return Err(PolicyViolation::with_context(
                "customer.create.email_unique",
                format!("Email '{}' is already registered", cm.value),
                json!({ "email": cm.value }),
            )
            .into());
        }
    }

    let now = bss_clock::now();
    let party_id = next_id("PTY");
    let customer_id = next_id("CUST");
    let dob: Option<NaiveDate> = date_of_birth
        .filter(|s| !s.is_empty())
        .map(|s| NaiveDate::parse_from_str(s, "%Y-%m-%d"))
        .transpose()
        .map_err(|e| ApiError::BadRequest(format!("bad date_of_birth: {e}")))?;

    let mut tx = st.pool.begin().await?;
    sqlx::query("INSERT INTO crm.party (id, party_type, tenant_id) VALUES ($1,'individual',$2)")
        .bind(&party_id)
        .bind(&ctx.tenant)
        .execute(&mut *tx)
        .await?;
    sqlx::query("INSERT INTO crm.individual (party_id, given_name, family_name, date_of_birth, tenant_id) VALUES ($1,$2,$3,$4,$5)")
        .bind(&party_id)
        .bind(given_name)
        .bind(family_name)
        .bind(dob)
        .bind(&ctx.tenant)
        .execute(&mut *tx)
        .await?;
    sqlx::query("INSERT INTO crm.customer (id, party_id, status, customer_since, tenant_id) VALUES ($1,$2,'active',$3,$4)")
        .bind(&customer_id)
        .bind(&party_id)
        .bind(now)
        .bind(&ctx.tenant)
        .execute(&mut *tx)
        .await?;
    for cm in &contact_mediums {
        sqlx::query("INSERT INTO crm.contact_medium (id, party_id, medium_type, value, is_primary, valid_from, tenant_id) VALUES ($1,$2,$3,$4,$5,$6,$7)")
            .bind(next_id("CM"))
            .bind(&party_id)
            .bind(&cm.medium_type)
            .bind(&cm.value)
            .bind(cm.is_primary)
            .bind(now)
            .bind(&ctx.tenant)
            .execute(&mut *tx)
            .await?;
    }
    stage(
        &mut tx,
        ctx,
        "customer.created",
        "customer",
        &customer_id,
        json!({ "given_name": given_name, "family_name": family_name }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        &customer_id,
        &format!("Customer created: {given_name} {family_name}"),
        None,
        None,
    )
    .await?;
    tx.commit().await?;

    // Best-effort loyalty registry mirror (never fails customer creation).
    if let Some(loyalty) = &st.loyalty {
        if let Err(e) = loyalty.register_customer(&customer_id).await {
            tracing::warn!(customer_id, error = %e, "crm.customer.loyalty_register_failed");
        }
    }

    repo::get_customer_full(&st.pool, &customer_id)
        .await?
        .ok_or_else(|| ApiError::Internal("customer vanished after create".into()))
}

pub async fn find_by_msisdn(st: &AppState, msisdn: &str) -> Result<Option<CustomerFull>, ApiError> {
    let Some(row) = repo::get_msisdn(&st.pool, msisdn).await? else {
        return Ok(None);
    };
    let Some(sub_id) = repo::msisdn_assigned_sub(&row) else {
        return Ok(None);
    };
    let subscription = match st.subscription.get(sub_id).await {
        Ok(v) => v,
        Err(_) => return Ok(None), // best-effort cross-service read
    };
    let Some(customer_id) = subscription.get("customerId").and_then(Value::as_str) else {
        return Ok(None);
    };
    repo::get_customer_full(&st.pool, customer_id).await
}

pub async fn find_by_email(st: &AppState, email: &str) -> Result<Option<CustomerFull>, ApiError> {
    match repo::find_customer_by_email(&st.pool, email).await? {
        Some(id) => repo::get_customer_full(&st.pool, &id).await,
        None => Ok(None),
    }
}

pub async fn update_customer(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    status: Option<&str>,
    status_reason: Option<&str>,
) -> Result<CustomerFull, ApiError> {
    let cust = repo::get_customer(&st.pool, customer_id)
        .await?
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "customer.update.not_found",
                format!("Customer {customer_id} not found"),
                json!({ "customer_id": customer_id }),
            ))
        })?;

    // Deactivation guard: cannot deactivate a customer with active subscriptions.
    if let Some(new_status) = status {
        if new_status != "active" && cust.status == "active" {
            let subs = st
                .subscription
                .list_for_customer(customer_id)
                .await
                .map_err(upstream)?;
            let active: Vec<String> = subs
                .as_array()
                .map(|arr| {
                    arr.iter()
                        .filter(|s| {
                            matches!(
                                s.get("state").and_then(Value::as_str),
                                Some("active") | Some("pending")
                            )
                        })
                        .filter_map(|s| s.get("id").and_then(Value::as_str).map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            if !active.is_empty() {
                return Err(PolicyViolation::with_context(
                    "customer.close.no_active_subscriptions",
                    format!("Customer {customer_id} has {} active subscription(s): {}. Terminate them first.", active.len(), active.join(", ")),
                    json!({ "customer_id": customer_id, "active_subscriptions": active }),
                )
                .into());
            }
        }
    }

    let mut fields = Vec::new();
    if status.is_some() {
        fields.push("status");
    }
    if status_reason.is_some() {
        fields.push("status_reason");
    }
    let mut tx = st.pool.begin().await?;
    if status.is_some() || status_reason.is_some() {
        sqlx::query(
            "UPDATE crm.customer SET status = COALESCE($2, status), status_reason = COALESCE($3, status_reason), updated_at = now() WHERE id = $1",
        )
        .bind(customer_id)
        .bind(status)
        .bind(status_reason)
        .execute(&mut *tx)
        .await?;
    }
    stage(
        &mut tx,
        ctx,
        "customer.updated",
        "customer",
        customer_id,
        json!({ "fields_updated": fields }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("Customer updated: {}", fields.join(", ")),
        None,
        None,
    )
    .await?;
    tx.commit().await?;
    repo::get_customer_full(&st.pool, customer_id)
        .await?
        .ok_or_else(|| ApiError::Internal("customer vanished".into()))
}

async fn require_customer_party(pool: &PgPool, customer_id: &str) -> Result<String, ApiError> {
    repo::get_customer(pool, customer_id)
        .await?
        .map(|c| c.party_id)
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "customer.contact.not_found",
                format!("Customer {customer_id} not found"),
                json!({ "customer_id": customer_id }),
            ))
        })
}

pub async fn add_contact_medium(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    medium_type: &str,
    value: &str,
    is_primary: bool,
) -> Result<ContactMediumRow, ApiError> {
    let party_id = require_customer_party(&st.pool, customer_id).await?;
    if medium_type == "email"
        && repo::find_customer_by_email(&st.pool, value)
            .await?
            .is_some()
    {
        return Err(PolicyViolation::with_context(
            "customer.create.email_unique",
            format!("Email '{value}' is already registered"),
            json!({ "email": value }),
        )
        .into());
    }
    let cm_id = next_id("CM");
    let now = bss_clock::now();
    let mut tx = st.pool.begin().await?;
    sqlx::query("INSERT INTO crm.contact_medium (id, party_id, medium_type, value, is_primary, valid_from, tenant_id) VALUES ($1,$2,$3,$4,$5,$6,$7)")
        .bind(&cm_id)
        .bind(&party_id)
        .bind(medium_type)
        .bind(value)
        .bind(is_primary)
        .bind(now)
        .bind(&ctx.tenant)
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "customer.contact_medium_added",
        "customer",
        customer_id,
        json!({ "medium_type": medium_type }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("Contact medium added: {medium_type}"),
        None,
        None,
    )
    .await?;
    tx.commit().await?;
    repo::get_contact_medium(&st.pool, &cm_id)
        .await?
        .ok_or_else(|| ApiError::Internal("cm vanished".into()))
}

pub async fn remove_contact_medium(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    cm_id: &str,
) -> Result<(), ApiError> {
    let party_id = require_customer_party(&st.pool, customer_id).await?;
    let cm = repo::get_contact_medium(&st.pool, cm_id).await?;
    if cm.as_ref().map(|c| c.party_id.as_str()) != Some(party_id.as_str()) {
        return Err(PolicyViolation::with_context(
            "customer.contact.medium_not_found",
            format!("Contact medium {cm_id} not found for customer {customer_id}"),
            json!({ "customer_id": customer_id, "cm_id": cm_id }),
        )
        .into());
    }
    let mut tx = st.pool.begin().await?;
    sqlx::query("DELETE FROM crm.contact_medium WHERE id = $1")
        .bind(cm_id)
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "customer.contact_medium_removed",
        "customer",
        customer_id,
        json!({ "cm_id": cm_id }),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn update_contact_medium(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    cm_id: &str,
    value: &str,
) -> Result<ContactMediumRow, ApiError> {
    let party_id = require_customer_party(&st.pool, customer_id).await?;
    let cm = repo::get_contact_medium(&st.pool, cm_id).await?;
    let cm = match cm {
        Some(c) if c.party_id == party_id => c,
        _ => {
            return Err(PolicyViolation::with_context(
                "customer.contact_medium.unknown",
                format!("Contact medium {cm_id} not found for customer {customer_id}"),
                json!({ "customer_id": customer_id, "cm_id": cm_id }),
            )
            .into())
        }
    };
    if cm.medium_type == "email" {
        return Err(PolicyViolation::with_context(
            "customer.contact_medium.email_must_use_change_flow",
            "Email updates must use the verified email-change flow (start_email_change → verify_email_change), not the direct contact-medium update.",
            json!({ "customer_id": customer_id, "cm_id": cm_id }),
        )
        .into());
    }
    let mut tx = st.pool.begin().await?;
    sqlx::query("UPDATE crm.contact_medium SET value = $2, updated_at = now() WHERE id = $1")
        .bind(cm_id)
        .bind(value)
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "customer.contact_medium_updated",
        "customer",
        customer_id,
        json!({ "cm_id": cm_id, "medium_type": cm.medium_type }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("Contact medium updated: {}", cm.medium_type),
        None,
        None,
    )
    .await?;
    tx.commit().await?;
    repo::get_contact_medium(&st.pool, cm_id)
        .await?
        .ok_or_else(|| ApiError::Internal("cm vanished".into()))
}

pub async fn update_individual_name(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    given_name: Option<&str>,
    family_name: Option<&str>,
) -> Result<(), ApiError> {
    if given_name.is_none() && family_name.is_none() {
        return Err(PolicyViolation::with_context(
            "customer.individual.update.no_fields",
            "At least one of given_name or family_name is required",
            json!({ "customer_id": customer_id }),
        )
        .into());
    }
    let party_id = require_customer_party(&st.pool, customer_id).await?;
    let mut tx = st.pool.begin().await?;
    let res = sqlx::query(
        "UPDATE crm.individual SET given_name = COALESCE($2, given_name), family_name = COALESCE($3, family_name), updated_at = now() WHERE party_id = $1",
    )
    .bind(&party_id)
    .bind(given_name)
    .bind(family_name)
    .execute(&mut *tx)
    .await?;
    if res.rows_affected() == 0 {
        return Err(PolicyViolation::with_context(
            "customer.individual.not_found",
            format!("Customer {customer_id} has no individual record"),
            json!({ "customer_id": customer_id }),
        )
        .into());
    }
    let fields: Vec<&str> = [
        ("given_name", given_name.is_some()),
        ("family_name", family_name.is_some()),
    ]
    .into_iter()
    .filter(|(_, p)| *p)
    .map(|(n, _)| n)
    .collect();
    stage(
        &mut tx,
        ctx,
        "customer.individual_updated",
        "customer",
        customer_id,
        json!({ "fields": fields }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        "Customer updated their display name",
        None,
        None,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

// ── case ────────────────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub async fn open_case(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    subject: &str,
    description: Option<&str>,
    priority: &str,
    category: &str,
    opened_by_agent_id: Option<&str>,
    chat_transcript_hash: Option<&str>,
) -> Result<CaseFull, ApiError> {
    // Policy: customer active.
    let cust = repo::get_customer(&st.pool, customer_id).await?;
    match cust {
        None => {
            return Err(PolicyViolation::with_context(
                "case.open.customer_must_be_active",
                format!("Customer {customer_id} does not exist"),
                json!({ "customer_id": customer_id }),
            )
            .into())
        }
        Some(c) if c.status != "active" => {
            return Err(PolicyViolation::with_context(
                "case.open.customer_must_be_active",
                format!("Customer {customer_id} is not active (status={})", c.status),
                json!({ "customer_id": customer_id, "status": c.status }),
            )
            .into())
        }
        _ => {}
    }
    let now = bss_clock::now();
    let case_id = next_id("CASE");
    let mut tx = st.pool.begin().await?;
    sqlx::query(
        "INSERT INTO crm.\"case\" (id, customer_id, subject, description, state, priority, category, opened_by_agent_id, opened_at, tenant_id, chat_transcript_hash) \
         VALUES ($1,$2,$3,$4,'open',$5,$6,$7,$8,$9,$10)",
    )
    .bind(&case_id)
    .bind(customer_id)
    .bind(subject)
    .bind(description)
    .bind(priority)
    .bind(category)
    .bind(opened_by_agent_id)
    .bind(now)
    .bind(&ctx.tenant)
    .bind(chat_transcript_hash)
    .execute(&mut *tx)
    .await?;
    stage(
        &mut tx,
        ctx,
        "case.opened",
        "case",
        &case_id,
        json!({ "customer_id": customer_id, "subject": subject }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("Case opened: {subject}"),
        Some(&case_id),
        None,
    )
    .await?;
    tx.commit().await?;
    repo::get_case_full(&st.pool, &case_id)
        .await?
        .ok_or_else(|| ApiError::Internal("case vanished".into()))
}

pub async fn transition_case(
    st: &AppState,
    ctx: &RequestCtx,
    case_id: &str,
    trigger: &str,
    resolution_code: Option<&str>,
) -> Result<(), ApiError> {
    let case = repo::get_case(&st.pool, case_id).await?.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "case.not_found",
            format!("Case {case_id} not found"),
            json!({ "case_id": case_id }),
        ))
    })?;
    pol::check_case_transition(&case.state, trigger)?;

    let mut tx = st.pool.begin().await?;
    let mut resolution = case.resolution_code.clone();
    if trigger == "resolve" {
        let open = repo::find_open_by_case(&mut tx, case_id).await?;
        if !open.is_empty() {
            let ids: Vec<String> = open.iter().map(|t| t.id.clone()).collect();
            return Err(PolicyViolation::with_context(
                "case.close.requires_all_tickets_resolved",
                format!(
                    "Case {case_id} has {} open tickets: {}",
                    ids.len(),
                    ids.join(", ")
                ),
                json!({ "case_id": case_id, "open_tickets": ids }),
            )
            .into());
        }
    } else if trigger == "close" {
        let code = resolution_code.or(case.resolution_code.as_deref());
        pol::check_resolution_code(code)?;
        resolution = code.map(str::to_string);
    }

    let new_state = crate::domain::case::get_next_state(&case.state, trigger)
        .ok_or_else(|| ApiError::Internal("case transition produced no state".into()))?;
    let now = bss_clock::now();
    let closed_at = if new_state == "closed" {
        Some(now)
    } else {
        None
    };
    sqlx::query("UPDATE crm.\"case\" SET state = $2, resolution_code = $3, closed_at = COALESCE($4, closed_at), updated_at = now() WHERE id = $1")
        .bind(case_id)
        .bind(new_state)
        .bind(&resolution)
        .bind(closed_at)
        .execute(&mut *tx)
        .await?;
    // Cancel open tickets on cancel → closed.
    if new_state == "closed" && trigger == "cancel" {
        let open = repo::find_open_by_case(&mut tx, case_id).await?;
        for t in open {
            sqlx::query("UPDATE crm.ticket SET state = 'cancelled', closed_at = $2, updated_at = now() WHERE id = $1")
                .bind(&t.id)
                .bind(now)
                .execute(&mut *tx)
                .await?;
        }
    }
    stage(
        &mut tx,
        ctx,
        &format!("case.{trigger}"),
        "case",
        case_id,
        json!({ "from_state": case.state, "to_state": new_state, "trigger": trigger }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        &case.customer_id,
        &format!("Case {trigger}: {} → {new_state}", case.state),
        Some(case_id),
        None,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn close_case(
    st: &AppState,
    ctx: &RequestCtx,
    case_id: &str,
    resolution_code: &str,
) -> Result<(), ApiError> {
    let case = repo::get_case(&st.pool, case_id).await?.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "case.not_found",
            format!("Case {case_id} not found"),
            json!({ "case_id": case_id }),
        ))
    })?;
    if crate::domain::case::is_valid_transition(&case.state, "resolve") {
        transition_case(st, ctx, case_id, "resolve", None).await?;
    }
    transition_case(st, ctx, case_id, "close", Some(resolution_code)).await
}

pub async fn update_case_fields(
    st: &AppState,
    ctx: &RequestCtx,
    case_id: &str,
    priority: Option<&str>,
    category: Option<&str>,
) -> Result<(), ApiError> {
    let case = repo::get_case(&st.pool, case_id).await?.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "case.not_found",
            format!("Case {case_id} not found"),
            json!({ "case_id": case_id }),
        ))
    })?;
    pol::check_case_not_closed(case_id, &case.state)?;
    if let Some(p) = priority {
        pol::check_priority_valid(p)?;
    }
    let mut changed = serde_json::Map::new();
    if let Some(p) = priority {
        if Some(p) != case.priority.as_deref() {
            changed.insert("priority".into(), json!({ "from": case.priority, "to": p }));
        }
    }
    if let Some(c) = category {
        if Some(c) != case.category.as_deref() {
            changed.insert("category".into(), json!({ "from": case.category, "to": c }));
        }
    }
    if changed.is_empty() {
        return Ok(());
    }
    let mut tx = st.pool.begin().await?;
    sqlx::query("UPDATE crm.\"case\" SET priority = COALESCE($2, priority), category = COALESCE($3, category), updated_at = now() WHERE id = $1")
        .bind(case_id)
        .bind(priority.filter(|_| changed.contains_key("priority")))
        .bind(category.filter(|_| changed.contains_key("category")))
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "case.updated",
        "case",
        case_id,
        json!({ "changed": changed }),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn add_note(
    st: &AppState,
    ctx: &RequestCtx,
    case_id: &str,
    body: &str,
    author_agent_id: Option<&str>,
) -> Result<Value, ApiError> {
    let case = repo::get_case(&st.pool, case_id).await?.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "case.not_found",
            format!("Case {case_id} not found"),
            json!({ "case_id": case_id }),
        ))
    })?;
    if case.state == "closed" {
        return Err(PolicyViolation::with_context(
            "case.add_note.case_is_closed",
            format!("Case {case_id} is closed; cannot add notes"),
            json!({ "case_id": case_id, "state": case.state }),
        )
        .into());
    }
    let note_id = next_id("NOTE");
    let mut tx = st.pool.begin().await?;
    sqlx::query("INSERT INTO crm.case_note (id, case_id, author_agent_id, body, tenant_id) VALUES ($1,$2,$3,$4,$5)")
        .bind(&note_id)
        .bind(case_id)
        .bind(author_agent_id)
        .bind(body)
        .bind(&ctx.tenant)
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "case.note_added",
        "case",
        case_id,
        json!({ "note_id": note_id }),
    )
    .await?;
    tx.commit().await?;
    // Re-read the note for the response.
    let full = repo::get_case_full(&st.pool, case_id)
        .await?
        .ok_or_else(|| ApiError::Internal("case vanished".into()))?;
    let note = full
        .notes
        .iter()
        .find(|n| n.id == note_id)
        .ok_or_else(|| ApiError::Internal("note vanished".into()))?;
    Ok(crate::schemas::to_case_note_response(note))
}

// ── ticket ──────────────────────────────────────────────────────────────────

async fn ticket_history(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    ticket_id: &str,
    from: Option<&str>,
    to: &str,
    changed_by: Option<&str>,
    reason: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO crm.ticket_state_history (ticket_id, from_state, to_state, changed_by_agent_id, reason, tenant_id) VALUES ($1,$2,$3,$4,$5,$6)",
    )
    .bind(ticket_id)
    .bind(from)
    .bind(to)
    .bind(changed_by)
    .bind(reason)
    .bind(&ctx.tenant)
    .execute(conn)
    .await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub async fn open_ticket(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    subject: &str,
    description: Option<&str>,
    ticket_type: &str,
    priority: &str,
    case_id: Option<&str>,
    assigned_to_agent_id: Option<&str>,
) -> Result<TicketRow, ApiError> {
    if repo::get_customer(&st.pool, customer_id).await?.is_none() {
        return Err(PolicyViolation::with_context(
            "ticket.open.requires_customer",
            format!("Customer {customer_id} does not exist"),
            json!({ "customer_id": customer_id }),
        )
        .into());
    }
    if let Some(a) = assigned_to_agent_id {
        check_agent_active(&st.pool, a).await?;
    }
    if let Some(cid) = case_id {
        if repo::get_case(&st.pool, cid).await?.is_none() {
            return Err(PolicyViolation::with_context(
                "ticket.open.case_not_found",
                format!("Case {cid} not found"),
                json!({ "case_id": cid }),
            )
            .into());
        }
    }
    let now = bss_clock::now();
    let ticket_id = next_id("TKT");
    let mut tx = st.pool.begin().await?;
    sqlx::query(
        "INSERT INTO crm.ticket (id, case_id, customer_id, ticket_type, subject, description, state, priority, assigned_to_agent_id, opened_at, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,'open',$7,$8,$9,$10)",
    )
    .bind(&ticket_id)
    .bind(case_id)
    .bind(customer_id)
    .bind(ticket_type)
    .bind(subject)
    .bind(description)
    .bind(priority)
    .bind(assigned_to_agent_id)
    .bind(now)
    .bind(&ctx.tenant)
    .execute(&mut *tx)
    .await?;
    ticket_history(
        &mut tx,
        ctx,
        &ticket_id,
        None,
        "open",
        None,
        "Ticket created",
    )
    .await?;
    stage(
        &mut tx,
        ctx,
        "ticket.opened",
        "ticket",
        &ticket_id,
        json!({ "customer_id": customer_id, "case_id": case_id, "subject": subject }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("Ticket opened: {subject}"),
        case_id,
        Some(&ticket_id),
    )
    .await?;
    tx.commit().await?;
    repo::get_ticket(&st.pool, &ticket_id)
        .await?
        .ok_or_else(|| ApiError::Internal("ticket vanished".into()))
}

#[allow(clippy::too_many_arguments)]
pub async fn transition_ticket(
    st: &AppState,
    ctx: &RequestCtx,
    ticket_id: &str,
    trigger: &str,
    assigned_to_agent_id: Option<&str>,
    resolution_notes: Option<&str>,
    reason: Option<&str>,
) -> Result<TicketRow, ApiError> {
    let ticket = repo::get_ticket(&st.pool, ticket_id)
        .await?
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "ticket.not_found",
                format!("Ticket {ticket_id} not found"),
                json!({ "ticket_id": ticket_id }),
            ))
        })?;

    if trigger == "cancel" {
        pol::check_ticket_cancel_allowed(&ticket.state)?;
    } else {
        pol::check_ticket_transition(&ticket.state, trigger)?;
    }

    let mut new_assignee = ticket.assigned_to_agent_id.clone();
    if trigger == "ack" && ticket.assigned_to_agent_id.is_none() {
        let agent_id = assigned_to_agent_id.filter(|a| !a.is_empty());
        let Some(agent_id) = agent_id else {
            return Err(PolicyViolation::with_context(
                "ticket.ack.requires_agent",
                "Acknowledging requires an assigned agent",
                json!({ "ticket_id": ticket_id }),
            )
            .into());
        };
        check_agent_active(&st.pool, agent_id).await?;
        new_assignee = Some(agent_id.to_string());
    }
    let mut resolution = ticket.resolution_notes.clone();
    let mut resolved_at = ticket.resolved_at;
    let now = bss_clock::now();
    if trigger == "resolve" {
        pol::check_resolution_notes(resolution_notes)?;
        resolution = resolution_notes.map(str::to_string);
        resolved_at = Some(now);
    }
    let new_state = crate::domain::ticket::get_next_state(&ticket.state, trigger)
        .ok_or_else(|| ApiError::Internal("ticket transition produced no state".into()))?;
    let closed_at = if new_state == "closed" || new_state == "cancelled" {
        Some(now)
    } else {
        ticket.closed_at
    };

    let mut tx = st.pool.begin().await?;
    sqlx::query(
        "UPDATE crm.ticket SET state=$2, assigned_to_agent_id=$3, resolution_notes=$4, resolved_at=$5, closed_at=$6, updated_at=now() WHERE id=$1",
    )
    .bind(ticket_id)
    .bind(new_state)
    .bind(&new_assignee)
    .bind(&resolution)
    .bind(resolved_at)
    .bind(closed_at)
    .execute(&mut *tx)
    .await?;
    let hist_changed_by = new_assignee.as_deref();
    let hist_reason = reason
        .map(str::to_string)
        .unwrap_or_else(|| format!("Trigger: {trigger}"));
    ticket_history(
        &mut tx,
        ctx,
        ticket_id,
        Some(&ticket.state),
        new_state,
        hist_changed_by,
        &hist_reason,
    )
    .await?;
    stage(
        &mut tx,
        ctx,
        &format!("ticket.{trigger}"),
        "ticket",
        ticket_id,
        json!({ "from_state": ticket.state, "to_state": new_state, "trigger": trigger }),
    )
    .await?;
    log_interaction(
        &mut tx,
        ctx,
        &ticket.customer_id,
        &format!("Ticket {trigger}: {} → {new_state}", ticket.state),
        ticket.case_id.as_deref(),
        Some(ticket_id),
    )
    .await?;
    tx.commit().await?;
    repo::get_ticket(&st.pool, ticket_id)
        .await?
        .ok_or_else(|| ApiError::Internal("ticket vanished".into()))
}

pub async fn update_ticket(
    st: &AppState,
    _ctx: &RequestCtx,
    ticket_id: &str,
    priority: Option<&str>,
    assigned_to_agent_id: Option<&str>,
    description: Option<&str>,
) -> Result<TicketRow, ApiError> {
    if repo::get_ticket(&st.pool, ticket_id).await?.is_none() {
        return Err(PolicyViolation::with_context(
            "ticket.not_found",
            format!("Ticket {ticket_id} not found"),
            json!({ "ticket_id": ticket_id }),
        )
        .into());
    }
    if let Some(a) = assigned_to_agent_id.filter(|a| !a.is_empty()) {
        check_agent_active(&st.pool, a).await?;
    }
    let mut tx = st.pool.begin().await?;
    sqlx::query(
        "UPDATE crm.ticket SET priority=COALESCE($2,priority), assigned_to_agent_id=COALESCE($3,assigned_to_agent_id), description=COALESCE($4,description), updated_at=now() WHERE id=$1",
    )
    .bind(ticket_id)
    .bind(priority)
    .bind(assigned_to_agent_id)
    .bind(description)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    repo::get_ticket(&st.pool, ticket_id)
        .await?
        .ok_or_else(|| ApiError::Internal("ticket vanished".into()))
}

async fn check_agent_active(pool: &PgPool, agent_id: &str) -> Result<(), ApiError> {
    let agent = repo::get_agent(pool, agent_id).await?;
    match agent {
        None => Err(PolicyViolation::with_context(
            "ticket.assign.agent_must_be_active",
            format!("Agent {agent_id} does not exist"),
            json!({ "agent_id": agent_id }),
        )
        .into()),
        Some(a) if a.status != "active" => Err(PolicyViolation::with_context(
            "ticket.assign.agent_must_be_active",
            format!("Agent {agent_id} is not active (status={})", a.status),
            json!({ "agent_id": agent_id, "status": a.status }),
        )
        .into()),
        _ => Ok(()),
    }
}

// ── inventory ───────────────────────────────────────────────────────────────

/// Post-commit fire-and-forget pool-low check (best-effort; not transactional).
async fn maybe_emit_pool_low(st: &AppState, tenant: &str) {
    let available: Result<i64, sqlx::Error> = sqlx::query_scalar(
        "SELECT COUNT(*) FROM inventory.msisdn_pool WHERE status = 'available' AND tenant_id = $1",
    )
    .bind(tenant)
    .fetch_one(&st.pool)
    .await;
    let Ok(available) = available else {
        tracing::warn!("inventory.msisdn.pool_low.emit_failed");
        return;
    };
    let threshold = st.settings.msisdn_pool_low_threshold;
    if available <= threshold {
        let ctx = RequestCtx::default();
        let mut tx = match st.pool.begin().await {
            Ok(t) => t,
            Err(_) => return,
        };
        if stage(
            &mut tx,
            &ctx,
            "inventory.msisdn.pool_low",
            "msisdn_pool",
            "DEFAULT",
            json!({ "available": available, "threshold": threshold }),
        )
        .await
        .is_ok()
        {
            let _ = tx.commit().await;
            tracing::warn!(available, threshold, "inventory.msisdn.pool_low");
        }
    }
}

pub async fn reserve_msisdn(
    st: &AppState,
    ctx: &RequestCtx,
    msisdn: &str,
) -> Result<MsisdnRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let picked: Option<String> = sqlx::query_scalar(
        "SELECT msisdn FROM inventory.msisdn_pool WHERE msisdn = $1 AND status = 'available' AND tenant_id = $2 FOR UPDATE SKIP LOCKED",
    )
    .bind(msisdn)
    .bind(&ctx.tenant)
    .fetch_optional(&mut *tx)
    .await?;
    if picked.is_none() {
        return Err(PolicyViolation::with_context(
            "msisdn.reserve.status_must_be_available",
            format!("MSISDN {msisdn} is not available for reservation"),
            json!({ "msisdn": msisdn }),
        )
        .into());
    }
    sqlx::query("UPDATE inventory.msisdn_pool SET status='reserved', reserved_at=$2, updated_at=$2 WHERE msisdn=$1")
        .bind(msisdn)
        .bind(bss_clock::now())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    maybe_emit_pool_low(st, &ctx.tenant).await;
    repo::get_msisdn(&st.pool, msisdn)
        .await?
        .ok_or_else(|| ApiError::Internal("msisdn vanished".into()))
}

pub async fn reserve_next_msisdn(
    st: &AppState,
    ctx: &RequestCtx,
    preference: Option<&str>,
) -> Result<MsisdnRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let picked: Option<String> = match preference {
        Some(p) => {
            sqlx::query_scalar("SELECT msisdn FROM inventory.msisdn_pool WHERE msisdn=$1 AND status='available' AND tenant_id=$2 FOR UPDATE SKIP LOCKED")
                .bind(p)
                .bind(&ctx.tenant)
                .fetch_optional(&mut *tx)
                .await?
        }
        None => {
            sqlx::query_scalar("SELECT msisdn FROM inventory.msisdn_pool WHERE status='available' AND tenant_id=$1 ORDER BY msisdn LIMIT 1 FOR UPDATE SKIP LOCKED")
                .bind(&ctx.tenant)
                .fetch_optional(&mut *tx)
                .await?
        }
    };
    let Some(msisdn) = picked else {
        return Err(PolicyViolation::with_context(
            "msisdn.reserve.no_available",
            "No MSISDN available matching criteria",
            json!({ "preference": preference }),
        )
        .into());
    };
    sqlx::query("UPDATE inventory.msisdn_pool SET status='reserved', reserved_at=$2, updated_at=$2 WHERE msisdn=$1")
        .bind(&msisdn)
        .bind(bss_clock::now())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    maybe_emit_pool_low(st, &ctx.tenant).await;
    repo::get_msisdn(&st.pool, &msisdn)
        .await?
        .ok_or_else(|| ApiError::Internal("msisdn vanished".into()))
}

pub async fn assign_msisdn(st: &AppState, msisdn: &str) -> Result<MsisdnRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let row = repo::get_msisdn_conn(&mut tx, msisdn).await?;
    let row = row.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "msisdn.assign.not_found",
            format!("MSISDN {msisdn} not found"),
            json!({ "msisdn": msisdn }),
        ))
    })?;
    if row.status != "reserved" && row.status != "assigned" {
        return Err(PolicyViolation::with_context(
            "msisdn.assign.must_be_reserved",
            format!("MSISDN {msisdn} must be reserved before assignment"),
            json!({ "msisdn": msisdn, "status": row.status }),
        )
        .into());
    }
    sqlx::query(
        "UPDATE inventory.msisdn_pool SET status='assigned', updated_at=now() WHERE msisdn=$1",
    )
    .bind(msisdn)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    repo::get_msisdn(&st.pool, msisdn)
        .await?
        .ok_or_else(|| ApiError::Internal("msisdn vanished".into()))
}

pub async fn release_msisdn(st: &AppState, msisdn: &str) -> Result<MsisdnRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let row = repo::get_msisdn_conn(&mut tx, msisdn).await?;
    let row = row.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "msisdn.release.not_found",
            format!("MSISDN {msisdn} not found"),
            json!({ "msisdn": msisdn }),
        ))
    })?;
    pol::check_msisdn_releasable(&row.status, msisdn)?;
    sqlx::query("UPDATE inventory.msisdn_pool SET status='available', reserved_at=NULL, assigned_to_subscription_id=NULL, updated_at=now() WHERE msisdn=$1")
        .bind(msisdn)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    repo::get_msisdn(&st.pool, msisdn)
        .await?
        .ok_or_else(|| ApiError::Internal("msisdn vanished".into()))
}

pub async fn add_msisdn_range(
    st: &AppState,
    ctx: &RequestCtx,
    prefix: &str,
    count: i64,
) -> Result<Value, ApiError> {
    pol::check_sane_prefix(prefix, count)?;
    let mut tx = st.pool.begin().await?;
    let mut inserted = 0i64;
    for i in 0..count {
        let m = format!("{prefix}{i:04}");
        let res = sqlx::query("INSERT INTO inventory.msisdn_pool (msisdn, status, tenant_id) VALUES ($1,'available',$2) ON CONFLICT (msisdn) DO NOTHING")
            .bind(&m)
            .bind(&ctx.tenant)
            .execute(&mut *tx)
            .await?;
        inserted += res.rows_affected() as i64;
    }
    let first = format!("{prefix}{:04}", 0);
    let last = format!("{prefix}{:04}", count - 1);
    let skipped = count - inserted;
    stage(&mut tx, ctx, "inventory.msisdn.range_added", "msisdn_pool", prefix, json!({ "prefix": prefix, "count": count, "inserted": inserted, "skipped": skipped, "first": first, "last": last })).await?;
    tx.commit().await?;
    tracing::info!(
        prefix,
        count,
        inserted,
        skipped,
        "inventory.msisdn.range_added"
    );
    Ok(
        json!({ "prefix": prefix, "count": count, "inserted": inserted, "skipped": skipped, "first": first, "last": last }),
    )
}

pub async fn count_msisdns(
    st: &AppState,
    ctx: &RequestCtx,
    prefix: Option<&str>,
) -> Result<Value, ApiError> {
    let rows = match prefix {
        Some(p) => {
            sqlx::query("SELECT status, COUNT(*) AS n FROM inventory.msisdn_pool WHERE tenant_id=$1 AND msisdn LIKE $2 GROUP BY status")
                .bind(&ctx.tenant)
                .bind(format!("{p}%"))
                .fetch_all(&st.pool)
                .await?
        }
        None => {
            sqlx::query("SELECT status, COUNT(*) AS n FROM inventory.msisdn_pool WHERE tenant_id=$1 GROUP BY status")
                .bind(&ctx.tenant)
                .fetch_all(&st.pool)
                .await?
        }
    };
    let mut counts = std::collections::BTreeMap::new();
    for r in &rows {
        let status: String = r.try_get("status")?;
        let n: i64 = r.try_get("n")?;
        counts.insert(status, n);
    }
    for canonical in ["available", "reserved", "assigned", "ported_out"] {
        counts.entry(canonical.to_string()).or_insert(0);
    }
    let total: i64 = counts.values().sum();
    Ok(json!({
        "available": counts.get("available").copied().unwrap_or(0),
        "reserved": counts.get("reserved").copied().unwrap_or(0),
        "assigned": counts.get("assigned").copied().unwrap_or(0),
        "ported_out": counts.get("ported_out").copied().unwrap_or(0),
        "total": total,
        "prefix": prefix,
    }))
}

pub async fn reserve_esim(st: &AppState, ctx: &RequestCtx) -> Result<EsimRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let picked: Option<String> = sqlx::query_scalar(
        "SELECT iccid FROM inventory.esim_profile WHERE profile_state='available' AND tenant_id=$1 ORDER BY iccid LIMIT 1 FOR UPDATE SKIP LOCKED",
    )
    .bind(&ctx.tenant)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(iccid) = picked else {
        return Err(PolicyViolation::with_context(
            "esim.reserve.status_must_be_available",
            "No eSIM profile available for reservation",
            json!({}),
        )
        .into());
    };
    sqlx::query("UPDATE inventory.esim_profile SET profile_state='reserved', reserved_at=$2, updated_at=$2 WHERE iccid=$1")
        .bind(&iccid)
        .bind(bss_clock::now())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    repo::get_esim(&st.pool, &iccid)
        .await?
        .ok_or_else(|| ApiError::Internal("esim vanished".into()))
}

pub async fn assign_msisdn_to_esim(
    st: &AppState,
    iccid: &str,
    msisdn: &str,
) -> Result<EsimRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let esim = repo::get_esim_conn(&mut tx, iccid).await?;
    let esim = esim.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "esim.not_found",
            format!("eSIM {iccid} not found"),
            json!({ "iccid": iccid }),
        ))
    })?;
    if esim.profile_state != "reserved" {
        return Err(PolicyViolation::with_context(
            "esim.assign_msisdn.esim_must_be_reserved",
            format!("eSIM {iccid} must be reserved to assign MSISDN"),
            json!({ "iccid": iccid, "state": esim.profile_state }),
        )
        .into());
    }
    let m = repo::get_msisdn_conn(&mut tx, msisdn).await?;
    let m = m.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "esim.assign_msisdn.msisdn_not_found",
            format!("MSISDN {msisdn} not found"),
            json!({ "msisdn": msisdn }),
        ))
    })?;
    pol::check_msisdn_reserved_for_assign(&m.status, msisdn)?;
    sqlx::query("UPDATE inventory.esim_profile SET profile_state='reserved', assigned_msisdn=$2, updated_at=now() WHERE iccid=$1")
        .bind(iccid)
        .bind(msisdn)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    repo::get_esim(&st.pool, iccid)
        .await?
        .ok_or_else(|| ApiError::Internal("esim vanished".into()))
}

pub async fn transition_esim(
    st: &AppState,
    iccid: &str,
    trigger: &str,
) -> Result<EsimRow, ApiError> {
    let mut tx = st.pool.begin().await?;
    let esim = repo::get_esim_conn(&mut tx, iccid).await?;
    let esim = esim.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "esim.not_found",
            format!("eSIM {iccid} not found"),
            json!({ "iccid": iccid }),
        ))
    })?;
    if !crate::domain::esim::is_valid_transition(&esim.profile_state, trigger) {
        return Err(PolicyViolation::with_context(
            "esim.transition.invalid",
            format!(
                "Cannot '{trigger}' eSIM from state '{}'",
                esim.profile_state
            ),
            json!({ "iccid": iccid, "state": esim.profile_state, "trigger": trigger }),
        )
        .into());
    }
    let new_state = crate::domain::esim::get_next_state(&esim.profile_state, trigger)
        .ok_or_else(|| ApiError::Internal("esim transition produced no state".into()))?;
    let now = bss_clock::now();
    // Per-state side-effect columns (mirrors EsimRepository.update_state).
    match new_state {
        "downloaded" => {
            sqlx::query("UPDATE inventory.esim_profile SET profile_state=$2, downloaded_at=$3, updated_at=now() WHERE iccid=$1").bind(iccid).bind(new_state).bind(now).execute(&mut *tx).await?;
        }
        "activated" => {
            sqlx::query("UPDATE inventory.esim_profile SET profile_state=$2, activated_at=$3, updated_at=now() WHERE iccid=$1").bind(iccid).bind(new_state).bind(now).execute(&mut *tx).await?;
        }
        "available" => {
            sqlx::query("UPDATE inventory.esim_profile SET profile_state=$2, reserved_at=NULL, assigned_msisdn=NULL, assigned_to_subscription_id=NULL, updated_at=now() WHERE iccid=$1").bind(iccid).bind(new_state).execute(&mut *tx).await?;
        }
        _ => {
            sqlx::query("UPDATE inventory.esim_profile SET profile_state=$2, updated_at=now() WHERE iccid=$1").bind(iccid).bind(new_state).execute(&mut *tx).await?;
        }
    }
    tx.commit().await?;
    repo::get_esim(&st.pool, iccid)
        .await?
        .ok_or_else(|| ApiError::Internal("esim vanished".into()))
}

pub async fn get_activation_code(st: &AppState, iccid: &str) -> Result<EsimRow, ApiError> {
    repo::get_esim(&st.pool, iccid).await?.ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "esim.not_found",
            format!("eSIM {iccid} not found"),
            json!({ "iccid": iccid }),
        ))
    })
}

// ── interaction ─────────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub async fn create_interaction(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    channel: Option<&str>,
    direction: &str,
    summary: &str,
    body: Option<&str>,
    agent_id: Option<&str>,
    related_case_id: Option<&str>,
    related_ticket_id: Option<&str>,
) -> Result<Value, ApiError> {
    let id = next_id("INT");
    let now = bss_clock::now();
    let ch = channel
        .filter(|c| !c.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| ctx.channel.clone());
    let mut tx = st.pool.begin().await?;
    sqlx::query(
        "INSERT INTO crm.interaction (id, customer_id, channel, direction, summary, body, agent_id, related_case_id, related_ticket_id, occurred_at, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
    )
    .bind(&id)
    .bind(customer_id)
    .bind(&ch)
    .bind(direction)
    .bind(summary)
    .bind(body)
    .bind(agent_id)
    .bind(related_case_id)
    .bind(related_ticket_id)
    .bind(now)
    .bind(&ctx.tenant)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    let rows = repo::list_interactions(&st.pool, customer_id, 50, 0).await?;
    let row = rows
        .into_iter()
        .find(|r| r.id == id)
        .ok_or_else(|| ApiError::Internal("interaction vanished".into()))?;
    Ok(crate::schemas::to_tmf683_interaction(&row))
}

// ── KYC ─────────────────────────────────────────────────────────────────────

fn allow_prebaked() -> bool {
    match std::env::var("BSS_KYC_ALLOW_PREBAKED")
        .ok()
        .filter(|v| !v.is_empty())
    {
        Some(v) => matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"),
        None => std::env::var("BSS_ENV").unwrap_or_default().to_lowercase() != "production",
    }
}

fn allow_doc_reuse() -> bool {
    std::env::var("BSS_KYC_ALLOW_DOC_REUSE")
        .ok()
        .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false)
}

#[allow(clippy::too_many_arguments)]
pub async fn attest_kyc(
    st: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    provider: &str,
    provider_reference: &str,
    document_type: &str,
    document_number: Option<&str>,
    document_number_last4: Option<&str>,
    document_number_hash: Option<&str>,
    document_country: &str,
    date_of_birth: &str,
    nationality: Option<&str>,
    verified_at: &str,
    attestation_payload: &Value,
    corroboration_id: Option<&str>,
) -> Result<Value, ApiError> {
    // Reduce raw → last4 + hash if the pre-reduced form wasn't supplied.
    let (last4, hash) = match (document_number_last4, document_number_hash) {
        (Some(l), Some(h)) => (l.to_string(), h.to_string()),
        _ => {
            let Some(raw) = document_number else {
                return Err(PolicyViolation::with_context(
                    "customer.attest_kyc.document_required",
                    "Either document_number (legacy) or (document_number_last4 + document_number_hash) must be supplied",
                    json!({ "provider": provider }),
                )
                .into());
            };
            let normalized = raw.to_uppercase();
            let normalized = normalized.trim();
            let last4 = if normalized.len() >= 4 {
                normalized[normalized.len() - 4..].to_string()
            } else {
                normalized.to_string()
            };
            let mut hasher = Sha256::new();
            hasher.update(format!("{normalized}|{document_country}|{provider}").as_bytes());
            (last4, hex::encode(hasher.finalize()))
        }
    };

    // Policy: customer exists.
    if repo::get_customer(&st.pool, customer_id).await?.is_none() {
        return Err(PolicyViolation::with_context(
            "customer.attest_kyc.customer_exists",
            format!("Customer {customer_id} does not exist"),
            json!({ "customer_id": customer_id }),
        )
        .into());
    }
    // Policy: attestation signature (provider-aware).
    check_attestation_signature(st, provider, attestation_payload, corroboration_id).await?;
    // Policy: document hash unique per tenant (unless doc-reuse sandbox flag).
    if !allow_doc_reuse() {
        let existing = kyc_find_by_hash(&st.pool, &ctx.tenant, document_type, &hash).await?;
        if existing.is_some() {
            return Err(PolicyViolation::with_context(
                "customer.attest_kyc.document_hash_unique_per_tenant",
                "This identity document is already registered",
                json!({ "document_type": document_type }),
            )
            .into());
        }
    }

    let now = bss_clock::now();
    let dob = NaiveDate::parse_from_str(date_of_birth, "%Y-%m-%d")
        .map_err(|e| ApiError::BadRequest(format!("bad date_of_birth: {e}")))?;
    let verified_dt = chrono::DateTime::parse_from_rfc3339(verified_at)
        .map(|d| d.with_timezone(&chrono::Utc))
        .map_err(|e| ApiError::BadRequest(format!("bad verified_at: {e}")))?;
    let corr_uuid = match corroboration_id {
        Some(c) => Some(
            uuid::Uuid::parse_str(c)
                .map_err(|e| ApiError::BadRequest(format!("bad corroboration_id: {e}")))?,
        ),
        None => None,
    };

    let mut tx = st.pool.begin().await?;
    // doc-reuse: delete the existing identity row so the new customer can bind.
    if allow_doc_reuse() {
        sqlx::query("DELETE FROM crm.customer_identity WHERE tenant_id=$1 AND document_type=$2 AND document_number_hash=$3")
            .bind(&ctx.tenant)
            .bind(document_type)
            .bind(&hash)
            .execute(&mut *tx)
            .await?;
    }
    sqlx::query(
        "INSERT INTO crm.customer_identity \
         (customer_id, document_type, document_number_hash, document_number_last4, document_country, date_of_birth, nationality, verified_by, attestation_payload, verified_at, corroboration_id, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
    )
    .bind(customer_id)
    .bind(document_type)
    .bind(&hash)
    .bind(&last4)
    .bind(document_country)
    .bind(dob)
    .bind(nationality)
    .bind(provider)
    .bind(sqlx::types::Json(attestation_payload.clone()))
    .bind(verified_dt)
    .bind(corr_uuid)
    .bind(&ctx.tenant)
    .execute(&mut *tx)
    .await?;
    sqlx::query("UPDATE crm.customer SET kyc_status='verified', kyc_verified_at=$2, kyc_verification_method=$3, kyc_reference=$4, updated_at=now() WHERE id=$1")
        .bind(customer_id)
        .bind(now)
        .bind(provider)
        .bind(provider_reference)
        .execute(&mut *tx)
        .await?;
    stage(&mut tx, ctx, "customer.kyc_attested", "customer", customer_id, json!({ "provider": provider, "document_type": document_type, "document_country": document_country, "kyc_status": "verified" })).await?;
    log_interaction(
        &mut tx,
        ctx,
        customer_id,
        &format!("KYC attested via {provider}"),
        None,
        None,
    )
    .await?;
    tx.commit().await?;

    Ok(
        json!({ "customer_id": customer_id, "kyc_status": "verified", "provider": provider, "verified_at": bss_clock::isoformat(now) }),
    )
}

async fn kyc_find_by_hash(
    pool: &PgPool,
    tenant: &str,
    doc_type: &str,
    hash: &str,
) -> Result<Option<String>, ApiError> {
    let r = sqlx::query_scalar::<_, String>(
        "SELECT customer_id FROM crm.customer_identity WHERE tenant_id=$1 AND document_type=$2 AND document_number_hash=$3",
    )
    .bind(tenant)
    .bind(doc_type)
    .bind(hash)
    .fetch_optional(pool)
    .await?;
    Ok(r)
}

async fn check_attestation_signature(
    st: &AppState,
    provider: &str,
    payload: &Value,
    corroboration_id: Option<&str>,
) -> Result<(), ApiError> {
    let rule = "kyc.attestation.uncorroborated";
    if provider == "didit" {
        let Some(cid) = corroboration_id else {
            return Err(PolicyViolation::with_context(
                rule,
                "Didit attestation requires corroboration_id",
                json!({ "provider": provider }),
            )
            .into());
        };
        let cid = uuid::Uuid::parse_str(cid).map_err(|e| {
            ApiError::from(PolicyViolation::with_context(
                rule,
                format!("Malformed corroboration_id: {e}"),
                json!({ "provider": provider }),
            ))
        })?;
        let row = sqlx::query("SELECT decision_status, received_at FROM integrations.kyc_webhook_corroboration WHERE id=$1 AND provider='didit'")
            .bind(cid)
            .fetch_optional(&st.pool)
            .await?;
        let Some(row) = row else {
            return Err(PolicyViolation::with_context(
                rule,
                "No corroborating webhook delivery found for the supplied corroboration_id",
                json!({ "corroboration_id": cid.to_string() }),
            )
            .into());
        };
        let decision_status: String = row.try_get("decision_status")?;
        if decision_status != "Approved" {
            return Err(PolicyViolation::with_context(rule, format!("Corroborating webhook reports status '{decision_status}'; only 'Approved' attestations are acceptable"), json!({ "decision_status": decision_status })).into());
        }
        let received_at: chrono::DateTime<chrono::Utc> = row.try_get("received_at")?;
        let age = bss_clock::now() - received_at;
        if age > chrono::Duration::minutes(30) {
            return Err(PolicyViolation::with_context(
                rule,
                "Corroborating webhook is older than the freshness window (0:30:00)",
                json!({ "age_seconds": age.num_seconds() }),
            )
            .into());
        }
        return Ok(());
    }
    // Legacy / prebaked.
    if !allow_prebaked() {
        return Err(PolicyViolation::with_context(rule, format!("Provider '{provider}' not accepted in this environment. Production requires BSS_KYC_ALLOW_PREBAKED=true to accept prebaked / legacy attestations."), json!({ "provider": provider })).into());
    }
    if payload.get("signature").is_none() {
        return Err(PolicyViolation::with_context(
            rule,
            "Attestation payload missing signature",
            json!({}),
        )
        .into());
    }
    Ok(())
}

pub async fn kyc_status(st: &AppState, customer_id: &str) -> Result<Value, ApiError> {
    let cust = repo::get_customer(&st.pool, customer_id)
        .await?
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "customer.kyc.not_found",
                format!("Customer {customer_id} not found"),
                json!({ "customer_id": customer_id }),
            ))
        })?;
    Ok(json!({
        "customer_id": customer_id,
        "kyc_status": cust.kyc_status,
        "kyc_verified_at": cust.kyc_verified_at.map(bss_clock::isoformat),
        "kyc_verification_method": cust.kyc_verification_method,
        "kyc_reference": cust.kyc_reference,
    }))
}

// ── chat transcript ─────────────────────────────────────────────────────────

pub async fn store_transcript(
    st: &AppState,
    hash: &str,
    customer_id: &str,
    body: &str,
) -> Result<crate::repo::ChatTranscriptRow, ApiError> {
    let mut hasher = Sha256::new();
    hasher.update(body.as_bytes());
    let actual = hex::encode(hasher.finalize());
    if actual != hash {
        return Err(PolicyViolation::with_context(
            "chat_transcript.hash_mismatch",
            "Provided hash does not match SHA-256 of body.",
            json!({ "provided": hash, "expected": actual }),
        )
        .into());
    }
    let mut tx = st.pool.begin().await?;
    sqlx::query("INSERT INTO audit.chat_transcript (hash, customer_id, body) VALUES ($1,$2,$3) ON CONFLICT (hash) DO NOTHING")
        .bind(hash)
        .bind(customer_id)
        .bind(body)
        .execute(&mut *tx)
        .await?;
    let row = repo::get_transcript_conn(&mut tx, hash)
        .await?
        .ok_or_else(|| ApiError::Internal("transcript vanished".into()))?;
    tx.commit().await?;
    Ok(row)
}

// ── port request ────────────────────────────────────────────────────────────

pub async fn create_port_request(
    st: &AppState,
    ctx: &RequestCtx,
    direction: &str,
    donor_carrier: &str,
    donor_msisdn: &str,
    target_subscription_id: Option<&str>,
    requested_port_date: NaiveDate,
) -> Result<PortRequestRow, ApiError> {
    pol::check_direction_valid(direction)?;
    pol::check_target_sub_required(direction, target_subscription_id)?;
    let mut tx = st.pool.begin().await?;
    let existing = repo::active_port_for_donor(&mut tx, donor_msisdn, &ctx.tenant).await?;
    pol::check_donor_msisdn_unique(donor_msisdn, existing.as_ref())?;

    let id = next_port_id();
    sqlx::query(
        "INSERT INTO crm.port_request (id, direction, donor_carrier, donor_msisdn, target_subscription_id, requested_port_date, state, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,'requested',$7)",
    )
    .bind(&id)
    .bind(direction)
    .bind(donor_carrier)
    .bind(donor_msisdn)
    .bind(target_subscription_id)
    .bind(requested_port_date)
    .bind(&ctx.tenant)
    .execute(&mut *tx)
    .await?;
    stage(&mut tx, ctx, "port_request.created", "port_request", &id, json!({
        "portRequestId": id, "direction": direction, "donorCarrier": donor_carrier, "donorMsisdn": donor_msisdn,
        "targetSubscriptionId": target_subscription_id, "requestedPortDate": requested_port_date.format("%Y-%m-%d").to_string(),
    })).await?;
    tx.commit().await?;
    tracing::info!(
        port_request_id = id,
        direction,
        donor_msisdn,
        "port_request.created"
    );
    repo::get_port_request(&st.pool, &id)
        .await?
        .ok_or_else(|| ApiError::Internal("port request vanished".into()))
}

pub async fn approve_port_request(
    st: &AppState,
    ctx: &RequestCtx,
    port_id: &str,
) -> Result<(), ApiError> {
    let port = repo::get_port_request(&st.pool, port_id)
        .await?
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "port_request.not_found",
                format!("Port request {port_id} not found"),
                json!({ "port_request_id": port_id }),
            ))
        })?;
    pol::check_pr_transition_valid(&port.state, "complete")?;

    let mut tx = st.pool.begin().await?;
    if port.direction == "port_in" {
        let status = if port.target_subscription_id.is_some() {
            "assigned"
        } else {
            "available"
        };
        sqlx::query("INSERT INTO inventory.msisdn_pool (msisdn, status, assigned_to_subscription_id, tenant_id) VALUES ($1,$2,$3,$4) ON CONFLICT (msisdn) DO NOTHING")
            .bind(&port.donor_msisdn)
            .bind(status)
            .bind(&port.target_subscription_id)
            .bind(&ctx.tenant)
            .execute(&mut *tx)
            .await?;
        stage(&mut tx, ctx, "inventory.msisdn.seeded_from_port_in", "msisdn_pool", &port.donor_msisdn, json!({
            "msisdn": port.donor_msisdn, "donorCarrier": port.donor_carrier, "portRequestId": port.id,
            "targetSubscriptionId": port.target_subscription_id, "status": status,
        })).await?;
    } else {
        // port_out
        let Some(target_sub) = &port.target_subscription_id else {
            return Err(PolicyViolation::with_context(
                "port_request.create.target_sub_required_for_port_out",
                "port_out requires target_subscription_id at approve",
                json!({ "port_request_id": port.id }),
            )
            .into());
        };
        sqlx::query(
            "UPDATE inventory.msisdn_pool SET status='ported_out', quarantine_until='9999-12-31'::timestamptz, \
             assigned_to_subscription_id=COALESCE($2, assigned_to_subscription_id), updated_at=$3 WHERE msisdn=$1",
        )
        .bind(&port.donor_msisdn)
        .bind(Some(target_sub))
        .bind(bss_clock::now())
        .execute(&mut *tx)
        .await?;
        stage(&mut tx, ctx, "inventory.msisdn.ported_out", "msisdn_pool", &port.donor_msisdn, json!({
            "msisdn": port.donor_msisdn, "donorCarrier": port.donor_carrier, "portRequestId": port.id,
            "targetSubscriptionId": target_sub, "portedOutAt": bss_clock::isoformat(bss_clock::now()),
        })).await?;
    }

    sqlx::query("UPDATE crm.port_request SET state='completed', updated_at=now() WHERE id=$1")
        .bind(port_id)
        .execute(&mut *tx)
        .await?;
    stage(&mut tx, ctx, "port_request.approved", "port_request", port_id, json!({
        "portRequestId": port.id, "direction": port.direction, "donorMsisdn": port.donor_msisdn, "targetSubscriptionId": port.target_subscription_id,
    })).await?;
    stage(
        &mut tx,
        ctx,
        "port_request.completed",
        "port_request",
        port_id,
        json!({ "portRequestId": port.id, "direction": port.direction }),
    )
    .await?;
    tx.commit().await?;

    // port_out: terminate the subscription (release_inventory=false), best-effort.
    if port.direction == "port_out" {
        if let Some(sub) = &port.target_subscription_id {
            if let Err(_e) = terminate_ported_sub(&st.subscription, sub).await {
                tracing::warn!(
                    subscription_id = sub,
                    "port_request.subscription_terminate_failed"
                );
            }
        }
    }
    tracing::info!(
        port_request_id = port.id,
        direction = port.direction,
        "port_request.approved"
    );
    Ok(())
}

async fn terminate_ported_sub(
    sub_client: &SubscriptionClient,
    sub_id: &str,
) -> Result<(), ClientError> {
    // SubscriptionClient has no typed terminate yet; POST the terminate body.
    let body = json!({ "reason": "ported_out", "releaseInventory": false });
    sub_client.terminate(sub_id, &body).await.map(|_| ())
}

pub async fn reject_port_request(
    st: &AppState,
    ctx: &RequestCtx,
    port_id: &str,
    reason: &str,
) -> Result<(), ApiError> {
    let port = repo::get_port_request(&st.pool, port_id)
        .await?
        .ok_or_else(|| {
            ApiError::from(PolicyViolation::with_context(
                "port_request.not_found",
                format!("Port request {port_id} not found"),
                json!({ "port_request_id": port_id }),
            ))
        })?;
    pol::check_reject_reason(reason)?;
    pol::check_pr_transition_valid(&port.state, "reject")?;
    let mut tx = st.pool.begin().await?;
    sqlx::query("UPDATE crm.port_request SET state='rejected', rejection_reason=$2, updated_at=now() WHERE id=$1").bind(port_id).bind(reason).execute(&mut *tx).await?;
    stage(&mut tx, ctx, "port_request.rejected", "port_request", port_id, json!({
        "portRequestId": port.id, "direction": port.direction, "donorMsisdn": port.donor_msisdn, "reason": reason,
    })).await?;
    tx.commit().await?;
    tracing::info!(port_request_id = port.id, reason, "port_request.rejected");
    Ok(())
}
