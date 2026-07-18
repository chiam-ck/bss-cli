//! `bss admin seed` — populate reference data across every domain. Port of the
//! `bss-seed` package (`packages/bss-seed/bss_seed/`).
//!
//! Idempotent (`INSERT … ON CONFLICT DO NOTHING`), so it's safe to re-run — the clean
//! reseed for a disposable demo environment. Writes directly to the shared Postgres
//! schemas via `BSS_DB_URL` (bypassing the services, exactly as the Python seed does);
//! the schema is language-agnostic, so this seeds the Rust stack identically.
//!
//! Seeds: catalog (1 spec, 3 plans + prices + allowances, 4 VAS, 3 service specs, 3
//! mappings), inventory (1000 MSISDNs + 1000 eSIM profiles), CRM (5 agents, 12 SLA
//! policies), provisioning (6 fault-injection rules, all disabled).

use std::process::ExitCode;

use bss_db::PgPool;
use uuid::Uuid;

/// The SM-DP+ host baked into every seeded eSIM activation code.
const SMDP_SERVER: &str = "smdp.bss-cli.local";

pub async fn run() -> ExitCode {
    let db_url = match std::env::var("BSS_DB_URL") {
        Ok(v) if !v.is_empty() => v,
        _ => {
            eprintln!("ERROR: BSS_DB_URL environment variable is not set");
            return ExitCode::from(1);
        }
    };
    let pool = match bss_db::connect(&db_url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("seed: connect failed: {e}");
            return ExitCode::from(1);
        }
    };
    match seed(&pool).await {
        Ok(()) => {
            println!("Seed complete.");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("seed failed: {e}");
            ExitCode::from(1)
        }
    }
}

/// Run every domain seed in one transaction (all-or-nothing, matching the Python
/// `session.begin()` block).
async fn seed(pool: &PgPool) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::raw_sql(CATALOG_SQL).execute(&mut *tx).await?;
    seed_inventory(&mut tx).await?;
    sqlx::raw_sql(CRM_SQL).execute(&mut *tx).await?;
    sqlx::raw_sql(PROVISIONING_SQL).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}

/// 1000 MSISDNs (9000_0000–9000_0999) + 1000 eSIM profiles with generated
/// ICCID/IMSI/ki-ref/activation-code. Batched via `UNNEST` (one round-trip each) rather
/// than the Python's row-at-a-time loop. `ki_ref` is a stubbed HSM slot reference —
/// **never** a real Ki (HSM territory, out of scope).
async fn seed_inventory(tx: &mut sqlx::PgConnection) -> Result<(), sqlx::Error> {
    let msisdns: Vec<String> = (0..1000).map(|i| format!("9000{i:04}")).collect();
    sqlx::query(
        "INSERT INTO inventory.msisdn_pool (msisdn, status) \
         SELECT unnest($1::text[]), 'available' \
         ON CONFLICT (msisdn) DO NOTHING",
    )
    .bind(&msisdns)
    .execute(&mut *tx)
    .await?;

    let mut iccids = Vec::with_capacity(1000);
    let mut imsis = Vec::with_capacity(1000);
    let mut ki_refs = Vec::with_capacity(1000);
    let mut matching_ids = Vec::with_capacity(1000);
    let mut activation_codes = Vec::with_capacity(1000);
    for i in 0..1000u32 {
        let matching_id = format!("{:016x}", rand::random::<u64>());
        iccids.push(format!("8910101{i:012}"));
        imsis.push(format!("525010000{i:06}"));
        ki_refs.push(format!("hsm://ref/{}", Uuid::new_v4()));
        activation_codes.push(format!("LPA:1${SMDP_SERVER}${matching_id}"));
        matching_ids.push(matching_id);
    }
    sqlx::query(
        "INSERT INTO inventory.esim_profile \
         (iccid, imsi, ki_ref, profile_state, smdp_server, matching_id, activation_code) \
         SELECT iccid, imsi, ki_ref, 'available', $6, matching_id, activation_code \
         FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[]) \
              AS t(iccid, imsi, ki_ref, matching_id, activation_code) \
         ON CONFLICT (iccid) DO NOTHING",
    )
    .bind(&iccids)
    .bind(&imsis)
    .bind(&ki_refs)
    .bind(&matching_ids)
    .bind(&activation_codes)
    .bind(SMDP_SERVER)
    .execute(&mut *tx)
    .await?;
    Ok(())
}

