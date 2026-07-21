//! `bss admin seed-demo` — synced demo dataset, BSS + loyalty-cli in lockstep.
//!
//! Rust port of the retired Python `bss_seed.demo` (`seed` + `reset`). A SEPARATE
//! seed from `bss admin seed` (reference data only). On top of reference data it
//! adds, idempotently:
//!
//!   - 3 demo customers (Alice / Bob / Carol Demo) in CRM. The stable across-runs
//!     identifier is the `*.demo@bss-cli.local` email. CRM's eager-sync mirrors
//!     them into loyalty automatically when loyalty is configured.
//!   - 2 demo promotions: `PROMO_DEMO_WELCOME` (public typed code `DEMO_WELCOME10`,
//!     10% off, multi 3 periods) and `PROMO_DEMO_VIP` (targeted, 20% off single).
//!     Created via the catalog HTTP API — the same saga `bss promo create` runs, so
//!     loyalty's OfferDefinition + promo_code register fire in lockstep. If loyalty
//!     isn't configured the catalog refuses with `loyalty_not_configured` and the
//!     promo lane is skipped (BSS-only mode).
//!   - Alice + Bob assigned to the targeted VIP (mints the loyalty offer upfront).
//!
//! `--reset` is the surgical reverse (demo-prefix only, never operator data):
//! unassign the VIP (loyalty revoke via the API), then raw-delete the demo
//! promotions and demo customers keyed on `PROMO_DEMO_*` / `*.demo@bss-cli.local`.
//!
//! Promotions go through the HTTP clients (so the loyalty saga + CRM eager-sync
//! fire); the existence check and the reset deletes use `BSS_DB_URL` directly, as
//! the Python original did. The loyalty full-DB wipe (`bss_seed.demo loyalty-wipe`)
//! is NOT ported — truncating loyalty-cli's own schema + alembic is that project's
//! concern, not BSS's.

use std::collections::HashMap;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_db::PgPool;
use serde_json::Value;

use crate::runtime::{run_safely_code, Clients};

const DEMO_EMAIL_DOMAIN: &str = "demo@bss-cli.local";
const PROMO_WELCOME: &str = "PROMO_DEMO_WELCOME";
const PROMO_VIP: &str = "PROMO_DEMO_VIP";

