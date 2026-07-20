//! `crm` + `inventory` + `audit.chat_transcript` persistence — port of the nine
//! repositories. Dumb CRUD over the ORM tables; the FSM/policy logic lives in the
//! service layer. Reads run on the pool; write paths take a `&mut PgConnection`
//! (the caller's transaction) so the aggregate write + event stage commit together.
//! Server defaults (tenant_id='DEFAULT', created_at/updated_at=now()) are relied on
//! exactly as the oracle does.

use chrono::{DateTime, NaiveDate, Utc};
use serde_json::Value;
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::{PgPool, Row};

use crate::error::ApiError;

// ── row structs ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct CustomerRow {
    pub id: String,
    pub party_id: String,
    pub status: String,
    pub status_reason: Option<String>,
    pub customer_since: Option<DateTime<Utc>>,
    pub kyc_status: String,
    pub kyc_verified_at: Option<DateTime<Utc>>,
    pub kyc_verification_method: Option<String>,
    pub kyc_reference: Option<String>,
}

#[derive(Debug, Clone)]
pub struct IndividualRow {
    pub given_name: String,
    pub family_name: String,
    pub date_of_birth: Option<NaiveDate>,
}

#[derive(Debug, Clone)]
pub struct ContactMediumRow {
    pub id: String,
    pub party_id: String,
    pub medium_type: String,
    pub value: String,
    pub is_primary: bool,
    pub valid_from: Option<DateTime<Utc>>,
    pub valid_to: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct CustomerFull {
    pub customer: CustomerRow,
    pub individual: Option<IndividualRow>,
    pub contact_mediums: Vec<ContactMediumRow>,
}

#[derive(Debug, Clone)]
pub struct AgentRow {
    pub id: String,
    pub name: String,
    pub email: Option<String>,
    pub role: Option<String>,
    pub status: String,
}

#[derive(Debug, Clone)]
pub struct InteractionRow {
    pub id: String,
    pub customer_id: String,
    pub channel: Option<String>,
    pub direction: Option<String>,
    pub summary: String,
    pub body: Option<String>,
    pub agent_id: Option<String>,
    pub related_case_id: Option<String>,
    pub related_ticket_id: Option<String>,
    pub occurred_at: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct CaseRow {
    pub id: String,
    pub customer_id: String,
    pub subject: String,
    pub description: Option<String>,
    pub state: String,
    pub priority: Option<String>,
    pub category: Option<String>,
    pub resolution_code: Option<String>,
    pub opened_by_agent_id: Option<String>,
    pub opened_at: DateTime<Utc>,
    pub closed_at: Option<DateTime<Utc>>,
    pub chat_transcript_hash: Option<String>,
}

#[derive(Debug, Clone)]
pub struct CaseNoteRow {
    pub id: String,
    pub case_id: String,
    pub author_agent_id: Option<String>,
    pub body: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct CaseFull {
    pub case: CaseRow,
    pub notes: Vec<CaseNoteRow>,
    pub ticket_ids: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct TicketRow {
    pub id: String,
    pub case_id: Option<String>,
    pub customer_id: String,
    pub ticket_type: Option<String>,
    pub subject: String,
    pub description: Option<String>,
    pub state: String,
    pub priority: Option<String>,
    pub assigned_to_agent_id: Option<String>,
    pub resolution_notes: Option<String>,
    pub opened_at: DateTime<Utc>,
    pub resolved_at: Option<DateTime<Utc>>,
    pub closed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct PortRequestRow {
    pub id: String,
    pub direction: String,
    pub donor_carrier: String,
    pub donor_msisdn: String,
    pub target_subscription_id: Option<String>,
    pub requested_port_date: NaiveDate,
    pub state: String,
    pub rejection_reason: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct MsisdnRow {
    pub msisdn: String,
    pub status: String,
    pub reserved_at: Option<DateTime<Utc>>,
    pub assigned_to_subscription_id: Option<String>,
    /// Soft-hold expiry (v-reservation). `None` = hard reserve / not held.
    pub reserved_until: Option<DateTime<Utc>>,
    /// Soft-hold owner (the open-order / holder id). `None` = not soft-held.
    pub reserved_for: Option<String>,
}

/// A persisted, resumable signup funnel (v-reservation phase 2).
#[derive(Debug, Clone)]
pub struct OpenOrderRow {
    pub id: String,
    pub owner_identity: String,
    pub customer_id: Option<String>,
    pub plan_code: String,
    pub msisdn: Option<String>,
    pub iccid: Option<String>,
    pub step: String,
    pub status: String,
    pub reserved_until: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct EsimRow {
    pub iccid: String,
    pub imsi: String,
    pub profile_state: String,
    pub smdp_server: Option<String>,
    pub matching_id: Option<String>,
    pub activation_code: Option<String>,
    pub assigned_msisdn: Option<String>,
    pub assigned_to_subscription_id: Option<String>,
    pub reserved_at: Option<DateTime<Utc>>,
    pub downloaded_at: Option<DateTime<Utc>>,
    pub activated_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct ChatTranscriptRow {
    pub hash: String,
    pub customer_id: String,
    pub body: String,
    pub recorded_at: DateTime<Utc>,
}

// ── column lists + parsers ──────────────────────────────────────────────────

const CUST_COLS: &str = "id, party_id, status, status_reason, customer_since, kyc_status, \
     kyc_verified_at, kyc_verification_method, kyc_reference";
const CM_COLS: &str = "id, party_id, medium_type, value, is_primary, valid_from, valid_to";
const AGENT_COLS: &str = "id, name, email, role, status";
const CASE_COLS: &str = "id, customer_id, subject, description, state, priority, category, \
     resolution_code, opened_by_agent_id, opened_at, closed_at, chat_transcript_hash";
const NOTE_COLS: &str = "id, case_id, author_agent_id, body, created_at";
const TICKET_COLS: &str = "id, case_id, customer_id, ticket_type, subject, description, state, \
     priority, assigned_to_agent_id, resolution_notes, opened_at, resolved_at, closed_at";
const INT_COLS: &str = "id, customer_id, channel, direction, summary, body, agent_id, \
     related_case_id, related_ticket_id, occurred_at";
const PORT_COLS: &str = "id, direction, donor_carrier, donor_msisdn, target_subscription_id, \
     requested_port_date, state, rejection_reason, created_at, updated_at";
const MSISDN_COLS: &str =
    "msisdn, status, reserved_at, assigned_to_subscription_id, reserved_until, reserved_for";
const ESIM_COLS: &str = "iccid, imsi, profile_state, smdp_server, matching_id, activation_code, \
     assigned_msisdn, assigned_to_subscription_id, reserved_at, downloaded_at, activated_at";

fn cust_row(r: &PgRow) -> Result<CustomerRow, ApiError> {
    Ok(CustomerRow {
        id: r.try_get("id")?,
        party_id: r.try_get("party_id")?,
        status: r.try_get("status")?,
        status_reason: r.try_get("status_reason")?,
        customer_since: r.try_get("customer_since")?,
        kyc_status: r.try_get("kyc_status")?,
        kyc_verified_at: r.try_get("kyc_verified_at")?,
        kyc_verification_method: r.try_get("kyc_verification_method")?,
        kyc_reference: r.try_get("kyc_reference")?,
    })
}

fn cm_row(r: &PgRow) -> Result<ContactMediumRow, ApiError> {
    Ok(ContactMediumRow {
        id: r.try_get("id")?,
        party_id: r.try_get("party_id")?,
        medium_type: r.try_get("medium_type")?,
        value: r.try_get("value")?,
        is_primary: r.try_get("is_primary")?,
        valid_from: r.try_get("valid_from")?,
        valid_to: r.try_get("valid_to")?,
    })
}

fn agent_row(r: &PgRow) -> Result<AgentRow, ApiError> {
    Ok(AgentRow {
        id: r.try_get("id")?,
        name: r.try_get("name")?,
        email: r.try_get("email")?,
        role: r.try_get("role")?,
        status: r.try_get("status")?,
    })
}

fn case_row(r: &PgRow) -> Result<CaseRow, ApiError> {
    Ok(CaseRow {
        id: r.try_get("id")?,
        customer_id: r.try_get("customer_id")?,
        subject: r.try_get("subject")?,
        description: r.try_get("description")?,
        state: r.try_get("state")?,
        priority: r.try_get("priority")?,
        category: r.try_get("category")?,
        resolution_code: r.try_get("resolution_code")?,
        opened_by_agent_id: r.try_get("opened_by_agent_id")?,
        opened_at: r.try_get("opened_at")?,
        closed_at: r.try_get("closed_at")?,
        chat_transcript_hash: r.try_get("chat_transcript_hash")?,
    })
}

fn note_row(r: &PgRow) -> Result<CaseNoteRow, ApiError> {
    Ok(CaseNoteRow {
        id: r.try_get("id")?,
        case_id: r.try_get("case_id")?,
        author_agent_id: r.try_get("author_agent_id")?,
        body: r.try_get("body")?,
        created_at: r.try_get("created_at")?,
    })
}

fn ticket_row(r: &PgRow) -> Result<TicketRow, ApiError> {
    Ok(TicketRow {
        id: r.try_get("id")?,
        case_id: r.try_get("case_id")?,
        customer_id: r.try_get("customer_id")?,
        ticket_type: r.try_get("ticket_type")?,
        subject: r.try_get("subject")?,
        description: r.try_get("description")?,
        state: r.try_get("state")?,
        priority: r.try_get("priority")?,
        assigned_to_agent_id: r.try_get("assigned_to_agent_id")?,
        resolution_notes: r.try_get("resolution_notes")?,
        opened_at: r.try_get("opened_at")?,
        resolved_at: r.try_get("resolved_at")?,
        closed_at: r.try_get("closed_at")?,
    })
}

fn int_row(r: &PgRow) -> Result<InteractionRow, ApiError> {
    Ok(InteractionRow {
        id: r.try_get("id")?,
        customer_id: r.try_get("customer_id")?,
        channel: r.try_get("channel")?,
        direction: r.try_get("direction")?,
        summary: r.try_get("summary")?,
        body: r.try_get("body")?,
        agent_id: r.try_get("agent_id")?,
        related_case_id: r.try_get("related_case_id")?,
        related_ticket_id: r.try_get("related_ticket_id")?,
        occurred_at: r.try_get("occurred_at")?,
    })
}

fn port_row(r: &PgRow) -> Result<PortRequestRow, ApiError> {
    Ok(PortRequestRow {
        id: r.try_get("id")?,
        direction: r.try_get("direction")?,
        donor_carrier: r.try_get("donor_carrier")?,
        donor_msisdn: r.try_get("donor_msisdn")?,
        target_subscription_id: r.try_get("target_subscription_id")?,
        requested_port_date: r.try_get("requested_port_date")?,
        state: r.try_get("state")?,
        rejection_reason: r.try_get("rejection_reason")?,
        created_at: r.try_get("created_at")?,
        updated_at: r.try_get("updated_at")?,
    })
}

fn msisdn_row(r: &PgRow) -> Result<MsisdnRow, ApiError> {
    Ok(MsisdnRow {
        msisdn: r.try_get("msisdn")?,
        status: r.try_get("status")?,
        reserved_at: r.try_get("reserved_at")?,
        assigned_to_subscription_id: r.try_get("assigned_to_subscription_id")?,
        reserved_until: r.try_get("reserved_until")?,
        reserved_for: r.try_get("reserved_for")?,
    })
}

fn esim_row(r: &PgRow) -> Result<EsimRow, ApiError> {
    Ok(EsimRow {
        iccid: r.try_get("iccid")?,
        imsi: r.try_get("imsi")?,
        profile_state: r.try_get("profile_state")?,
        smdp_server: r.try_get("smdp_server")?,
        matching_id: r.try_get("matching_id")?,
        activation_code: r.try_get("activation_code")?,
        assigned_msisdn: r.try_get("assigned_msisdn")?,
        assigned_to_subscription_id: r.try_get("assigned_to_subscription_id")?,
        reserved_at: r.try_get("reserved_at")?,
        downloaded_at: r.try_get("downloaded_at")?,
        activated_at: r.try_get("activated_at")?,
    })
}

fn transcript_row(r: &PgRow) -> Result<ChatTranscriptRow, ApiError> {
    Ok(ChatTranscriptRow {
        hash: r.try_get("hash")?,
        customer_id: r.try_get("customer_id")?,
        body: r.try_get("body")?,
        recorded_at: r.try_get("recorded_at")?,
    })
}

// ── customer reads ──────────────────────────────────────────────────────────

pub async fn get_customer(pool: &PgPool, id: &str) -> Result<Option<CustomerRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {CUST_COLS} FROM crm.customer WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(cust_row).transpose()
}

async fn individual_for(pool: &PgPool, party_id: &str) -> Result<Option<IndividualRow>, ApiError> {
    let r = sqlx::query(
        "SELECT given_name, family_name, date_of_birth FROM crm.individual WHERE party_id = $1",
    )
    .bind(party_id)
    .fetch_optional(pool)
    .await?;
    match r {
        Some(r) => Ok(Some(IndividualRow {
            given_name: r.try_get("given_name")?,
            family_name: r.try_get("family_name")?,
            date_of_birth: r.try_get("date_of_birth")?,
        })),
        None => Ok(None),
    }
}

/// Active (valid_to IS NULL) contact mediums for a party. No ORDER BY — the
/// oracle's `to_tmf629_customer` iterates the un-ordered `selectinload`
/// relationship (physical/insertion order), so both read the same heap order.
async fn active_mediums(pool: &PgPool, party_id: &str) -> Result<Vec<ContactMediumRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {CM_COLS} FROM crm.contact_medium WHERE party_id = $1 AND valid_to IS NULL"
    ))
    .bind(party_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(cm_row).collect()
}

pub async fn get_customer_full(pool: &PgPool, id: &str) -> Result<Option<CustomerFull>, ApiError> {
    let Some(customer) = get_customer(pool, id).await? else {
        return Ok(None);
    };
    let individual = individual_for(pool, &customer.party_id).await?;
    let contact_mediums = active_mediums(pool, &customer.party_id).await?;
    Ok(Some(CustomerFull {
        customer,
        individual,
        contact_mediums,
    }))
}

pub async fn list_customers(
    pool: &PgPool,
    status: Option<&str>,
    name_contains: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<CustomerFull>, ApiError> {
    // Base query ordered by id; optional status + name filters (name joins
    // party+individual with ILIKE on given/family).
    let mut sql = "SELECT c.id AS id, c.party_id AS party_id, c.status AS status, \
         c.status_reason AS status_reason, c.customer_since AS customer_since, c.kyc_status AS kyc_status, \
         c.kyc_verified_at AS kyc_verified_at, c.kyc_verification_method AS kyc_verification_method, \
         c.kyc_reference AS kyc_reference FROM crm.customer c"
        .to_string();
    if name_contains.is_some() {
        sql.push_str(
            " JOIN crm.party p ON c.party_id = p.id JOIN crm.individual i ON i.party_id = p.id",
        );
    }
    let mut conds = Vec::new();
    if status.is_some() {
        conds.push("c.status = $1".to_string());
    }
    if name_contains.is_some() {
        let n = if status.is_some() { "$2" } else { "$1" };
        conds.push(format!(
            "(i.given_name ILIKE {n} OR i.family_name ILIKE {n})"
        ));
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(" ORDER BY c.id");
    // limit/offset are appended positionally after the filters.
    let like = name_contains.map(|n| format!("%{n}%"));
    let (lim_p, off_p) = match (status.is_some(), name_contains.is_some()) {
        (true, true) => ("$3", "$4"),
        (true, false) | (false, true) => ("$2", "$3"),
        (false, false) => ("$1", "$2"),
    };
    sql.push_str(&format!(" LIMIT {lim_p} OFFSET {off_p}"));

    let mut q = sqlx::query(&sql);
    if let Some(s) = status {
        q = q.bind(s.to_string());
    }
    if let Some(l) = &like {
        q = q.bind(l.clone());
    }
    q = q.bind(limit).bind(offset);
    let rows = q.fetch_all(pool).await?;

    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let customer = cust_row(r)?;
        let individual = individual_for(pool, &customer.party_id).await?;
        let contact_mediums = active_mediums(pool, &customer.party_id).await?;
        out.push(CustomerFull {
            customer,
            individual,
            contact_mediums,
        });
    }
    Ok(out)
}

pub async fn find_customer_by_email(
    pool: &PgPool,
    email: &str,
) -> Result<Option<String>, ApiError> {
    let r = sqlx::query(
        "SELECT c.id AS id FROM crm.customer c \
         JOIN crm.party p ON c.party_id = p.id \
         JOIN crm.contact_medium cm ON cm.party_id = p.id \
         WHERE cm.medium_type = 'email' AND cm.value = $1 AND cm.valid_to IS NULL LIMIT 1",
    )
    .bind(email)
    .fetch_optional(pool)
    .await?;
    Ok(r.map(|r| r.get("id")))
}

pub async fn get_contact_medium(
    pool: &PgPool,
    cm_id: &str,
) -> Result<Option<ContactMediumRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {CM_COLS} FROM crm.contact_medium WHERE id = $1"
    ))
    .bind(cm_id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(cm_row).transpose()
}

pub async fn get_agent(pool: &PgPool, id: &str) -> Result<Option<AgentRow>, ApiError> {
    let r = sqlx::query(&format!("SELECT {AGENT_COLS} FROM crm.agent WHERE id = $1"))
        .bind(id)
        .fetch_optional(pool)
        .await?;
    r.as_ref().map(agent_row).transpose()
}

pub async fn list_agents(
    pool: &PgPool,
    status: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<AgentRow>, ApiError> {
    let rows = match status {
        Some(s) => {
            sqlx::query(&format!(
            "SELECT {AGENT_COLS} FROM crm.agent WHERE status = $1 ORDER BY id LIMIT $2 OFFSET $3"
        ))
            .bind(s)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        None => {
            sqlx::query(&format!(
                "SELECT {AGENT_COLS} FROM crm.agent ORDER BY id LIMIT $1 OFFSET $2"
            ))
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
    };
    rows.iter().map(agent_row).collect()
}

// ── case reads ──────────────────────────────────────────────────────────────

// No ORDER BY on the case's relationship-backed collections — the oracle's
// `to_case_response` iterates the un-ordered `selectinload` `notes`/`tickets`
// (physical order), so both read the same heap order.
async fn notes_for(pool: &PgPool, case_id: &str) -> Result<Vec<CaseNoteRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {NOTE_COLS} FROM crm.case_note WHERE case_id = $1"
    ))
    .bind(case_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(note_row).collect()
}

async fn ticket_ids_for(pool: &PgPool, case_id: &str) -> Result<Vec<String>, ApiError> {
    let rows = sqlx::query("SELECT id FROM crm.ticket WHERE case_id = $1")
        .bind(case_id)
        .fetch_all(pool)
        .await?;
    Ok(rows.iter().map(|r| r.get("id")).collect())
}

pub async fn get_case_full(pool: &PgPool, id: &str) -> Result<Option<CaseFull>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {CASE_COLS} FROM crm.\"case\" WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    let Some(r) = r else { return Ok(None) };
    let case = case_row(&r)?;
    let notes = notes_for(pool, id).await?;
    let ticket_ids = ticket_ids_for(pool, id).await?;
    Ok(Some(CaseFull {
        case,
        notes,
        ticket_ids,
    }))
}

pub async fn get_case(pool: &PgPool, id: &str) -> Result<Option<CaseRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {CASE_COLS} FROM crm.\"case\" WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(case_row).transpose()
}

pub async fn list_cases(
    pool: &PgPool,
    customer_id: Option<&str>,
    state: Option<&str>,
    assigned_agent_id: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<CaseFull>, ApiError> {
    let mut sql = format!("SELECT {CASE_COLS} FROM crm.\"case\"");
    let mut conds = Vec::new();
    let mut idx = 1;
    if customer_id.is_some() {
        conds.push(format!("customer_id = ${idx}"));
        idx += 1;
    }
    if state.is_some() {
        conds.push(format!("state = ${idx}"));
        idx += 1;
    }
    if assigned_agent_id.is_some() {
        conds.push(format!("opened_by_agent_id = ${idx}"));
        idx += 1;
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(&format!(
        " ORDER BY opened_at DESC LIMIT ${idx} OFFSET ${}",
        idx + 1
    ));

    let mut q = sqlx::query(&sql);
    if let Some(c) = customer_id {
        q = q.bind(c);
    }
    if let Some(s) = state {
        q = q.bind(s);
    }
    if let Some(a) = assigned_agent_id {
        q = q.bind(a);
    }
    q = q.bind(limit).bind(offset);
    let rows = q.fetch_all(pool).await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let case = case_row(r)?;
        let notes = notes_for(pool, &case.id).await?;
        let ticket_ids = ticket_ids_for(pool, &case.id).await?;
        out.push(CaseFull {
            case,
            notes,
            ticket_ids,
        });
    }
    Ok(out)
}

/// Non-terminal tickets on a case (on the tx connection — used by the resolve/
/// cancel guards inside a case transition).
pub async fn find_open_by_case(
    conn: &mut PgConnection,
    case_id: &str,
) -> Result<Vec<TicketRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {TICKET_COLS} FROM crm.ticket WHERE case_id = $1 \
         AND state NOT IN ('closed','cancelled') ORDER BY id"
    ))
    .bind(case_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter().map(ticket_row).collect()
}

// ── ticket reads ────────────────────────────────────────────────────────────

pub async fn get_ticket(pool: &PgPool, id: &str) -> Result<Option<TicketRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {TICKET_COLS} FROM crm.ticket WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(ticket_row).transpose()
}

pub async fn list_tickets(
    pool: &PgPool,
    customer_id: Option<&str>,
    case_id: Option<&str>,
    state: Option<&str>,
    assigned_to_agent_id: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<TicketRow>, ApiError> {
    let mut sql = format!("SELECT {TICKET_COLS} FROM crm.ticket");
    let mut conds = Vec::new();
    let mut idx = 1;
    for (present, col) in [
        (customer_id.is_some(), "customer_id"),
        (case_id.is_some(), "case_id"),
        (state.is_some(), "state"),
        (assigned_to_agent_id.is_some(), "assigned_to_agent_id"),
    ] {
        if present {
            conds.push(format!("{col} = ${idx}"));
            idx += 1;
        }
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(&format!(
        " ORDER BY opened_at DESC LIMIT ${idx} OFFSET ${}",
        idx + 1
    ));
    let mut q = sqlx::query(&sql);
    for v in [customer_id, case_id, state, assigned_to_agent_id]
        .into_iter()
        .flatten()
    {
        q = q.bind(v);
    }
    q = q.bind(limit).bind(offset);
    let rows = q.fetch_all(pool).await?;
    rows.iter().map(ticket_row).collect()
}

// ── interaction reads ───────────────────────────────────────────────────────

pub async fn list_interactions(
    pool: &PgPool,
    customer_id: &str,
    limit: i64,
    offset: i64,
) -> Result<Vec<InteractionRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {INT_COLS} FROM crm.interaction WHERE customer_id = $1 \
         ORDER BY occurred_at DESC LIMIT $2 OFFSET $3"
    ))
    .bind(customer_id)
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;
    rows.iter().map(int_row).collect()
}

