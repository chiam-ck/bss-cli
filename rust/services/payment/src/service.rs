//! Orchestration — port of `PaymentService` + `PaymentMethodService`.
//!
//! Router → here → policies → repo → event stage → commit. One `bss-clients`
//! write chokepoint (CRM existence check); the tokenizer seam is injected via
//! `AppState`. Each write runs in a single transaction; the response is built
//! from the persisted row (re-fetched post-commit) so server defaults
//! (`created_at = now()`) render exactly as the oracle's flushed ORM object.

use bss_db::PolicyViolation;
use rust_decimal::Decimal;
use serde_json::{json, Value};

use crate::domain::event_type_for_status;
use crate::error::ApiError;
use crate::events::stage;
use crate::policies;
use crate::repo::{self, PaymentAttemptRow, PaymentMethodRow};
use crate::schemas::PaymentMethodCreateRequest;
use crate::state::AppState;
use bss_context::RequestCtx;

fn internal(e: impl std::fmt::Display) -> ApiError {
    ApiError::Internal(e.to_string())
}

// ── charge ───────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub async fn charge(
    state: &AppState,
    ctx: &RequestCtx,
    customer_id: &str,
    payment_method_id: &str,
    amount: Decimal,
    currency: &str,
    purpose: &str,
) -> Result<PaymentAttemptRow, ApiError> {
    let method = repo::get_method(&state.pool, payment_method_id)
        .await?
        .ok_or_else(|| {
            ApiError::Policy(PolicyViolation::with_context(
                "payment.charge.method_not_found",
                format!("Payment method {payment_method_id} not found"),
                json!({ "payment_method_id": payment_method_id }),
            ))
        })?;

    policies::check_method_active(&method)?;
    policies::check_positive_amount(&amount)?;
    policies::check_customer_matches_method(customer_id, &method)?;
    policies::check_token_provider_matches_active(&method, state.tokenizer.class_name())?;

    // provider-side customer ref (None for mock; cus_* for stripe once cached).
    let customer_external_ref =
        repo::lookup_customer_external_ref(&state.pool, customer_id).await?;

    let mut tx = state.pool.begin().await?;
    let attempt_id = repo::next_attempt_id(&mut tx).await?;
    let now = bss_clock::now();
    let idempotency_key = format!("ATT-{attempt_id}-r0");

    let charge_result = state
        .tokenizer
        .charge(
            &method.token,
            &amount,
            currency,
            &idempotency_key,
            purpose,
            customer_external_ref.as_deref(),
        )
        .await
        .map_err(internal)?;

    repo::insert_attempt(
        &mut tx,
        &attempt_id,
        customer_id,
        payment_method_id,
        &amount,
        currency,
        purpose,
        &charge_result.status,
        &charge_result.gateway_ref,
        charge_result.reason.as_deref(),
        &charge_result.provider_call_id,
        charge_result.decline_code.as_deref(),
        &idempotency_key,
        now,
        &ctx.tenant,
    )
    .await?;

    let event_type = event_type_for_status(&charge_result.status);
    stage(
        &mut tx,
        ctx,
        event_type,
        "payment_attempt",
        &attempt_id,
        json!({
            "customer_id": customer_id,
            "payment_method_id": payment_method_id,
            "amount": amount.to_string(),
            "currency": currency,
            "purpose": purpose,
            "status": charge_result.status,
            "gateway_ref": charge_result.gateway_ref,
            "provider_call_id": charge_result.provider_call_id,
            "decline_code": charge_result.decline_code,
        }),
    )
    .await?;

    tx.commit().await?;
    tracing::info!(
        attempt_id = attempt_id,
        amount = %amount,
        purpose = purpose,
        "payment.{}",
        charge_result.status
    );

    Ok(PaymentAttemptRow {
        id: attempt_id,
        customer_id: customer_id.to_string(),
        payment_method_id: payment_method_id.to_string(),
        amount,
        currency: currency.to_string(),
        purpose: purpose.to_string(),
        status: charge_result.status,
        gateway_ref: Some(charge_result.gateway_ref),
        decline_reason: charge_result.reason,
        attempted_at: now,
    })
}

// ── register payment method ──────────────────────────────────────────

