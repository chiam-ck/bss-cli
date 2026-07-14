//! Customer-scoped `*.mine` / `*_for_me` tool wrappers — the prompt-injection
//! containment layer (v0.12). Port of `orchestrator/bss_orchestrator/tools/
//! mine_wrappers.py`.
//!
//! Every tool binds `customer_id` from `ctx.actor` (never a parameter), pre-checks
//! ownership against that bound actor for resource-scoped calls, and calls the same
//! ported client methods the operator tools use. They add no server-side capability
//! — they narrow the prompt-visible surface so a chat LLM cannot even *attempt* to
//! act on another customer; server-side policies remain the real boundary.

use std::sync::Arc;

use bss_clients::{CrmClient, MediationClient, PaymentClient, SubscriptionClient};
use futures_util::future::FutureExt;
use rust_decimal::Decimal;
use serde_json::{json, Value};
use std::str::FromStr;

use super::{
    map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolCtx, ToolError, ToolRegistry,
};

macro_rules! desc {
    ($f:literal) => {
        include_str!(concat!("desc/", $f, ".txt"))
    };
}

// ── shared machinery ─────────────────────────────────────────────────────────

/// The bound customer id, or the `_NoActorBound` observation. In the chat path the
/// route sets `ctx.actor = customer_id`; the default `"system"` (or empty) means the
/// tool ran outside a customer-scoped session — the Rust analogue of Python's
/// `auth_context.current().actor` defaulting to `None`.
fn require_actor(ctx: &ToolCtx) -> Result<String, ToolError> {
    let a = ctx.actor.trim();
    if a.is_empty() || a == "system" {
        return Err(ToolError::Other {
            kind: "_NoActorBound".to_string(),
            detail: "chat.no_actor_bound: this tool can only run inside a \
                     customer-scoped chat session"
                .to_string(),
        });
    }
    Ok(a.to_string())
}

/// Fetch the subscription and confirm it belongs to `actor`, else the
/// `_NotOwnedByActor` observation (identical shape irrespective of which `*.mine`
/// tool tried — a prompt-injection attempt never leaks a foreign subscription dict).
async fn assert_subscription_owned(
    sub: &SubscriptionClient,
    subscription_id: &str,
    actor: &str,
) -> Result<Value, ToolError> {
    let s = sub.get(subscription_id).await.map_err(map_err)?;
    let owner = s
        .get("customerId")
        .or_else(|| s.get("customer_id"))
        .and_then(Value::as_str);
    if owner != Some(actor) {
        return Err(ToolError::Other {
            kind: "_NotOwnedByActor".to_string(),
            detail: format!(
                "policy.subscription.not_owned_by_actor: subscription {subscription_id} is not yours"
            ),
        });
    }
    Ok(s)
}

/// `f"{current}"` for a money `Value` (string → inner, number → its digits).
fn render_money(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        _ => v.to_string(),
    }
}

/// Human label for a promo discount — port of `_discount_label` (`Decimal` math via
/// `rust_decimal`: `normalize()` strips trailing zeros for percent, `round_dp(2)`
/// quantizes absolute amounts).
fn discount_label(dtype: &str, dval: &Value, currency: &str) -> String {
    let v = Decimal::from_str(&render_money(dval)).unwrap_or_default();
    if dtype == "percent" {
        // `format(v.normalize(), 'f')` — strip trailing zeros, fixed notation.
        format!("{}% off", v.normalize())
    } else {
        // `v.quantize(Decimal('0.01'))` — always two decimal places.
        format!("{currency} {v:.2} off")
    }
}

/// Surface the *current* monthly charge + an active-discount note on a subscription
/// dict the chat sees (effective price when a promo is live). Port of
/// `_annotate_pricing`; code-enforced at the seam (the prompt alone isn't reliable
/// on small models).
fn annotate_pricing(mut sub: Value) -> Value {
    let Some(obj) = sub.as_object_mut() else {
        return sub;
    };
    let price = obj.get("priceAmount").cloned().unwrap_or(Value::Null);
    let effective = obj.get("effectiveAmount").cloned().unwrap_or(Value::Null);
    let currency = obj
        .get("priceCurrency")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("SGD")
        .to_string();
    let dtype = obj
        .get("discountType")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(String::from);
    let dval = obj.get("discountValue").cloned().unwrap_or(Value::Null);
    let remaining = obj
        .get("discountPeriodsRemaining")
        .and_then(Value::as_i64)
        .unwrap_or(0);

    let current = if !effective.is_null() {
        &effective
    } else {
        &price
    };
    let cmc = if current.is_null() {
        Value::Null
    } else {
        json!(format!("{currency} {}", render_money(current)))
    };
    obj.insert("currentMonthlyCharge".to_string(), cmc);

    let active = match dtype.as_deref() {
        Some(dt) if !dval.is_null() && remaining != 0 => {
            let label = discount_label(dt, &dval, &currency);
            if remaining == -1 {
                json!(format!("{label} (ongoing)"))
            } else {
                let plural = if remaining == 1 { "" } else { "s" };
                json!(format!(
                    "{label} — {remaining} more renewal{plural} at this price, then {currency} {}/mo",
                    render_money(&price)
                ))
            }
        }
        _ => Value::Null,
    };
    obj.insert("activeDiscount".to_string(), active);
    sub
}