// ── port-request reads ──────────────────────────────────────────────────────

pub async fn get_port_request(pool: &PgPool, id: &str) -> Result<Option<PortRequestRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {PORT_COLS} FROM crm.port_request WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(port_row).transpose()
}

pub async fn list_port_requests(
    pool: &PgPool,
    state: Option<&str>,
    direction: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<PortRequestRow>, ApiError> {
    let mut sql = format!("SELECT {PORT_COLS} FROM crm.port_request");
    let mut conds = Vec::new();
    let mut idx = 1;
    if state.is_some() {
        conds.push(format!("state = ${idx}"));
        idx += 1;
    }
    if direction.is_some() {
        conds.push(format!("direction = ${idx}"));
        idx += 1;
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(&format!(
        " ORDER BY created_at DESC LIMIT ${idx} OFFSET ${}",
        idx + 1
    ));
    let mut q = sqlx::query(&sql);
    for v in [state, direction].into_iter().flatten() {
        q = q.bind(v);
    }
    q = q.bind(limit).bind(offset);
    let rows = q.fetch_all(pool).await?;
    rows.iter().map(port_row).collect()
}

/// Open (requested|validated) port request for a donor MSISDN (the uniqueness guard).
pub async fn active_port_for_donor(
    conn: &mut PgConnection,
    donor_msisdn: &str,
    tenant_id: &str,
) -> Result<Option<PortRequestRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {PORT_COLS} FROM crm.port_request \
         WHERE donor_msisdn = $1 AND tenant_id = $2 AND state IN ('requested','validated') LIMIT 1"
    ))
    .bind(donor_msisdn)
    .bind(tenant_id)
    .fetch_optional(&mut *conn)
    .await?;
    r.as_ref().map(port_row).transpose()
}