/// (display name, stable email — the across-runs identifier).
fn demo_customers() -> [(&'static str, String); 3] {
    [
        ("Alice Demo", format!("alice.{DEMO_EMAIL_DOMAIN}")),
        ("Bob Demo", format!("bob.{DEMO_EMAIL_DOMAIN}")),
        ("Carol Demo", format!("carol.{DEMO_EMAIL_DOMAIN}")),
    ]
}

/// The two customers assigned to the targeted VIP promo.
fn vip_assigned_emails() -> [String; 2] {
    [
        format!("alice.{DEMO_EMAIL_DOMAIN}"),
        format!("bob.{DEMO_EMAIL_DOMAIN}"),
    ]
}

/// Funnel a DB error through the shared `ClientError` handling in `run_safely_code`.
fn db_err(e: sqlx::Error) -> ClientError {
    ClientError::Transport(format!("db: {e}"))
}

/// String elements of `value[key]` (an array), skipping non-strings.
fn string_list(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

pub async fn run(reset: bool) -> ExitCode {
    run_safely_code(move |c| async move {
        let db_url = match std::env::var("BSS_DB_URL") {
            Ok(v) if !v.is_empty() => v
                .replace("postgresql+asyncpg://", "postgres://")
                .replace("postgresql://", "postgres://"),
            _ => {
                eprintln!("seed-demo: BSS_DB_URL is not set");
                return Ok(ExitCode::from(1));
            }
        };
        let pool = match bss_db::connect(&db_url).await {
            Ok(p) => p,
            Err(e) => {
                eprintln!("seed-demo: DB connect failed: {e}");
                return Ok(ExitCode::from(1));
            }
        };
        if reset {
            do_reset(&c, &pool).await
        } else {
            do_seed(&c, &pool).await
        }
    })
    .await
}

/// The stable identifier across runs is the email; returns the `CUST-…` id if the
/// customer already exists.
async fn customer_id_by_email(pool: &PgPool, email: &str) -> Result<Option<String>, ClientError> {
    sqlx::query_scalar(
        "SELECT c.id FROM crm.customer c \
         JOIN crm.contact_medium cm ON cm.party_id = c.party_id \
         WHERE cm.medium_type = 'email' AND cm.value = $1 LIMIT 1",
    )
    .bind(email)
    .fetch_optional(pool)
    .await
    .map_err(db_err)
}

/// Whether the promo lane could proceed (loyalty configured) or not.
enum PromoLane {
    Ok,
    LoyaltyDisabled,
}

/// Idempotently ensure one promotion exists (get → create). A
/// `loyalty_not_configured` refusal from the catalog means BSS-only mode.
#[allow(clippy::too_many_arguments)]
async fn ensure_promo(
    c: &Arc<Clients>,
    id: &str,
    discount_value: &str,
    duration_kind: &str,
    audience: &str,
    code: Option<&str>,
    promo_code_kind: Option<&str>,
    periods_total: Option<i64>,
    display_name: &str,
) -> Result<PromoLane, ClientError> {
    match c.catalog.get_promotion(id).await {
        Ok(_) => {
            println!("  · promotion {id} already exists");
            return Ok(PromoLane::Ok);
        }
        Err(ClientError::NotFound(_)) => {}
        Err(e) => return Err(e),
    }
    match c
        .catalog
        .create_promotion(
            id,
            "percent",
            discount_value,
            duration_kind,
            audience,
            "SGD",
            code,
            promo_code_kind,
            None,
            periods_total,
            None,
            None,
            Some(display_name),
        )
        .await
    {
        Ok(_) => {
            println!("  + promotion {id} ({audience})");
            Ok(PromoLane::Ok)
        }
        Err(ClientError::Policy(p)) if p.rule == "catalog.promotion.loyalty_not_configured" => {
            Ok(PromoLane::LoyaltyDisabled)
        }
        Err(e) => Err(e),
    }
}

async fn do_seed(c: &Arc<Clients>, pool: &PgPool) -> Result<ExitCode, ClientError> {
    println!("── demo seed (BSS + loyalty in sync) ──");
    let mut email_to_id: HashMap<String, String> = HashMap::new();

    // 1) customers (stable identifier = email).
    for (name, email) in demo_customers() {
        if let Some(cid) = customer_id_by_email(pool, &email).await? {
            println!("  · customer {name} <{email}> already exists → {cid}");
            email_to_id.insert(email, cid);
        } else {
            let created = c.crm.create_customer(name, Some(&email), None).await?;
            let cid = created
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            println!("  + customer {name} <{email}> → {cid}");
            email_to_id.insert(email, cid);
        }
    }

    // 2) promotions (BSS + loyalty saga via the catalog API).
    match ensure_promo(
        c,
        PROMO_WELCOME,
        "10",
        "multi",
        "public",
        Some("DEMO_WELCOME10"),
        Some("multi_use"),
        Some(3),
        "Demo Welcome 10%",
    )
    .await?
    {
        PromoLane::LoyaltyDisabled => {
            println!("  · promos skipped (catalog → loyalty_not_configured; BSS-only mode)");
        }
        PromoLane::Ok => {
            ensure_promo(
                c,
                PROMO_VIP,
                "20",
                "single",
                "targeted",
                None,
                None,
                None,
                "Demo VIP 20%",
            )
            .await?;

            // 3) targeted assign — mints the loyalty offer upfront.
            let assign_ids: Vec<String> = vip_assigned_emails()
                .iter()
                .filter_map(|e| email_to_id.get(e).cloned())
                .collect();
            if !assign_ids.is_empty() {
                match c.catalog.assign_promotion(PROMO_VIP, &assign_ids).await {
                    Ok(res) => {
                        for cid in string_list(&res, "eligible") {
                            println!("  + assigned {cid} → {PROMO_VIP} (loyalty offer minted)");
                        }
                        for cid in string_list(&res, "already") {
                            println!("  · {cid} already eligible for {PROMO_VIP}");
                        }
                    }
                    // Targeted promo wasn't created (earlier loyalty refusal) — skip.
                    Err(ClientError::Policy(p)) if p.rule == "catalog.promotion.not_targeted" => {}
                    Err(e) => return Err(e),
                }
            }
        }
    }

    println!("demo seed complete.");
    Ok(ExitCode::SUCCESS)
}

async fn do_reset(c: &Arc<Clients>, pool: &PgPool) -> Result<ExitCode, ClientError> {
    println!("── demo reset (BSS + loyalty, demo-prefix only) ──");

    // Resolve demo customer ids by email (the stable identifier).
    let mut demo_ids: Vec<String> = Vec::new();
    for (_, email) in demo_customers() {
        if let Some(cid) = customer_id_by_email(pool, &email).await? {
            demo_ids.push(cid);
        }
    }

    // 1) unassign the targeted VIP via the API so loyalty's offer.revoke fires.
    if !demo_ids.is_empty() {
        match c.catalog.unassign_promotion(PROMO_VIP, &demo_ids).await {
            Ok(res) => {
                for cid in string_list(&res, "removed") {
                    println!("  - unassigned {cid} from {PROMO_VIP} (loyalty cleared)");
                }
            }
            Err(ClientError::NotFound(_)) | Err(ClientError::Policy(_)) => {
                println!("  · unassign skipped (promo absent or not targeted)");
            }
            Err(e) => return Err(e),
        }
    }

    // 2) drop demo promotions in BSS (catalog has no delete verb; the demo-prefix
    //    raw delete is surgical and safe).
    for pid in [PROMO_WELCOME, PROMO_VIP] {
        sqlx::query("DELETE FROM catalog.promotion_eligibility WHERE promotion_id = $1")
            .bind(pid)
            .execute(pool)
            .await
            .map_err(db_err)?;
        let r = sqlx::query("DELETE FROM catalog.promotion WHERE id = $1")
            .bind(pid)
            .execute(pool)
            .await
            .map_err(db_err)?;
        if r.rows_affected() == 1 {
            println!("  - promotion {pid} deleted (BSS)");
        }
    }

    // 3) drop demo customers in BSS. Real customers are soft-archived; these are
    //    clearly-tagged demo rows, so a surgical raw delete is OK. (The loyalty-side
    //    customer rows are left as-is — the unassign above already revoked their
    //    offers, and stale demo customers in loyalty are harmless + idempotent.)
    for cid in &demo_ids {
        let party: Option<String> =
            sqlx::query_scalar("SELECT party_id FROM crm.customer WHERE id = $1")
                .bind(cid)
                .fetch_optional(pool)
                .await
                .map_err(db_err)?;
        let Some(party) = party else { continue };
        sqlx::query("DELETE FROM crm.interaction WHERE customer_id = $1")
            .bind(cid)
            .execute(pool)
            .await
            .map_err(db_err)?;
        sqlx::query("DELETE FROM crm.customer WHERE id = $1")
            .bind(cid)
            .execute(pool)
            .await
            .map_err(db_err)?;
        sqlx::query("DELETE FROM crm.contact_medium WHERE party_id = $1")
            .bind(&party)
            .execute(pool)
            .await
            .map_err(db_err)?;
        sqlx::query("DELETE FROM crm.individual WHERE party_id = $1")
            .bind(&party)
            .execute(pool)
            .await
            .map_err(db_err)?;
        sqlx::query("DELETE FROM crm.party WHERE id = $1")
            .bind(&party)
            .execute(pool)
            .await
            .map_err(db_err)?;
        println!("  - customer {cid} deleted (BSS)");
    }

    println!("demo reset complete.");
    Ok(ExitCode::SUCCESS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn string_list_reads_array_of_strings_and_tolerates_junk() {
        let v = json!({"eligible": ["CUST-1", "CUST-2"], "already": [], "n": 3});
        assert_eq!(string_list(&v, "eligible"), vec!["CUST-1", "CUST-2"]);
        assert!(string_list(&v, "already").is_empty());
        assert!(string_list(&v, "missing").is_empty()); // absent key → empty
        assert!(string_list(&v, "n").is_empty()); // non-array → empty
    }

    #[test]
    fn vip_assignees_are_a_subset_of_seeded_customers() {
        // A typo'd VIP email would silently skip the assignment (email→id miss),
        // so pin that every assignee is actually one of the seeded customers.
        let seeded: Vec<String> = demo_customers().iter().map(|(_, e)| e.clone()).collect();
        for email in vip_assigned_emails() {
            assert!(
                seeded.contains(&email),
                "VIP assignee {email} is not a seeded demo customer"
            );
        }
    }
}