/// Catalog reference data — all constant rows, so a single multi-statement script
/// (values inlined; `ON CONFLICT DO NOTHING` keeps it idempotent).
const CATALOG_SQL: &str = r#"
INSERT INTO catalog.product_specification (id, name, description, brand, lifecycle_status)
VALUES ('SPEC_MOBILE_PREPAID', 'Mobile Prepaid Bundle', 'Bundled prepaid mobile plan', 'BSS-CLI', 'active')
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.product_offering
    (id, name, spec_id, is_bundle, is_sellable, lifecycle_status, valid_from, valid_to)
VALUES
    ('PLAN_S', 'Lite', 'SPEC_MOBILE_PREPAID', true, true, 'active', NULL, NULL),
    ('PLAN_M', 'Standard', 'SPEC_MOBILE_PREPAID', true, true, 'active', NULL, NULL),
    ('PLAN_L', 'Max', 'SPEC_MOBILE_PREPAID', true, true, 'active', NULL, NULL)
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.product_offering_price
    (id, offering_id, price_type, recurring_period_length, recurring_period_type, amount, currency, valid_from, valid_to)
VALUES
    ('PRICE_PLAN_S', 'PLAN_S', 'recurring', 1, 'month', 10.00, 'SGD', NULL, NULL),
    ('PRICE_PLAN_M', 'PLAN_M', 'recurring', 1, 'month', 25.00, 'SGD', NULL, NULL),
    ('PRICE_PLAN_L', 'PLAN_L', 'recurring', 1, 'month', 45.00, 'SGD', NULL, NULL)
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.bundle_allowance (id, offering_id, allowance_type, quantity, unit)
VALUES
    ('BA_S_DATA', 'PLAN_S', 'data', 5120, 'mb'),
    ('BA_S_VOICE', 'PLAN_S', 'voice', 100, 'minutes'),
    ('BA_S_SMS', 'PLAN_S', 'sms', 100, 'count'),
    ('BA_S_ROAM', 'PLAN_S', 'data_roaming', 0, 'mb'),
    ('BA_M_DATA', 'PLAN_M', 'data', 30720, 'mb'),
    ('BA_M_VOICE', 'PLAN_M', 'voice', -1, 'minutes'),
    ('BA_M_SMS', 'PLAN_M', 'sms', -1, 'count'),
    ('BA_M_ROAM', 'PLAN_M', 'data_roaming', 500, 'mb'),
    ('BA_L_DATA', 'PLAN_L', 'data', 153600, 'mb'),
    ('BA_L_VOICE', 'PLAN_L', 'voice', -1, 'minutes'),
    ('BA_L_SMS', 'PLAN_L', 'sms', -1, 'count'),
    ('BA_L_ROAM', 'PLAN_L', 'data_roaming', 2048, 'mb')
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.vas_offering
    (id, name, price_amount, currency, allowance_type, allowance_quantity, allowance_unit, expiry_hours)
VALUES
    ('VAS_DATA_1GB', 'Data Top-Up 1GB', 3.00, 'SGD', 'data', 1024, 'mb', NULL),
    ('VAS_DATA_5GB', 'Data Top-Up 5GB', 12.00, 'SGD', 'data', 5120, 'mb', NULL),
    ('VAS_UNLIMITED_DAY', 'Unlimited Data Day Pass', 5.00, 'SGD', 'data', -1, 'mb', 24),
    ('VAS_ROAMING_1GB', 'Roaming Data 1GB', 8.00, 'SGD', 'data_roaming', 1024, 'mb', NULL)
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.service_specification (id, name, type, parameters)
VALUES
    ('SSPEC_CFS_MOBILE_BROADBAND', 'Mobile Broadband CFS', 'CFS', CAST('{}' AS jsonb)),
    ('SSPEC_RFS_DATA_BEARER', 'Data Bearer RFS', 'RFS', CAST('{}' AS jsonb)),
    ('SSPEC_RFS_VOICE_BEARER', 'Voice Bearer RFS', 'RFS', CAST('{}' AS jsonb))
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalog.product_to_service_mapping (offering_id, cfs_spec_id, rfs_spec_ids)
SELECT v.offering_id, 'SSPEC_CFS_MOBILE_BROADBAND',
       ARRAY['SSPEC_RFS_DATA_BEARER', 'SSPEC_RFS_VOICE_BEARER']