// ── inventory reads ─────────────────────────────────────────────────────────

pub async fn get_msisdn(pool: &PgPool, msisdn: &str) -> Result<Option<MsisdnRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {MSISDN_COLS} FROM inventory.msisdn_pool WHERE msisdn = $1"
    ))
    .bind(msisdn)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(msisdn_row).transpose()
}

pub async fn get_msisdn_conn(
    conn: &mut PgConnection,
    msisdn: &str,
) -> Result<Option<MsisdnRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {MSISDN_COLS} FROM inventory.msisdn_pool WHERE msisdn = $1"
    ))
    .bind(msisdn)
    .fetch_optional(&mut *conn)
    .await?;
    r.as_ref().map(msisdn_row).transpose()
}

pub async fn list_msisdns(
    pool: &PgPool,
    status: Option<&str>,
    prefix: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<MsisdnRow>, ApiError> {
    let mut sql = format!("SELECT {MSISDN_COLS} FROM inventory.msisdn_pool");
    let mut conds = Vec::new();
    let mut idx = 1;
    if status.is_some() {
        conds.push(format!("status = ${idx}"));
        idx += 1;
    }
    let like = prefix.map(|p| format!("{p}%"));
    if like.is_some() {
        conds.push(format!("msisdn LIKE ${idx}"));
        idx += 1;
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(&format!(
        " ORDER BY msisdn LIMIT ${idx} OFFSET ${}",
        idx + 1
    ));
    let mut q = sqlx::query(&sql);
    if let Some(s) = status {
        q = q.bind(s);
    }
    if let Some(l) = &like {
        q = q.bind(l.clone());
    }
    q = q.bind(limit).bind(offset);
    let rows = q.fetch_all(pool).await?;
    rows.iter().map(msisdn_row).collect()
}

/// Atomic self-healing **soft hold** on a specific MSISDN (v-reservation, phase 1).
/// Succeeds iff the number is `available` OR a soft hold whose `reserved_until`
/// has already passed (self-healing reclaim — no dependence on the sweep). Single
/// UPDATE, so there is no read-then-write race between concurrent pickers.
/// Returns `true` if the hold was taken.
pub async fn hold_msisdn(
    conn: &mut PgConnection,
    msisdn: &str,
    reserved_for: &str,
    reserved_until: DateTime<Utc>,
    now: DateTime<Utc>,
    tenant: &str,
) -> Result<bool, ApiError> {
    let n = sqlx::query(
        "UPDATE inventory.msisdn_pool \
         SET status='reserved', reserved_at=$3, reserved_until=$4, reserved_for=$5, updated_at=$3 \
         WHERE msisdn=$1 AND tenant_id=$2 \
           AND (status='available' \
                OR reserved_for=$5 \
                OR (status='reserved' AND reserved_until IS NOT NULL AND reserved_until < $3))",
    )
    .bind(msisdn)
    .bind(tenant)
    .bind(now)
    .bind(reserved_until)
    .bind(reserved_for)
    .execute(&mut *conn)
    .await?;
    Ok(n.rows_affected() > 0)
}

/// Release every soft hold owned by `reserved_for` back to `available`. Only ever
/// touches soft holds (`reserved_until IS NOT NULL`) — never a hard reserve or an
/// assigned number. Returns the released MSISDNs (for event emission). Idempotent.
pub async fn release_holds_for(
    conn: &mut PgConnection,
    reserved_for: &str,
    now: DateTime<Utc>,
) -> Result<Vec<String>, ApiError> {
    let rows = sqlx::query(
        "UPDATE inventory.msisdn_pool \
         SET status='available', reserved_at=NULL, reserved_until=NULL, reserved_for=NULL, updated_at=$2 \
         WHERE reserved_for=$1 AND status='reserved' AND reserved_until IS NOT NULL \
         RETURNING msisdn",
    )
    .bind(reserved_for)
    .bind(now)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| r.try_get("msisdn").map_err(ApiError::from))
        .collect()
}

// ── open_order (v-reservation phase 2) ──────────────────────────────────────

const OPEN_ORDER_COLS: &str =
    "id, owner_identity, customer_id, plan_code, msisdn, iccid, step, status, reserved_until";

fn open_order_row(r: &PgRow) -> Result<OpenOrderRow, ApiError> {
    Ok(OpenOrderRow {
        id: r.try_get("id")?,
        owner_identity: r.try_get("owner_identity")?,
        customer_id: r.try_get("customer_id")?,
        plan_code: r.try_get("plan_code")?,
        msisdn: r.try_get("msisdn")?,
        iccid: r.try_get("iccid")?,
        step: r.try_get("step")?,
        status: r.try_get("status")?,
        reserved_until: r.try_get("reserved_until")?,
    })
}

/// The one open order for `owner_identity`, if any (status='open').
pub async fn get_open_order_by_identity(
    conn: &mut PgConnection,
    identity: &str,
    tenant: &str,
) -> Result<Option<OpenOrderRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {OPEN_ORDER_COLS} FROM inventory.open_order \
         WHERE owner_identity=$1 AND status='open' AND tenant_id=$2 FOR UPDATE"
    ))
    .bind(identity)
    .bind(tenant)
    .fetch_optional(&mut *conn)
    .await?;
    r.as_ref().map(open_order_row).transpose()
}

