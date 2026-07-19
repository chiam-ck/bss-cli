//! Promotion service — the v1.1 create saga + reads (catalog side).
//!
//! Catalog owns the *money terms* (`promotion` row) + the join key to loyalty-cli
//! (which owns the entitlement). Free functions over `(pool, loyalty, actor)`;
//! `loyalty` is `None` when the subsystem is OFF (creates rejected, reads return
//! "no promo" so orders proceed at full price). Saga ordering (BSS row →
//! loyalty → BSS confirm) makes a crash harmless.

use std::collections::HashSet;

use bss_clients::{ClientError, LoyaltyClient};
use bss_db::{PgPool, PolicyViolation};
use rust_decimal::Decimal;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::money::{apply_discount, discount_label};
use crate::promo_repo::{self, PromotionRow};
use crate::repo;

const DISCOUNT_TYPES: [&str; 2] = ["percent", "absolute"];
const DURATION_KINDS: [&str; 3] = ["single", "multi", "perpetual"];
const AUDIENCES: [&str; 2] = ["public", "targeted"];
const PROMO_CODE_KINDS: [&str; 3] = [
    "single_use_unique_per_customer",
    "single_use_shared",
    "multi_use",
];

/// Composed money + display terms (shared by the typed-code and assigned-offer paths).
#[derive(Debug, Clone)]
pub struct Terms {
    pub discount_type: String,
    pub discount_value: Decimal,
    pub duration_kind: String,
    pub periods_total: Option<i16>,
    pub discount_periods_total: i32,
    pub base: Decimal,
    pub effective: Decimal,
    pub label: String,
    pub name: Option<String>,
}

enum Composed {
    Reason(String),
    Terms(Terms),
}

#[derive(Debug, Clone, Default)]
pub struct ValidateResult {
    pub valid: bool,
    pub code: String,
    pub offering_id: String,
    pub reason: Option<String>,
    pub offer_definition_id: Option<String>,
    pub loyalty_offer_id: Option<String>,
    pub terms: Option<Terms>,
}

#[derive(Debug, Clone, Default)]
pub struct ResolveResult {
    pub valid: bool,
    pub reason: Option<String>,
    pub code: Option<String>,
    pub promotion_id: Option<String>,
    pub offer_definition_id: Option<String>,
    pub loyalty_offer_id: Option<String>,
    pub terms: Option<Terms>,
}

fn discount_periods_total(duration_kind: &str, periods_total: Option<i16>) -> i32 {
    match duration_kind {
        "perpetual" => -1,
        "multi" => periods_total.unwrap_or(0) as i32,
        _ => 1,
    }
}

fn offer_definition_id_for(promotion_id: &str) -> String {
    format!("OD_{promotion_id}")
}

// ── reads ─────────────────────────────────────────────────────────────────────

pub async fn get(pool: &PgPool, promotion_id: &str) -> Result<Option<PromotionRow>, ApiError> {
    promo_repo::get(pool, promotion_id).await
}