FROM (VALUES ('PLAN_S'), ('PLAN_M'), ('PLAN_L')) AS v(offering_id)
WHERE NOT EXISTS (
    SELECT 1 FROM catalog.product_to_service_mapping m WHERE m.offering_id = v.offering_id
);
"#;

/// CRM reference data — 5 agents + 12 SLA policies (4 priorities × 3 ticket types).
const CRM_SQL: &str = r#"
INSERT INTO crm.agent (id, name, email, role, status)
VALUES
    ('AGT-001', 'Alice Tan', 'alice.tan@bss-cli.local', 'csr', 'active'),
    ('AGT-002', 'Bob Lim', 'bob.lim@bss-cli.local', 'csr', 'active'),
    ('AGT-003', 'Carol Ng', 'carol.ng@bss-cli.local', 'supervisor', 'active'),
    ('AGT-004', 'Dave Koh', 'dave.koh@bss-cli.local', 'engineer', 'active'),
    ('AGT-SYS', 'System', 'system@bss-cli.local', 'system', 'active')
ON CONFLICT (id) DO NOTHING;

INSERT INTO crm.sla_policy (id, ticket_type, priority, target_resolution_minutes)
VALUES
    ('SLA_BILLING_DISPUTE_LOW', 'billing_dispute', 'low', 2880),
    ('SLA_BILLING_DISPUTE_NORMAL', 'billing_dispute', 'normal', 1440),
    ('SLA_BILLING_DISPUTE_HIGH', 'billing_dispute', 'high', 480),
    ('SLA_BILLING_DISPUTE_URGENT', 'billing_dispute', 'urgent', 120),
    ('SLA_SERVICE_OUTAGE_LOW', 'service_outage', 'low', 480),
    ('SLA_SERVICE_OUTAGE_NORMAL', 'service_outage', 'normal', 240),
    ('SLA_SERVICE_OUTAGE_HIGH', 'service_outage', 'high', 60),
    ('SLA_SERVICE_OUTAGE_URGENT', 'service_outage', 'urgent', 30),
    ('SLA_CONFIGURATION_LOW', 'configuration', 'low', 2880),
    ('SLA_CONFIGURATION_NORMAL', 'configuration', 'normal', 1440),
    ('SLA_CONFIGURATION_HIGH', 'configuration', 'high', 720),
    ('SLA_CONFIGURATION_URGENT', 'configuration', 'urgent', 240)
ON CONFLICT (id) DO NOTHING;
"#;

/// Provisioning fault-injection rules — all disabled by default; scenarios enable them
/// by id.
const PROVISIONING_SQL: &str = r#"
INSERT INTO provisioning.fault_injection (id, task_type, fault_type, probability, enabled)
VALUES
    ('FI_HLR_PROV_FAIL', 'HLR_PROVISION', 'fail_first_attempt', 0.30, false),
    ('FI_HLR_PROV_STUCK', 'HLR_PROVISION', 'stuck', 0.05, false),
    ('FI_PCRF_SLOW', 'PCRF_POLICY_PUSH', 'slow', 0.20, false),
    ('FI_OCS_FAIL', 'OCS_BALANCE_INIT', 'fail_first_attempt', 0.10, false),
    ('FI_ESIM_FAIL', 'ESIM_PROFILE_PREPARE', 'fail_first_attempt', 0.15, false),
    ('FI_HLR_DEPROV_STUCK', 'HLR_DEPROVISION', 'stuck', 0.05, false)
ON CONFLICT (id) DO NOTHING;
"#;