pub async fn get_open_order(pool: &PgPool, id: &str) -> Result<Option<OpenOrderRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {OPEN_ORDER_COLS} FROM inventory.open_order WHERE id=$1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(open_order_row).transpose()
}

/// Insert a fresh open order. Returns `false` on the partial-unique conflict
/// (the owner already has an open order) — the caller then resumes the existing.
#[allow(clippy::too_many_arguments)]
pub async fn insert_open_order(
    conn: &mut PgConnection,
    id: &str,
    owner_identity: &str,
    plan_code: &str,
    msisdn: &str,
    reserved_until: DateTime<Utc>,
    now: DateTime<Utc>,
    tenant: &str,
) -> Result<bool, ApiError> {
    let n = sqlx::query(
        "INSERT INTO inventory.open_order \
         (id, tenant_id, owner_identity, plan_code, msisdn, step, status, reserved_until, created_at, updated_at) \
         VALUES ($1,$2,$3,$4,$5,'pending_customer','open',$6,$7,$7) \
         ON CONFLICT (owner_identity) WHERE status='open' DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(owner_identity)
    .bind(plan_code)
    .bind(msisdn)
    .bind(reserved_until)
    .bind(now)
    .execute(&mut *conn)
    .await?;
    Ok(n.rows_affected() > 0)
}

/// Lock a batch of expired open orders (`status='open'`, hold window passed) for
/// the sweep. `FOR UPDATE SKIP LOCKED` so peer replicas grab disjoint rows.
/// Returns their ids (the hold's `reserved_for`).
pub async fn lock_expired_open_orders(
    conn: &mut PgConnection,
    now: DateTime<Utc>,
    limit: i64,
    tenant: &str,
) -> Result<Vec<String>, ApiError> {
    let rows = sqlx::query(
        "SELECT id FROM inventory.open_order \
         WHERE status='open' AND reserved_until IS NOT NULL AND reserved_until <= $1 \
           AND tenant_id=$2 \
         ORDER BY reserved_until LIMIT $3 FOR UPDATE SKIP LOCKED",
    )
    .bind(now)
    .bind(tenant)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| r.try_get("id").map_err(ApiError::from))
        .collect()
}