pub async fn list_promotions(
    pool: &PgPool,
    state: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<PromotionRow>, ApiError> {
    promo_repo::list(pool, state, limit, offset).await
}

/// Apply a promotion's discount to an offering's lowest-active base.
async fn compose(
    pool: &PgPool,
    promo: &PromotionRow,
    offering_id: &str,
) -> Result<Composed, ApiError> {
    if let Some(ids) = &promo.applicable_offering_ids {
        if !ids.iter().any(|i| i == offering_id) {
            return Ok(Composed::Reason("not_applicable_to_offering".into()));
        }
    }
    let now = bss_clock::now();
    if let Some(vf) = promo.valid_from {
        if now < vf {
            return Ok(Composed::Reason("not_yet_valid".into()));
        }
    }
    if let Some(vt) = promo.valid_to {
        if now >= vt {
            return Ok(Composed::Reason("expired".into()));
        }
    }
    let price = match repo::active_price(pool, offering_id, now).await? {
        Some(p) => p,
        None => return Ok(Composed::Reason("offering_not_priced".into())),
    };
    let base = price.amount;
    let effective = apply_discount(&promo.discount_type, promo.discount_value, base)
        .map_err(ApiError::Internal)?;
    let label = discount_label(&promo.discount_type, promo.discount_value, &promo.currency)
        .map_err(ApiError::Internal)?;
    Ok(Composed::Terms(Terms {
        discount_type: promo.discount_type.clone(),
        discount_value: promo.discount_value,
        duration_kind: promo.duration_kind.clone(),
        periods_total: promo.periods_total,
        discount_periods_total: discount_periods_total(&promo.duration_kind, promo.periods_total),
        base,
        effective,
        label,
        name: promo.name.clone(),
    }))
}

/// Resolve a typed code against an offering and compose the effective price.
/// Pure read — never consumes the code. `valid=false` with a `reason` rather than
/// raising, so the portal shows an inline note and the order proceeds full price.
pub async fn validate_for_order(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    code: &str,
    offering_id: &str,
    customer_id: Option<&str>,
) -> Result<ValidateResult, ApiError> {
    let mut r = ValidateResult {
        code: code.to_string(),
        offering_id: offering_id.to_string(),
        ..Default::default()
    };

    let Some(loyalty) = loyalty else {
        r.reason = Some("loyalty_not_configured".into());
        return Ok(r);
    };

    // 1. resolve code → OfferDefinition (loyalty read; no consume)
    let shown = match loyalty.show_promo_code(code).await {
        Ok(v) => v,
        Err(ClientError::NotFound(_)) => {
            r.reason = Some("unknown_code".into());
            return Ok(r);
        }
        Err(ClientError::Policy(pv)) => {
            r.reason = Some(pv.rule);
            return Ok(r);
        }
        Err(e) => return Err(ApiError::Internal(format!("loyalty show_promo_code: {e}"))),
    };
    let od_id = shown
        .get("offer_definition_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if od_id.is_empty() {
        r.reason = Some("unlinked_code".into());
        return Ok(r);
    }

    // 2. money terms by OD
    let promo = match promo_repo::get_by_offer_definition_id(pool, od_id).await? {
        Some(p) if p.state == "active" => p,
        _ => {
            r.reason = Some("no_active_promotion".into());
            return Ok(r);
        }
    };

    // 3. targeted codes are eligibility-gated (BSS is the gate).
    if promo.audience == "targeted" {
        let eligible = match customer_id {
            Some(cid) => promo_repo::is_eligible(pool, &promo.id, cid).await?,
            None => false,
        };
        if !eligible {
            r.reason = Some("not_eligible".into());
            return Ok(r);
        }
        if let Some(cid) = customer_id {
            r.loyalty_offer_id = promo_repo::get_loyalty_offer_id(pool, &promo.id, cid).await?;
        }
    }

    // 4-6. applicability + window + compose
    match compose(pool, &promo, offering_id).await? {
        Composed::Reason(reason) => r.reason = Some(reason),
        Composed::Terms(terms) => {
            r.valid = true;
            r.offer_definition_id = Some(od_id.to_string());
            r.terms = Some(terms);
        }
    }
    Ok(r)
}

/// OfferDefinition ids the customer already claimed/redeemed (drop already-used
/// promos). One loyalty call; a hiccup degrades to "no known usage".
async fn consumed_offer_definitions(
    loyalty: Option<&LoyaltyClient>,
    customer_id: &str,
) -> HashSet<String> {
    let Some(loyalty) = loyalty else {
        return HashSet::new();
    };
    let resp = match loyalty.list_offers(customer_id, Some(100)).await {
        Ok(v) => v,
        Err(_) => {
            tracing::warn!(customer_id, "catalog.promotion.consumed_check_failed");
            return HashSet::new();
        }
    };
    let mut out = HashSet::new();
    if let Some(rows) = resp.get("rows").and_then(Value::as_array) {
        for row in rows {
            let state = row.get("state").and_then(Value::as_str).unwrap_or("");
            if state == "claimed" || state == "redeemed" {
                if let Some(od) = row.get("offer_definition_id").and_then(Value::as_str) {
                    out.insert(od.to_string());
                }
            }
        }
    }
    out
}

/// Auto-apply path: the best targeted promo this customer is eligible for and
/// that applies to `offering_id` (lowest effective price wins).
pub async fn resolve_eligible_promo(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    customer_id: &str,
    offering_id: &str,
) -> Result<ResolveResult, ApiError> {
    if loyalty.is_none() {
        return Ok(ResolveResult {
            valid: false,
            reason: Some("loyalty_not_configured".into()),
            ..Default::default()
        });
    }
    let consumed = consumed_offer_definitions(loyalty, customer_id).await;
    let mut best: Option<ResolveResult> = None;
    for promo in promo_repo::list_eligible_promotions(pool, customer_id).await? {
        if let Some(od) = &promo.offer_definition_id {
            if consumed.contains(od) {
                continue;
            }
        }
        let terms = match compose(pool, &promo, offering_id).await? {
            Composed::Reason(_) => continue,
            Composed::Terms(t) => t,
        };
        let loyalty_offer_id =
            promo_repo::get_loyalty_offer_id(pool, &promo.id, customer_id).await?;
        let candidate = ResolveResult {
            valid: true,
            reason: None,
            code: promo.code.clone(),
            promotion_id: Some(promo.id.clone()),
            offer_definition_id: promo.offer_definition_id.clone(),
            loyalty_offer_id,
            terms: Some(terms),
        };
        let better = match &best {
            None => true,
            Some(b) => {
                candidate.terms.as_ref().map(|t| t.effective)
                    < b.terms.as_ref().map(|t| t.effective)
            }
        };
        if better {
            best = Some(candidate);
        }
    }
    Ok(best.unwrap_or(ResolveResult {
        valid: false,
        reason: Some("no_eligible_promo".into()),
        ..Default::default()
    }))
}

/// Targeted promotions this customer is eligible for (dashboard read). Pure BSS
/// eligibility query; `state` is accepted for back-compat and ignored.
pub async fn list_customer_offers(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    customer_id: &str,
) -> Result<Vec<Value>, ApiError> {
    if loyalty.is_none() {
        return Ok(vec![]);
    }
    let consumed = consumed_offer_definitions(loyalty, customer_id).await;
    let mut out = Vec::new();
    for promo in promo_repo::list_eligible_promotions(pool, customer_id).await? {
        if let Some(od) = &promo.offer_definition_id {
            if consumed.contains(od) {
                continue;
            }
        }
        let label = discount_label(&promo.discount_type, promo.discount_value, &promo.currency)
            .map_err(ApiError::Internal)?;
        out.push(json!({
            "promotion_id": promo.id,
            "code": promo.code,
            "offer_definition_id": promo.offer_definition_id,
            "state": "eligible",
            "promotion": {
                "promotion_id": promo.id,
                "name": promo.name,
                "discount_type": promo.discount_type,
                "discount_value": promo.discount_value.to_string(),
                "duration_kind": promo.duration_kind,
                "periods_total": promo.periods_total,
                "label": label,
            },
        }));
    }
    Ok(out)
}

// ── targeted assignment ───────────────────────────────────────────────────────

pub async fn assign_targeted(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    actor: &str,
    promotion_id: &str,
    customer_ids: &[String],
) -> Result<Value, ApiError> {
    crate::services::check_admin(actor)?;
    let promo = promo_repo::get(pool, promotion_id).await?;
    let promo = match &promo {
        Some(p) if p.state == "active" && p.audience == "targeted" => p,
        other => {
            return Err(PolicyViolation::with_context(
                "catalog.promotion.not_targeted",
                format!(
                    "Promotion {promotion_id} is not an active targeted promo; cannot add eligibility"
                ),
                json!({
                    "promotion_id": promotion_id,
                    "state": other.as_ref().map(|p| p.state.clone()),
                    "audience": other.as_ref().map(|p| p.audience.clone()),
                }),
            )
            .into());
        }
    };

    let mut tx = pool.begin().await?;
    let mut eligible: Vec<String> = Vec::new();
    let mut already: Vec<String> = Vec::new();
    for customer_id in customer_ids {
        if promo_repo::is_eligible_on(&mut tx, promotion_id, customer_id).await? {
            already.push(customer_id.clone());
            continue;
        }
        let offer_id = format!("OFF-{customer_id}-{promotion_id}");
        let mut loyalty_offer_id: Option<String> = None;
        if let (Some(loyalty), Some(od)) = (loyalty, promo.offer_definition_id.as_deref()) {
            match loyalty
                .issue_offer(
                    &offer_id,
                    od,
                    customer_id,
                    json!({ "type": "campaign", "campaign_id": promotion_id }),
                    &offer_id,
                )
                .await
            {
                Ok(_) => loyalty_offer_id = Some(offer_id.clone()),
                Err(e) => tracing::warn!(
                    promotion_id,
                    customer_id,
                    offer_id,
                    error = %e,
                    "catalog.promotion.loyalty_issue_failed_degrade"
                ),
            }
        }
        promo_repo::add_eligibility(
            &mut tx,
            promotion_id,
            customer_id,
            actor,
            loyalty_offer_id.as_deref(),
        )
        .await?;
        eligible.push(customer_id.clone());
    }
    tx.commit().await?;
    tracing::info!(
        promotion_id,
        added = eligible.len(),
        already = already.len(),
        actor,
        "catalog.promotion.eligibility_added"
    );

    Ok(json!({
        "promotion_id": promotion_id,
        "code": promo.code,
        "eligible": eligible,
        "already": already,
    }))
}

pub async fn unassign_targeted(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    actor: &str,
    promotion_id: &str,
    customer_ids: &[String],
) -> Result<Value, ApiError> {
    crate::services::check_admin(actor)?;
    let promo = promo_repo::get(pool, promotion_id).await?;
    let promo = match &promo {
        Some(p) if p.audience == "targeted" => p,
        other => {
            return Err(PolicyViolation::with_context(
                "catalog.promotion.not_targeted",
                format!(
                    "Promotion {promotion_id} is not a targeted promo; cannot remove eligibility"
                ),
                json!({
                    "promotion_id": promotion_id,
                    "audience": other.as_ref().map(|p| p.audience.clone()),
                }),
            )
            .into());
        }
    };

    let mut tx = pool.begin().await?;
    let mut removed: Vec<String> = Vec::new();
    let mut not_eligible: Vec<String> = Vec::new();
    for customer_id in customer_ids {
        let loyalty_offer_id =
            match promo_repo::remove_eligibility(&mut tx, promotion_id, customer_id).await? {
                None => {
                    not_eligible.push(customer_id.clone());
                    continue;
                }
                Some(x) => x,
            };
        // Terminal-transition the upfront-minted loyalty offer (if any).
        if let (Some(offer_id), Some(loyalty)) = (loyalty_offer_id.as_deref(), loyalty) {
            let expire_key = format!("{offer_id}:expire:unassign");
            match loyalty.expire_offer(offer_id, &expire_key).await {
                Ok(_) => {}
                Err(ClientError::Policy(pv)) if pv.rule == "offer.expire.illegal_state" => {
                    // Already past `issued` (customer used it to order) → revoke.
                    let revoke_key = format!("{offer_id}:revoke:unassign");
                    if let Err(e) = loyalty
                        .revoke_offer(offer_id, bss_clients::REVOKE_OPERATOR_ACTION, &revoke_key)
                        .await
                    {
                        tracing::warn!(promotion_id, customer_id, loyalty_offer_id = offer_id, error = %e, "catalog.promotion.loyalty_revoke_failed_drift");
                    }
                }
                Err(e) => {
                    tracing::warn!(promotion_id, customer_id, loyalty_offer_id = offer_id, error = %e, "catalog.promotion.loyalty_expire_failed_drift")
                }
            }
        }
        removed.push(customer_id.clone());
    }
    tx.commit().await?;
    tracing::info!(
        promotion_id,
        removed = removed.len(),
        not_eligible = not_eligible.len(),
        actor,
        "catalog.promotion.eligibility_removed"
    );

    Ok(json!({
        "promotion_id": promotion_id,
        "code": promo.code,
        "removed": removed,
        "not_eligible": not_eligible,
    }))
}

// ── exhaust ───────────────────────────────────────────────────────────────────

/// Flip an `active` promotion to `exhausted` (terminal for new orders, row stays
/// for audit). Idempotent; refuses non-`active` (except the idempotent exhausted).
/// The `catalog.promotion.not_found` rule is caught by the route → 404.
pub async fn exhaust_promotion(
    pool: &PgPool,
    actor: &str,
    promotion_id: &str,
) -> Result<PromotionRow, ApiError> {
    crate::services::check_admin(actor)?;
    let promo = match promo_repo::get(pool, promotion_id).await? {
        Some(p) => p,
        None => {
            return Err(PolicyViolation::with_context(
                "catalog.promotion.not_found",
                format!("Promotion {promotion_id} not found"),
                json!({ "promotion_id": promotion_id }),
            )
            .into());
        }
    };
    if promo.state == "exhausted" {
        return Ok(promo); // idempotent
    }
    if promo.state != "active" {
        return Err(PolicyViolation::with_context(
            "catalog.promotion.exhaust.not_active",
            format!(
                "Promotion {promotion_id} is in state '{}'; only ``active`` promos can be exhausted.",
                promo.state
            ),
            json!({ "promotion_id": promotion_id, "state": promo.state }),
        )
        .into());
    }
    sqlx::query(
        "UPDATE catalog.promotion SET state = 'exhausted', updated_at = now() WHERE id = $1",
    )
    .bind(promotion_id)
    .execute(pool)
    .await?;
    tracing::info!(promotion_id, actor, "catalog.promotion.exhausted");
    promo_repo::get(pool, promotion_id)
        .await?
        .ok_or_else(|| ApiError::Internal("promotion vanished after exhaust".into()))
}

// ── create saga ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct CreatePromotion {
    pub promotion_id: String,
    pub discount_type: String,
    pub discount_value: Decimal,
    pub duration_kind: String,
    pub audience: String,
    pub currency: String,
    pub code: Option<String>,
    pub promo_code_kind: Option<String>,
    pub applicable_offering_ids: Option<Vec<String>>,
    pub periods_total: Option<i16>,
    pub valid_from: Option<chrono::DateTime<chrono::Utc>>,
    pub valid_to: Option<chrono::DateTime<chrono::Utc>>,
    pub display_name: Option<String>,
}

