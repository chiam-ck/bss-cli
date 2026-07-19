//! Decomposition — port of `app.services.decomposition.decompose_order`.
//!
//! Breaks a commercial order into ServiceOrder → CFS → RFS(Data, Voice) +
//! atomic MSISDN/eSIM reservation, then stages `service_order.in_progress` and
//! the four `provisioning.task.created` events (the relay delivers them). Runs on
//! the consumer's transaction: any error rolls the whole graph back, and reserved
//! inventory is released best-effort before returning.

use bss_clients::InventoryClient;
use bss_context::RequestCtx;
use serde_json::{json, Value};
use sqlx::postgres::PgConnection;

use crate::domain::{check_service_order_transition, check_service_transition, TASK_TYPES};
use crate::events::stage;
use crate::repo;

/// The `order.in_progress` payload SOM decomposes.
pub struct DecomposeRequest {
    pub commercial_order_id: String,
    pub customer_id: String,
    pub offering_id: String,
    pub msisdn_preference: Option<String>,
    pub payment_method_id: String,
    pub price_snapshot: Option<Value>,
}

/// Decompose one commercial order. Returns `Err(reason)` on any failure (the
/// consumer retries/parks; the transaction rolls back).
pub async fn decompose_order(
    conn: &mut PgConnection,
    inventory: &InventoryClient,
    ctx: &RequestCtx,
    req: &DecomposeRequest,
) -> Result<(), String> {
    let tenant = &ctx.tenant;

    // 1. ServiceOrder (acknowledged).
    let so_id = repo::next_service_order_id(conn).await.map_err(db)?;
    repo::insert_service_order(
        conn,
        &so_id,
        &req.commercial_order_id,
        "acknowledged",
        tenant,
    )
    .await
    .map_err(db)?;

    // 2. ServiceOrderItem.
    let soi_id = repo::next_service_order_item_id(conn).await.map_err(db)?;
    repo::insert_service_order_item(
        conn,
        &soi_id,
        &so_id,
        "add",
        "MobileBroadband",
        None,
        tenant,
    )
    .await
    .map_err(db)?;

    // 3. CFS (designed).
    let cfs_id = repo::next_service_id(conn).await.map_err(db)?;
    repo::insert_service(
        conn,
        &cfs_id,
        "MobileBroadband",
        "CFS",
        None,
        "designed",
        &json!({}),
        tenant,
    )
    .await
    .map_err(db)?;
    repo::add_state_history(
        conn,
        &cfs_id,
        None,
        "designed",
        &ctx.actor,
        "decomposition",
        tenant,
    )
    .await
    .map_err(db)?;

    // 4-5. RFS Data + Voice (designed).
    let rfs_data_id = repo::next_service_id(conn).await.map_err(db)?;
    repo::insert_service(
        conn,
        &rfs_data_id,
        "DataService",
        "RFS",
        Some(&cfs_id),
        "designed",
        &json!({}),
        tenant,
    )
    .await
    .map_err(db)?;
    repo::add_state_history(
        conn,
        &rfs_data_id,
        None,
        "designed",
        &ctx.actor,
        "decomposition",
        tenant,
    )
    .await
    .map_err(db)?;

    let rfs_voice_id = repo::next_service_id(conn).await.map_err(db)?;
    repo::insert_service(
        conn,
        &rfs_voice_id,
        "VoiceService",
        "RFS",
        Some(&cfs_id),
        "designed",
        &json!({}),
        tenant,
    )
    .await
    .map_err(db)?;
    repo::add_state_history(
        conn,
        &rfs_voice_id,
        None,
        "designed",
        &ctx.actor,
        "decomposition",
        tenant,
    )
    .await
    .map_err(db)?;

    // Link SOI → CFS.
    repo::set_soi_target(conn, &soi_id, &cfs_id)
        .await
        .map_err(db)?;

    // RFS → reserved.
    for rfs in [&rfs_data_id, &rfs_voice_id] {
        check_service_transition("designed", "reserved").map_err(policy)?;
        repo::set_service_state(conn, rfs, "reserved")
            .await
            .map_err(db)?;
        repo::add_state_history(
            conn,
            rfs,
            Some("designed"),
            "reserved",
            &ctx.actor,
            "inventory reserved",
            tenant,
        )
        .await
        .map_err(db)?;
    }

    // 6-7. Reserve inventory (MSISDN + eSIM), rolling back on failure.
    let msisdn_result = inventory
        .reserve_next_msisdn(req.msisdn_preference.as_deref())
        .await
        .map_err(|e| format!("reserve_next_msisdn failed: {e}"))?;
    let esim_result = match inventory.reserve_esim().await {
        Ok(v) => v,
        Err(e) => {
            // Release the MSISDN we already took before bailing.
            if let Some(m) = msisdn_result.get("msisdn").and_then(Value::as_str) {
                if let Err(re) = inventory.release_msisdn(m).await {
                    tracing::warn!(error = %re, "inventory.rollback.msisdn.failed");
                }
            }
            return Err(format!("reserve_esim failed: {e}"));
        }
    };

    let msisdn = msisdn_result
        .get("msisdn")
        .and_then(Value::as_str)
        .ok_or("reserve_next_msisdn returned no msisdn")?
        .to_string();
    let iccid = esim_result
        .get("iccid")
        .and_then(Value::as_str)
        .ok_or("reserve_esim returned no iccid")?
        .to_string();
    let imsi = esim_result
        .get("imsi")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let activation_code = esim_result
        .get("activationCode")
        .or_else(|| esim_result.get("activation_code"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();

    // 8. Store resources + pending tasks in the CFS characteristics.
    let mut pending = serde_json::Map::new();
    for t in TASK_TYPES {
        pending.insert(t.to_string(), json!("pending"));
    }
    let mut characteristics = json!({
        "msisdn": msisdn,
        "iccid": iccid,
        "imsi": imsi,
        "activationCode": activation_code,
        "pendingTasks": Value::Object(pending),
        "commercialOrderId": req.commercial_order_id,
        "customerId": req.customer_id,
        "offeringId": req.offering_id,
        "paymentMethodId": req.payment_method_id,
    });
    if let Some(snap) = &req.price_snapshot {
        if let Some(obj) = characteristics.as_object_mut() {
            obj.insert("priceSnapshot".into(), snap.clone());
        }
    }
    repo::set_service_characteristics(conn, &cfs_id, &characteristics)
        .await
        .map_err(db)?;

    // 9. CFS designed → reserved.
    check_service_transition("designed", "reserved").map_err(policy)?;
    repo::set_service_state(conn, &cfs_id, "reserved")
        .await
        .map_err(db)?;
    repo::add_state_history(
        conn,
        &cfs_id,
        Some("designed"),
        "reserved",
        &ctx.actor,
        "inventory reserved",
        tenant,
    )
    .await
    .map_err(db)?;

    // 10. SO acknowledged → in_progress.
    check_service_order_transition("acknowledged", "in_progress").map_err(policy)?;
    repo::set_service_order_state(conn, &so_id, "in_progress", Some(bss_clock::now()), None)
        .await
        .map_err(db)?;

    stage(
        conn,
        ctx,
        "service_order.in_progress",
        "ServiceOrder",
        &so_id,
        json!({ "serviceOrderId": so_id, "commercialOrderId": req.commercial_order_id }),
    )
    .await
    .map_err(db)?;

    // 11. Four provisioning.task.created events.
    for task_type in TASK_TYPES {
        let payload = json!({
            "serviceId": cfs_id,
            "serviceOrderId": so_id,
            "commercialOrderId": req.commercial_order_id,
            "taskType": task_type,
            "payload": {
                "msisdn": msisdn,
                "iccid": iccid,
                "imsi": imsi,
                "activationCode": activation_code,
                "customerId": req.customer_id,
                "offeringId": req.offering_id,
            },
        });
        stage(
            conn,
            ctx,
            "provisioning.task.created",
            "ProvisioningTask",
            &format!("{cfs_id}:{task_type}"),
            payload,
        )
        .await
        .map_err(db)?;
    }

    tracing::info!(service_order_id = %so_id, cfs_id = %cfs_id, "decomposition.completed");
    Ok(())
}

fn db(e: sqlx::Error) -> String {
    format!("db error: {e}")
}

fn policy(p: bss_db::PolicyViolation) -> String {
    format!("policy {}: {}", p.rule, p.message)
}