/// Link the customer id onto an open order (create-customer step).
pub async fn link_open_order_customer(
    conn: &mut PgConnection,
    id: &str,
    customer_id: &str,
    now: DateTime<Utc>,
) -> Result<(), ApiError> {
    sqlx::query("UPDATE inventory.open_order SET customer_id=$2, updated_at=$3 WHERE id=$1")
        .bind(id)
        .bind(customer_id)
        .bind(now)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// Set the open order's step / status / hold-expiry (state-machine transition).
/// `None` args leave the existing value untouched.
pub async fn set_open_order_state(
    conn: &mut PgConnection,
    id: &str,
    step: Option<&str>,
    status: Option<&str>,
    reserved_until: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> Result<(), ApiError> {
    sqlx::query(
        "UPDATE inventory.open_order \
         SET step=COALESCE($2, step), status=COALESCE($3, status), \
             reserved_until=COALESCE($4, reserved_until), updated_at=$5 WHERE id=$1",
    )
    .bind(id)
    .bind(step)
    .bind(status)
    .bind(reserved_until)
    .bind(now)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

pub async fn get_esim(pool: &PgPool, iccid: &str) -> Result<Option<EsimRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {ESIM_COLS} FROM inventory.esim_profile WHERE iccid = $1"
    ))
    .bind(iccid)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(esim_row).transpose()
}