pub async fn create_promotion(
    pool: &PgPool,
    loyalty: Option<&LoyaltyClient>,
    actor: &str,
    mut req: CreatePromotion,
) -> Result<PromotionRow, ApiError> {
    crate::services::check_admin(actor)?;
    let Some(loyalty) = loyalty else {
        return Err(PolicyViolation::with_context(
            "catalog.promotion.loyalty_not_configured",
            "Promotions require loyalty-cli (BSS_LOYALTY_API_TOKEN is unset)",
            json!({ "promotion_id": req.promotion_id }),
        )
        .into());
    };

    // Targeted promos derive sensible loyalty code/kind defaults.
    if req.audience == "targeted" {
        if req.code.is_none() {
            req.code = Some(req.promotion_id.clone());
        }
        if req.promo_code_kind.is_none() {
            req.promo_code_kind = Some("single_use_unique_per_customer".into());
        }
    }

    validate_create(&req)?;

    let existing = promo_repo::get(pool, &req.promotion_id).await?;
    if let Some(ex) = &existing {
        if ex.state != "pending_link" {
            return Err(PolicyViolation::with_context(
                "catalog.promotion.already_exists",
                format!(
                    "Promotion {} already exists (state={})",
                    req.promotion_id, ex.state
                ),
                json!({ "promotion_id": req.promotion_id, "state": ex.state }),
            )
            .into());
        }
    }
    if existing.is_none() {
        if let Some(code) = &req.code {
            if let Some(clash) = promo_repo::get_by_code(pool, code).await? {
                return Err(PolicyViolation::with_context(
                    "catalog.promotion.code_in_use",
                    format!(
                        "Promo code {code} is already bound to promotion {}",
                        clash.id
                    ),
                    json!({ "code": code, "promotion_id": clash.id }),
                )
                .into());
            }
        }
    }

    // ── step 1: write (or resume) the pending_link row ──────────────
    if existing.is_none() {
        sqlx::query(
            "INSERT INTO catalog.promotion \
             (id, code, name, audience, offer_definition_id, discount_type, discount_value, \
              currency, applicable_offering_ids, duration_kind, periods_total, valid_from, valid_to, \
              state, created_by) \
             VALUES ($1,$2,$3,$4,NULL,$5,CAST($6 AS numeric),$7,$8,$9,$10,$11,$12,'pending_link',$13)",
        )
        .bind(&req.promotion_id)
        .bind(&req.code)
        .bind(&req.display_name)
        .bind(&req.audience)
        .bind(&req.discount_type)
        .bind(req.discount_value.to_string())
        .bind(&req.currency)
        .bind(&req.applicable_offering_ids)
        .bind(&req.duration_kind)
        .bind(req.periods_total)
        .bind(req.valid_from)
        .bind(req.valid_to)
        .bind(actor)
        .execute(pool)
        .await?;
        tracing::info!(
            promotion_id = req.promotion_id,
            actor,
            "catalog.promotion.pending"
        );
    }

    // ── steps 2-3: register the loyalty entitlement ─────────────────
    let od_id = offer_definition_id_for(&req.promotion_id);
    let display_name = req
        .display_name
        .clone()
        .unwrap_or_else(|| req.promotion_id.clone());
    let register = async {
        loyalty
            .register_offer_definition(&od_id, &display_name, &format!("{}:od", req.promotion_id))
            .await?;
        if let Some(code) = &req.code {
            let kind = req.promo_code_kind.as_deref().unwrap_or("");
            loyalty
                .register_promo_code(code, &od_id, kind, &format!("{}:code", req.promotion_id))
                .await?;
        }
        Ok::<(), ClientError>(())
    };
    if let Err(e) = register.await {
        // Leave the row pending_link (harmless) and surface as a catalog 422.
        let (loyalty_rule, detail) = match &e {
            ClientError::Policy(pv) => (pv.rule.clone(), pv.message.clone()),
            other => (String::new(), other.to_string()),
        };
        return Err(PolicyViolation::with_context(
            "catalog.promotion.loyalty_refused",
            format!("loyalty refused: {detail}"),
            json!({ "promotion_id": req.promotion_id, "loyalty_rule": loyalty_rule }),
        )
        .into());
    }

    // ── step 4: confirm the link ────────────────────────────────────
    sqlx::query(
        "UPDATE catalog.promotion SET offer_definition_id = $2, state = 'active', updated_at = now() \
         WHERE id = $1",
    )
    .bind(&req.promotion_id)
    .bind(&od_id)
    .execute(pool)
    .await?;
    tracing::info!(promotion_id = req.promotion_id, offer_definition_id = od_id, code = ?req.code, actor, "catalog.promotion.created");

    promo_repo::get(pool, &req.promotion_id)
        .await?
        .ok_or_else(|| ApiError::Internal("promotion vanished after create".into()))
}