pub async fn register_method(
    state: &AppState,
    ctx: &RequestCtx,
    body: PaymentMethodCreateRequest,
) -> Result<PaymentMethodRow, ApiError> {
    let customer = policies::check_customer_exists(&body.customer_id, &state.crm).await?;
    policies::check_customer_active_or_pending(&customer)?;

    let token_provider = if body.tokenization_provider == "stripe" {
        "stripe"
    } else {
        "mock"
    };
    if body.tokenization_provider != "stripe" {
        policies::check_card_not_expired(body.card_summary.exp_month, body.card_summary.exp_year)?;
    }

    let mut conn = state.pool.acquire().await?;
    let active_count = repo::count_active_methods(&mut conn, &body.customer_id).await?;
    policies::check_at_most_n_methods(&body.customer_id, active_count)?;
    let is_default = active_count == 0;
    drop(conn);

    // Card metadata: default to the request, overwritten from Stripe on the
    // stripe path (the portal sends placeholders; Stripe holds the truth).
    let mut last4 = body.card_summary.last4.clone();
    let mut brand = body.card_summary.brand.clone();
    let mut exp_month = body.card_summary.exp_month;
    let mut exp_year = body.card_summary.exp_year;

    if token_provider == "stripe" {
        let email = customer_email(&customer, &body.customer_id);
        let cus_ref = state
            .tokenizer
            .ensure_customer(&body.customer_id, &email)
            .await
            .map_err(internal)?;
        state
            .tokenizer
            .attach_payment_method_to_customer(&body.provider_token, &cus_ref)
            .await
            .map_err(internal)?;
        // Best-effort card-detail fetch (the oracle's try/except fallback).
        match state
            .tokenizer
            .retrieve_payment_method_card(&body.provider_token)
            .await
        {
            Ok(card) => {
                if !card.last4.is_empty() {
                    last4 = card.last4;
                } else if last4.is_empty() {
                    last4 = "stripe".to_string();
                }
                if !card.brand.is_empty() {
                    brand = card.brand;
                } else if brand.is_empty() {
                    brand = "card".to_string();
                }
                exp_month = card.exp_month.filter(|v| *v != 0).unwrap_or(exp_month);
                exp_year = card.exp_year.filter(|v| *v != 0).unwrap_or(exp_year);
            }
            Err(e) => {
                tracing::warn!(
                    payment_method_id = body.provider_token,
                    error = %e,
                    "payment_method.stripe_card_details_fetch_failed"
                );
                if exp_month == 0 {
                    exp_month = 12;
                }
                if exp_year == 0 {
                    exp_year = 2099;
                }
                if last4.is_empty() {
                    last4 = "stripe".to_string();
                }
                if brand.is_empty() {
                    brand = "card".to_string();
                }
            }
        }
    }

    let mut tx = state.pool.begin().await?;
    let pm_id = repo::next_method_id(&mut tx).await?;
    repo::insert_method(
        &mut tx,
        &pm_id,
        &body.customer_id,
        &body.type_,
        &body.provider_token,
        token_provider,
        &last4,
        &brand,
        exp_month as i16,
        exp_year as i16,
        is_default,
        &ctx.tenant,
    )
    .await?;
    stage(
        &mut tx,
        ctx,
        "payment_method.added",
        "payment_method",
        &pm_id,
        json!({
            "customer_id": body.customer_id,
            "brand": brand,
            "last4": last4,
            "tokenization_provider": body.tokenization_provider,
            "token_provider": token_provider,
        }),
    )
    .await?;
    tx.commit().await?;

    tracing::info!(
        pm_id,
        customer_id = body.customer_id,
        token_provider,
        "payment_method.registered"
    );
    repo::get_method(&state.pool, &pm_id)
        .await?
        .ok_or_else(|| ApiError::Internal("payment_method vanished after insert".into()))
}

fn customer_email(customer: &Value, customer_id: &str) -> String {
    // contactMedium[0].emailAddress, else top-level email, else the synthetic.
    if let Some(email) = customer
        .get("contactMedium")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .and_then(|m| m.get("emailAddress"))
        .and_then(Value::as_str)
    {
        return email.to_string();
    }
    if let Some(email) = customer.get("email").and_then(Value::as_str) {
        return email.to_string();
    }
    format!("{customer_id}@bss-cli.local")
}

// ── set default ──────────────────────────────────────────────────────