pub async fn get_esim_conn(
    conn: &mut PgConnection,
    iccid: &str,
) -> Result<Option<EsimRow>, ApiError> {
    let r = sqlx::query(&format!(
        "SELECT {ESIM_COLS} FROM inventory.esim_profile WHERE iccid = $1"
    ))
    .bind(iccid)
    .fetch_optional(&mut *conn)
    .await?;
    r.as_ref().map(esim_row).transpose()
}

pub async fn list_esims(
    pool: &PgPool,
    status: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<EsimRow>, ApiError> {
    let rows = match status {
        Some(s) => {
            sqlx::query(&format!(
                "SELECT {ESIM_COLS} FROM inventory.esim_profile WHERE profile_state = $1 \
                 ORDER BY iccid LIMIT $2 OFFSET $3"
            ))
            .bind(s)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        None => {
            sqlx::query(&format!(
                "SELECT {ESIM_COLS} FROM inventory.esim_profile ORDER BY iccid LIMIT $1 OFFSET $2"
            ))
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
    };
    rows.iter().map(esim_row).collect()
}

// ── chat transcript ─────────────────────────────────────────────────────────

pub async fn get_transcript(
    pool: &PgPool,
    hash: &str,
) -> Result<Option<ChatTranscriptRow>, ApiError> {
    let r = sqlx::query(
        "SELECT hash, customer_id, body, recorded_at FROM audit.chat_transcript WHERE hash = $1",
    )
    .bind(hash)
    .fetch_optional(pool)
    .await?;
    r.as_ref().map(transcript_row).transpose()
}

pub async fn get_transcript_conn(
    conn: &mut PgConnection,
    hash: &str,
) -> Result<Option<ChatTranscriptRow>, ApiError> {
    let r = sqlx::query(
        "SELECT hash, customer_id, body, recorded_at FROM audit.chat_transcript WHERE hash = $1",
    )
    .bind(hash)
    .fetch_optional(&mut *conn)
    .await?;
    r.as_ref().map(transcript_row).transpose()
}

/// Value helper used by services to fetch `assigned_to_subscription_id`.
pub fn msisdn_assigned_sub(row: &MsisdnRow) -> Option<&str> {
    row.assigned_to_subscription_id.as_deref()
}

/// Unused-import guard for `Value` (kept for the service layer's json helpers).
#[allow(dead_code)]
fn _value_marker(_v: &Value) {}