fn validate_create(req: &CreatePromotion) -> Result<(), ApiError> {
    let pv = |rule: &str, message: String, ctx: Value| -> ApiError {
        PolicyViolation::with_context(rule, message, ctx).into()
    };
    if !AUDIENCES.contains(&req.audience.as_str()) {
        return Err(pv(
            "catalog.promotion.invalid_audience",
            "audience must be one of [\"public\", \"targeted\"]".into(),
            json!({ "audience": req.audience }),
        ));
    }
    if !DISCOUNT_TYPES.contains(&req.discount_type.as_str()) {
        return Err(pv(
            "catalog.promotion.invalid_discount_type",
            "discount_type must be one of [\"absolute\", \"percent\"]".into(),
            json!({ "discount_type": req.discount_type }),
        ));
    }
    if req.discount_value <= Decimal::ZERO {
        return Err(pv(
            "catalog.promotion.invalid_discount_value",
            "discount_value must be positive".into(),
            json!({ "discount_value": req.discount_value.to_string() }),
        ));
    }
    if req.discount_type == "percent" && req.discount_value > Decimal::from(100) {
        return Err(pv(
            "catalog.promotion.invalid_discount_value",
            "percent discount cannot exceed 100".into(),
            json!({ "discount_value": req.discount_value.to_string() }),
        ));
    }
    if !DURATION_KINDS.contains(&req.duration_kind.as_str()) {
        return Err(pv(
            "catalog.promotion.invalid_duration_kind",
            "duration_kind must be one of [\"multi\", \"perpetual\", \"single\"]".into(),
            json!({ "duration_kind": req.duration_kind }),
        ));
    }
    if req.duration_kind == "multi" {
        if req.periods_total.map(|p| p < 2).unwrap_or(true) {
            return Err(pv(
                "catalog.promotion.invalid_periods_total",
                "multi-period promo requires periods_total >= 2".into(),
                json!({ "periods_total": req.periods_total }),
            ));
        }
    } else if req.periods_total.is_some() {
        return Err(pv(
            "catalog.promotion.invalid_periods_total",
            format!("{} promo must not set periods_total", req.duration_kind),
            json!({ "duration_kind": req.duration_kind, "periods_total": req.periods_total }),
        ));
    }
    match &req.code {
        None => {
            return Err(pv(
                "catalog.promotion.requires_code",
                "a promotion requires a code (targeted codes are derived if omitted)".into(),
                json!({ "audience": req.audience }),
            ));
        }
        Some(c) if c.is_empty() => {
            return Err(pv(
                "catalog.promotion.requires_code",
                "a promotion requires a code (targeted codes are derived if omitted)".into(),
                json!({ "audience": req.audience }),
            ));
        }
        Some(_) => {}
    }
    let kind = req.promo_code_kind.as_deref().unwrap_or("");
    if !PROMO_CODE_KINDS.contains(&kind) {
        return Err(pv(
            "catalog.promotion.invalid_promo_code_kind",
            "promo_code_kind must be one of [\"multi_use\", \"single_use_shared\", \"single_use_unique_per_customer\"]".into(),
            json!({ "promo_code_kind": req.promo_code_kind }),
        ));
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn discount_periods_total_matches_python() {
        assert_eq!(discount_periods_total("perpetual", None), -1);
        assert_eq!(discount_periods_total("perpetual", Some(3)), -1);
        assert_eq!(discount_periods_total("multi", Some(3)), 3);
        assert_eq!(discount_periods_total("multi", None), 0);
        assert_eq!(discount_periods_total("single", None), 1);
    }

    fn base_req() -> CreatePromotion {
        CreatePromotion {
            promotion_id: "PROMO_X".into(),
            discount_type: "percent".into(),
            discount_value: Decimal::from(20),
            duration_kind: "single".into(),
            audience: "public".into(),
            currency: "SGD".into(),
            code: Some("CODE_X".into()),
            promo_code_kind: Some("single_use_shared".into()),
            applicable_offering_ids: None,
            periods_total: None,
            valid_from: None,
            valid_to: None,
            display_name: None,
        }
    }

    fn rule_of(e: ApiError) -> String {
        match e {
            ApiError::Policy(pv) => pv.rule,
            other => panic!("expected policy, got {other:?}"),
        }
    }

    #[test]
    fn validate_accepts_a_good_public_promo() {
        assert!(validate_create(&base_req()).is_ok());
    }

    #[test]
    fn validate_rejects_bad_audience_type_value() {
        let mut r = base_req();
        r.audience = "everyone".into();
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_audience"
        );

        let mut r = base_req();
        r.discount_type = "freebie".into();
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_discount_type"
        );

        let mut r = base_req();
        r.discount_value = Decimal::from(150);
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_discount_value"
        );
    }

    #[test]
    fn validate_multi_requires_two_periods() {
        let mut r = base_req();
        r.duration_kind = "multi".into();
        r.periods_total = Some(1);
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_periods_total"
        );
        r.periods_total = Some(3);
        assert!(validate_create(&r).is_ok());
    }

    #[test]
    fn validate_single_rejects_periods_total() {
        let mut r = base_req();
        r.periods_total = Some(3);
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_periods_total"
        );
    }

    #[test]
    fn validate_rejects_bad_promo_code_kind() {
        let mut r = base_req();
        r.promo_code_kind = Some("weird".into());
        assert_eq!(
            rule_of(validate_create(&r).unwrap_err()),
            "catalog.promotion.invalid_promo_code_kind"
        );
    }
}