pub async fn set_default_method(
    state: &AppState,
    ctx: &RequestCtx,
    pm_id: &str,
) -> Result<PaymentMethodRow, ApiError> {
    let pm = repo::get_method(&state.pool, pm_id).await?.ok_or_else(|| {
        ApiError::Policy(PolicyViolation::with_context(
            "payment_method.set_default.not_found",
            format!("Payment method {pm_id} not found"),
            json!({ "payment_method_id": pm_id }),
        ))
    })?;
    if pm.status == "removed" {
        return Err(ApiError::Policy(PolicyViolation::with_context(
            "payment_method.set_default.removed",
            format!("Payment method {pm_id} has been removed"),
            json!({ "payment_method_id": pm_id }),
        )));
    }

    let mut tx = state.pool.begin().await?;
    repo::set_default(&mut tx, &pm.customer_id, pm_id).await?;
    stage(
        &mut tx,
        ctx,
        "payment_method.default_changed",
        "payment_method",
        pm_id,
        json!({ "customer_id": pm.customer_id, "last4": pm.last4 }),
    )
    .await?;
    tx.commit().await?;

    tracing::info!(
        pm_id,
        customer_id = pm.customer_id,
        "payment_method.default_changed"
    );
    repo::get_method(&state.pool, pm_id)
        .await?
        .ok_or_else(|| ApiError::Internal("payment_method vanished after set_default".into()))
}

// ── remove ───────────────────────────────────────────────────────────

pub async fn remove_method(
    state: &AppState,
    ctx: &RequestCtx,
    pm_id: &str,
) -> Result<PaymentMethodRow, ApiError> {
    let pm = repo::get_method(&state.pool, pm_id).await?.ok_or_else(|| {
        ApiError::Policy(PolicyViolation::with_context(
            "payment_method.remove.not_found",
            format!("Payment method {pm_id} not found"),
            json!({ "payment_method_id": pm_id }),
        ))
    })?;
    if pm.status == "removed" {
        return Err(ApiError::Policy(PolicyViolation::with_context(
            "payment_method.remove.already_removed",
            format!("Payment method {pm_id} is already removed"),
            json!({ "payment_method_id": pm_id }),
        )));
    }
    // check_not_last_if_active_subscription — STUB in the oracle (always allows).

    let mut tx = state.pool.begin().await?;
    repo::set_method_status(&mut tx, pm_id, "removed").await?;
    stage(
        &mut tx,
        ctx,
        "payment_method.removed",
        "payment_method",
        pm_id,
        json!({ "customer_id": pm.customer_id, "last4": pm.last4 }),
    )
    .await?;
    tx.commit().await?;

    tracing::info!(pm_id, "payment_method.removed");
    repo::get_method(&state.pool, pm_id)
        .await?
        .ok_or_else(|| ApiError::Internal("payment_method vanished after remove".into()))
}

// ── v0.16 cutover ────────────────────────────────────────────────────

pub async fn cutover_invalidate_mock_tokens(
    state: &AppState,
    ctx: &RequestCtx,
    dry_run: bool,
) -> Result<Value, ApiError> {
    let mut tx = state.pool.begin().await?;
    let rows = repo::list_active_mock_methods(&mut tx).await?;
    let affected_ids: Vec<String> = rows.iter().map(|r| r.0.clone()).collect();

    if dry_run || affected_ids.is_empty() {
        tx.rollback().await.ok();
        tracing::info!(
            dry_run,
            count = affected_ids.len(),
            "payment_method.cutover_invalidated"
        );
        return Ok(json!({
            "candidate_count": affected_ids.len(),
            "candidate_ids": affected_ids,
            "invalidated_count": 0,
            "invalidated_ids": [],
        }));
    }

    for (id, customer_id, last4, brand, token_provider) in &rows {
        repo::set_method_status(&mut tx, id, "expired").await?;
        stage(
            &mut tx,
            ctx,
            "payment_method.cutover_invalidated",
            "payment_method",
            id,
            json!({
                "customer_id": customer_id,
                "last4": last4,
                "brand": brand,
                "token_provider": token_provider,
                "reason": "operator_cutover",
            }),
        )
        .await?;
    }
    tx.commit().await?;
    tracing::info!(
        count = affected_ids.len(),
        dry_run = false,
        "payment_method.cutover_invalidated"
    );

    Ok(json!({
        "candidate_count": affected_ids.len(),
        "candidate_ids": affected_ids.clone(),
        "invalidated_count": affected_ids.len(),
        "invalidated_ids": affected_ids,
    }))
}