/// v0.12 `EscalationCategory` → CRM `CaseCategory` (queue routing).
fn escalation_category(category: &str) -> &'static str {
    match category {
        "fraud" | "regulator_complaint" | "identity_recovery" | "bereavement" => "account",
        "billing_dispute" => "billing",
        _ => "information", // "other"
    }
}

/// Default priority per escalation category.
fn escalation_priority(category: &str) -> &'static str {
    match category {
        "fraud" | "regulator_complaint" | "identity_recovery" => "high",
        _ => "medium", // billing_dispute / bereavement / other
    }
}

fn sha256_hex(s: &str) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(s.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

// ── registration ─────────────────────────────────────────────────────────────

/// Register all 14 `customer_self_serve` `*.mine` / `*_for_me` tools. Each captures
/// the ported clients it needs and binds the actor from `ctx`.
pub fn register_customer_self_serve_tools(
    registry: &mut ToolRegistry,
    sub: SubscriptionClient,
    crm: CrmClient,
    payment: PaymentClient,
    mediation: MediationClient,
) {
    // ── subscription reads (ownership-bound) ─────────────────────────────────
    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.list_mine".to_string(),
        description: desc!("subscription_list_mine").to_string(),
        func: Arc::new(move |_args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let subs = s.list_for_customer(&actor).await.map_err(map_err)?;
                let out: Vec<Value> = subs
                    .as_array()
                    .map(|a| a.iter().cloned().map(annotate_pricing).collect())
                    .unwrap_or_default();
                Ok(Value::Array(out))
            }
            .boxed()
        }),
    });

    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.get_mine".to_string(),
        description: desc!("subscription_get_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                let sub = assert_subscription_owned(&s, &id, &actor).await?;
                Ok(annotate_pricing(sub))
            }
            .boxed()
        }),
    });

    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.get_balance_mine".to_string(),
        description: desc!("subscription_get_balance_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.get_balance(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.get_lpa_mine".to_string(),
        description: desc!("subscription_get_lpa_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.get_esim_activation(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // ── usage (ownership-bound; fan-out when no subscription given) ───────────
    let s = sub.clone();
    let m = mediation.clone();
    registry.register(RegisteredTool {
        name: "usage.history_mine".to_string(),
        description: desc!("usage_history_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            let m = m.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let subscription_id = opt_str(&args, "subscription_id");
                let event_type = opt_str(&args, "event_type");
                let since = opt_str(&args, "since");
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(100);
                if let Some(sid) = subscription_id {
                    assert_subscription_owned(&s, &sid, &actor).await?;
                    return m
                        .list_usage(
                            Some(&sid),
                            None,
                            event_type.as_deref(),
                            since.as_deref(),
                            limit,
                        )
                        .await
                        .map_err(map_err);
                }
                // No specific line — fan out across the actor's subscriptions + merge.
                let subs = s.list_for_customer(&actor).await.map_err(map_err)?;
                let mut results: Vec<Value> = Vec::new();
                for sub in subs.as_array().unwrap_or(&Vec::new()) {
                    let Some(sub_id) = sub.get("id").and_then(Value::as_str) else {
                        continue;
                    };
                    let rows = m
                        .list_usage(
                            Some(sub_id),
                            None,
                            event_type.as_deref(),
                            since.as_deref(),
                            limit,
                        )
                        .await
                        .map_err(map_err)?;
                    if let Some(arr) = rows.as_array() {
                        results.extend(arr.iter().cloned());
                    }
                }
                // Newest-first by eventTime, capped at limit (stable sort).
                results.sort_by_key(|r| std::cmp::Reverse(event_time(r)));
                results.truncate(limit.max(0) as usize);
                Ok(Value::Array(results))
            }
            .boxed()
        }),
    });

    // ── customer + payment reads ─────────────────────────────────────────────
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.get_mine".to_string(),
        description: desc!("customer_get_mine").to_string(),
        func: Arc::new(move |_args, ctx| {
            let c = c.clone();
            async move {
                let actor = require_actor(&ctx)?;
                c.get_customer(&actor).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let p = payment.clone();
    registry.register(RegisteredTool {
        name: "payment.method_list_mine".to_string(),
        description: desc!("payment_method_list_mine").to_string(),
        func: Arc::new(move |_args, ctx| {
            let p = p.clone();
            async move {
                let actor = require_actor(&ctx)?;
                p.list_methods(&actor).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let p = payment.clone();
    registry.register(RegisteredTool {
        name: "payment.charge_history_mine".to_string(),
        description: desc!("payment_charge_history_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let p = p.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(20);
                p.list_payments(Some(&actor), None, limit, 0)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // ── writes (ownership-bound) ─────────────────────────────────────────────
    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "vas.purchase_for_me".to_string(),
        description: desc!("vas_purchase_for_me").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                let vas = req_str(&args, "vas_offering_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.purchase_vas(&id, &vas).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.schedule_plan_change_mine".to_string(),
        description: desc!("subscription_schedule_plan_change_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                let new_offering = req_str(&args, "new_offering_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.schedule_plan_change(&id, &new_offering)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let s = sub.clone();
    registry.register(RegisteredTool {
        name: "subscription.cancel_pending_plan_change_mine".to_string(),
        description: desc!("subscription_cancel_pending_plan_change_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.cancel_plan_change(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // subscription.terminate_mine — destructive; carries reason "customer_chat".
    let s = sub;
    registry.register(RegisteredTool {
        name: "subscription.terminate_mine".to_string(),
        description: desc!("subscription_terminate_mine").to_string(),
        func: Arc::new(move |args, ctx| {
            let s = s.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let id = req_str(&args, "subscription_id")?;
                assert_subscription_owned(&s, &id, &actor).await?;
                s.terminate_with_reason(&id, Some("customer_chat"), true)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // ── escalation ───────────────────────────────────────────────────────────
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "case.list_for_me".to_string(),
        description: desc!("case_list_for_me").to_string(),
        func: Arc::new(move |args, ctx| {
            let c = c.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let state = opt_str(&args, "state");
                c.list_cases(Some(&actor), state.as_deref(), None)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // case.open_for_me — hash + store the transcript, then open the case with the
    // escalation category/priority mapping and the `[category] …` description.
    let c = crm;
    registry.register(RegisteredTool {
        name: "case.open_for_me".to_string(),
        description: desc!("case_open_for_me").to_string(),
        func: Arc::new(move |args, ctx| {
            let c = c.clone();
            async move {
                let actor = require_actor(&ctx)?;
                let category = req_str(&args, "category")?;
                let subject = req_str(&args, "subject")?;
                let description = req_str(&args, "description")?;
                let transcript = ctx.transcript.clone();
                let hash = sha256_hex(&transcript);
                // Persist the transcript first (idempotent on the hash PK).
                c.store_chat_transcript(&hash, &actor, &transcript)
                    .await
                    .map_err(map_err)?;
                c.open_case(
                    &actor,
                    &subject,
                    escalation_category(&category),
                    escalation_priority(&category),
                    Some(&format!("[{category}] {description}")),
                    None,
                    Some(&hash),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// The `eventTime` (or snake `event_time`) sort key, empty string when absent.
fn event_time(row: &Value) -> String {
    row.get("eventTime")
        .or_else(|| row.get("event_time"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn annotate_pricing_effective_and_discount() {
        // Percent discount, 2 renewals remaining.
        let sub = json!({
            "priceAmount": "25.00", "effectiveAmount": "20.00", "priceCurrency": "SGD",
            "discountType": "percent", "discountValue": "20", "discountPeriodsRemaining": 2
        });
        let out = annotate_pricing(sub);
        assert_eq!(out["currentMonthlyCharge"], json!("SGD 20.00"));
        assert_eq!(
            out["activeDiscount"],
            json!("20% off — 2 more renewals at this price, then SGD 25.00/mo")
        );
    }

    #[test]
    fn annotate_pricing_no_discount_uses_price() {
        let sub = json!({ "priceAmount": "25.00", "priceCurrency": "SGD" });
        let out = annotate_pricing(sub);
        assert_eq!(out["currentMonthlyCharge"], json!("SGD 25.00"));
        assert_eq!(out["activeDiscount"], Value::Null);
    }

    #[test]
    fn ongoing_and_singular_labels() {
        let ongoing = annotate_pricing(json!({
            "priceAmount": "10", "priceCurrency": "SGD",
            "discountType": "absolute", "discountValue": "3", "discountPeriodsRemaining": -1
        }));
        assert_eq!(ongoing["activeDiscount"], json!("SGD 3.00 off (ongoing)"));

        let one = annotate_pricing(json!({
            "priceAmount": "10", "priceCurrency": "SGD",
            "discountType": "percent", "discountValue": "5", "discountPeriodsRemaining": 1
        }));
        assert_eq!(
            one["activeDiscount"],
            json!("5% off — 1 more renewal at this price, then SGD 10/mo")
        );
    }

    #[test]
    fn escalation_maps() {
        assert_eq!(escalation_category("fraud"), "account");
        assert_eq!(escalation_category("billing_dispute"), "billing");
        assert_eq!(escalation_category("other"), "information");
        assert_eq!(escalation_priority("fraud"), "high");
        assert_eq!(escalation_priority("billing_dispute"), "medium");
    }
}
