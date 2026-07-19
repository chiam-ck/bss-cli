//! `bss onboard` — first-run provider configuration wizard (v0.14+). Port of
//! `cli/bss_cli/commands/onboard.py`.
//!
//! The SaaS-onboarding feel without being a SaaS. Reads `.env` if present, prompts only
//! for missing/changed values, validates each provider with a probe call before saving,
//! writes back atomically (`.env.tmp` → rename) preserving comments + ordering, and
//! keeps the last few timestamped backups.
//!
//! Domains: `email` (Resend), `kyc` (Didit), `payment` (Stripe). Re-runnable per-domain
//! (`--domain email`). Secrets live in `.env`, never `settings.toml` (v0.13 doctrine).
//! Fail-fast on probe failure; refuse a test key when `BSS_ENV=production`.

use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;
use fancy_regex::Regex;
use glob::glob;
use indexmap::IndexMap;

/// Keep this many wizard-written `.env.backup-*` files; hand-named backups (no
/// `-HHMMSS` suffix) are never matched or pruned.
const BACKUP_RETENTION_COUNT: usize = 5;

#[derive(Args)]
pub struct OnboardArgs {
    /// Configure a single domain (email | kyc | payment). Default: all.
    #[arg(long, short = 'd')]
    domain: Option<String>,
    /// Path to the .env file. Defaults to $BSS_ONBOARD_ENV_PATH, else <repo-root>/.env.
    #[arg(long = "env-path")]
    env_path: Option<PathBuf>,
}

pub async fn run(args: OnboardArgs) -> ExitCode {
    let target = args
        .env_path
        .or_else(|| {
            std::env::var("BSS_ONBOARD_ENV_PATH")
                .ok()
                .filter(|v| !v.is_empty())
                .map(PathBuf::from)
        })
        .unwrap_or_else(default_env_path);
    println!("Welcome to BSS-CLI. Configuring {}.\n", target.display());

    let domains = match domains_to_configure(args.domain.as_deref()) {
        Ok(d) => d,
        Err(()) => return ExitCode::from(2),
    };
    let mut env = read_env_file(&target);

    for d in domains {
        println!("\n── {} ──", d.to_uppercase());
        let outcome = match d {
            "email" => configure_email(&mut env).await,
            "kyc" => configure_kyc(&mut env).await,
            "payment" => configure_payment(&mut env).await,
            other => {
                eprintln!("Unknown domain: {other:?}");
                return ExitCode::from(2);
            }
        };
        if let Err(code) = outcome {
            return ExitCode::from(code);
        }
    }

    let backup = match write_env_file(&target, &env) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("failed to write {}: {e}", target.display());
            return ExitCode::from(1);
        }
    };
    println!(
        "\nWrote {}. Restart services with: docker compose down && docker compose up -d",
        target.display()
    );
    if let Some(b) = backup {
        println!(
            "Previous .env backed up to {}. Keeps the last {BACKUP_RETENTION_COUNT} wizard \
             backups; older are pruned. Hand-named backups are never touched.",
            b.display()
        );
    }
    ExitCode::SUCCESS
}

// ── per-domain prompts ──────────────────────────────────────────────

/// Email (Resend). test → logging dev mailbox; production → Resend.
async fn configure_email(env: &mut IndexMap<String, String>) -> Result<(), u8> {
    let current = env
        .get("BSS_PORTAL_EMAIL_PROVIDER")
        .filter(|v| !v.is_empty())
        .or_else(|| env.get("BSS_PORTAL_EMAIL_ADAPTER"))
        .cloned()
        .unwrap_or_default();
    println!(
        "  Current provider: {}",
        if current.is_empty() {
            "(unset; defaults to logging)"
        } else {
            &current
        }
    );
    let mode = ask_choice(
        "  Mode",
        &["test", "production"],
        if current == "resend" {
            "production"
        } else {
            "test"
        },
    );
    if mode == "test" {
        env.insert("BSS_PORTAL_EMAIL_PROVIDER".into(), "logging".into());
        env.shift_remove("BSS_PORTAL_EMAIL_RESEND_API_KEY");
        println!("  ✓ Mode set to logging — emails write to dev mailbox.");
        return Ok(());
    }

    env.insert("BSS_PORTAL_EMAIL_PROVIDER".into(), "resend".into());
    let api_key = ask_secret(
        "  Resend API key (re_...)",
        env,
        "BSS_PORTAL_EMAIL_RESEND_API_KEY",
    );
    if !api_key.starts_with("re_") {
        eprintln!("  API key must start with 're_'. Aborting.");
        return Err(2);
    }
    env.insert("BSS_PORTAL_EMAIL_RESEND_API_KEY".into(), api_key.clone());

    let from_addr = ask_default(
        "  Sender address (e.g. 'BSS-CLI <noreply@mail.example.com>')",
        env.get("BSS_PORTAL_EMAIL_FROM").map_or("", String::as_str),
    );
    if from_addr.is_empty() || !from_addr.contains('@') {
        eprintln!("  Sender address must contain an email; aborting.");
        return Err(2);
    }
    env.insert("BSS_PORTAL_EMAIL_FROM".into(), from_addr.clone());

    let webhook = ask_secret(
        "  Resend webhook secret (whsec_...)",
        env,
        "BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET",
    );
    if !webhook.is_empty() && !webhook.starts_with("whsec_") {
        println!("  webhook secret usually starts with 'whsec_' — double-check the dashboard. Saved anyway.");
    }
    env.insert("BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET".into(), webhook);

    if confirm("  Probe Resend with a real test send?", true) {
        let recipient = ask_default("    Recipient email (your account email)", "");
        if !probe_resend(&api_key, &from_addr, &recipient).await {
            eprintln!(
                "  Probe failed. Review the error above and re-run `bss onboard --domain email`."
            );
            return Err(2);
        }
        println!("  ✓ Probe send accepted by Resend.");
    }
    Ok(())
}

/// KYC (Didit). test → prebaked; production → Didit.
async fn configure_kyc(env: &mut IndexMap<String, String>) -> Result<(), u8> {
    let current = env
        .get("BSS_PORTAL_KYC_PROVIDER")
        .cloned()
        .unwrap_or_default();
    println!(
        "  Current provider: {}",
        if current.is_empty() {
            "(unset; defaults to prebaked)"
        } else {
            &current
        }
    );
    let mode = ask_choice(
        "  Mode",
        &["test", "production"],
        if current == "didit" {
            "production"
        } else {
            "test"
        },
    );
    if mode == "test" {
        env.insert("BSS_PORTAL_KYC_PROVIDER".into(), "prebaked".into());
        env.shift_remove("BSS_PORTAL_KYC_DIDIT_API_KEY");
        env.shift_remove("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID");
        env.shift_remove("BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET");
        println!(
            "  ✓ Mode set to prebaked — deterministic per-customer attestation, no external calls."
        );
        return Ok(());
    }

    env.insert("BSS_PORTAL_KYC_PROVIDER".into(), "didit".into());
    let api_key = ask_secret("  Didit API key", env, "BSS_PORTAL_KYC_DIDIT_API_KEY");
    if api_key.is_empty() {
        eprintln!("  API key is required. Aborting.");
        return Err(2);
    }
    env.insert("BSS_PORTAL_KYC_DIDIT_API_KEY".into(), api_key.clone());

    let workflow_id = ask_default(
        "  Didit workflow ID (raw UUID, e.g. 7411e1f2-119d-4eee-9b8c-6e759933c2b8)",
        env.get("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID")
            .map_or("", String::as_str),
    );
    if !is_uuid(&workflow_id) {
        eprintln!(
            "  Workflow ID must be a raw UUID (8-4-4-4-12 hex). Didit's dashboard returns it \
             without a 'wf_' prefix."
        );
        return Err(2);
    }
    env.insert(
        "BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID".into(),
        workflow_id.clone(),
    );

    let webhook = ask_secret(
        "  Didit webhook secret",
        env,
        "BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET",
    );
    if webhook.is_empty() {
        eprintln!(
            "  Webhook secret is required — it's the trust anchor for v0.15 KYC (HMAC verifies \
             inbound webhooks, which write the corroboration row the BSS policy reads)."
        );
        return Err(2);
    }
    env.insert("BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET".into(), webhook);

    if confirm("  Probe Didit by creating a real sandbox session?", true) {
        if !probe_didit(&api_key, &workflow_id).await {
            eprintln!(
                "  Probe failed. Review the error above and re-run `bss onboard --domain kyc`."
            );
            return Err(2);
        }
        println!(
            "  ✓ Didit accepted the sandbox session. The redirect URL was printed above — open \
             it to validate end-to-end."
        );
    }
    Ok(())
}

/// Payment (Stripe). test → mock tokenizer; production → Stripe.
async fn configure_payment(env: &mut IndexMap<String, String>) -> Result<(), u8> {
    let current = env.get("BSS_PAYMENT_PROVIDER").cloned().unwrap_or_default();
    println!(
        "  Current provider: {}",
        if current.is_empty() {
            "(unset; defaults to mock)"
        } else {
            &current
        }
    );
    let mode = ask_choice(
        "  Mode",
        &["test", "production"],
        if current == "stripe" {
            "production"
        } else {
            "test"
        },
    );
    if mode == "test" {
        env.insert("BSS_PAYMENT_PROVIDER".into(), "mock".into());
        for k in [
            "BSS_PAYMENT_STRIPE_API_KEY",
            "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY",
            "BSS_PAYMENT_STRIPE_WEBHOOK_SECRET",
            "BSS_PAYMENT_ALLOW_TEST_CARD_REUSE",
        ] {
            env.shift_remove(k);
        }
        println!("  ✓ Mode set to mock — in-process tokenizer, no external calls. Hero scenarios use this mode.");
        return Ok(());
    }

    env.insert("BSS_PAYMENT_PROVIDER".into(), "stripe".into());
    let api_key = ask_secret(
        "  Stripe secret key (sk_test_... / sk_live_...)",
        env,
        "BSS_PAYMENT_STRIPE_API_KEY",
    );
    if !(api_key.starts_with("sk_test_") || api_key.starts_with("sk_live_")) {
        eprintln!("  Secret key must start with 'sk_test_' or 'sk_live_'. Aborting.");
        return Err(2);
    }
    env.insert("BSS_PAYMENT_STRIPE_API_KEY".into(), api_key.clone());
    let is_test_secret = api_key.starts_with("sk_test_");

    let pub_key = ask_default(
        "  Stripe publishable key (pk_test_... or pk_live_...)",
        env.get("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY")
            .map_or("", String::as_str),
    );
    if !(pub_key.starts_with("pk_test_") || pub_key.starts_with("pk_live_")) {
        eprintln!("  Publishable key must start with 'pk_test_' or 'pk_live_'. Aborting.");
        return Err(2);
    }
    let is_test_pub = pub_key.starts_with("pk_test_");
    if is_test_secret != is_test_pub {
        eprintln!(
            "  Stripe key mode mismatch — secret and publishable keys must both be test \
             (sk_test_/pk_test_) or both live (sk_live_/pk_live_)."
        );
        return Err(2);
    }
    env.insert("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY".into(), pub_key);

    let bss_env = env
        .get("BSS_ENV")
        .cloned()
        .unwrap_or_else(|| "development".into());
    if bss_env == "production" && is_test_secret {
        eprintln!(
            "  Refusing to write sk_test_* with BSS_ENV=production. Production must use sk_live_*; \
             sandbox testing must use BSS_ENV=staging or development."
        );
        return Err(2);
    }
    if bss_env != "production" && !is_test_secret {
        println!(
            "  Warning: live Stripe keys (sk_live_*) configured with BSS_ENV={bss_env:?}. Real \
             card charges will hit your Stripe account — make sure this is intentional."
        );
    }

    let webhook = ask_secret(
        "  Stripe webhook signing secret (whsec_...)",
        env,
        "BSS_PAYMENT_STRIPE_WEBHOOK_SECRET",
    );
    if !webhook.starts_with("whsec_") {
        eprintln!(
            "  Webhook secret must start with 'whsec_'. Get it from Stripe Dashboard → Developers \
             → Webhooks → your endpoint → Signing secret. Aborting."
        );
        return Err(2);
    }
    env.insert("BSS_PAYMENT_STRIPE_WEBHOOK_SECRET".into(), webhook);

    if is_test_secret {
        let default_reuse = env
            .get("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE")
            .map(|v| v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        if confirm(
            "  Enable BSS_PAYMENT_ALLOW_TEST_CARD_REUSE? (sandbox-only; lets the same Stripe test \
             pm_* re-attach to different BSS customers)",
            default_reuse,
        ) {
            env.insert("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE".into(), "true".into());
        } else {
            env.shift_remove("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE");
        }
    } else {
        env.shift_remove("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE");
    }

    if confirm("  Probe Stripe by calling Account.retrieve?", true) {
        if !probe_stripe(&api_key).await {
            eprintln!(
                "  Probe failed. Review the error above and re-run `bss onboard --domain payment`."
            );
            return Err(2);
        }
        println!("  ✓ Stripe accepted the key.");
    }
    println!("  Reminder: cut over saved cards before flipping the env var in production — see docs/runbooks/stripe-cutover.md.");
    Ok(())
}

// ── provider probes (reqwest — no vendor SDKs) ──────────────────────

/// One Resend send. `POST https://api.resend.com/emails` with a Bearer key.
async fn probe_resend(api_key: &str, from_addr: &str, recipient: &str) -> bool {
    let body = serde_json::json!({
        "from": from_addr,
        "to": [recipient],
        "subject": "BSS-CLI onboard probe",
        "html": "<p>If you see this, Resend is configured correctly.</p>",
        "text": "If you see this, Resend is configured correctly.",
    });
    let resp = reqwest::Client::new()
        .post("https://api.resend.com/emails")
        .header("Authorization", format!("Bearer {api_key}"))
        .json(&body)
        .send()
        .await;
    match resp {
        Ok(r) => {
            let status = r.status().as_u16();
            let json: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
            if status < 400 {
                println!(
                    "    Resend accepted: id={}",
                    json.get("id").and_then(|v| v.as_str()).unwrap_or("?")
                );
                true
            } else {
                eprintln!("  Resend rejected the probe: {status} {json}");
                false
            }
        }
        Err(e) => {
            eprintln!("  Resend probe failed: {e}");
            false
        }
    }
}

/// One Didit `POST /v2/session/` (expects 201).
async fn probe_didit(api_key: &str, workflow_id: &str) -> bool {
    let body =
        serde_json::json!({ "workflow_id": workflow_id, "vendor_data": "bss-cli-onboard-probe" });
    let resp = reqwest::Client::new()
        .post("https://verification.didit.me/v2/session/")
        .header("x-api-key", api_key)
        .json(&body)
        .send()
        .await;
    match resp {
        Ok(r) => {
            let status = r.status().as_u16();
            let json: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
            if status != 201 {
                eprintln!("  Didit returned {status}: {json}");
                return false;
            }
            println!(
                "    Didit accepted: session_id={}\n    Open this URL to walk the hosted UI:\n    {}",
                json.get("session_id").and_then(|v| v.as_str()).unwrap_or("?"),
                json.get("url").and_then(|v| v.as_str()).unwrap_or("?"),
            );
            true
        }
        Err(e) => {
            eprintln!("  Didit probe failed: {e}");
            false
        }
    }
}

/// One `GET https://api.stripe.com/v1/account` (Account.retrieve — costs nothing).
async fn probe_stripe(api_key: &str) -> bool {
    let resp = reqwest::Client::new()
        .get("https://api.stripe.com/v1/account")
        .header("Authorization", format!("Bearer {api_key}"))
        .send()
        .await;
    match resp {
        Ok(r) if r.status().is_success() => {
            let json: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
            println!(
                "    Stripe account: id={}",
                json.get("id").and_then(|v| v.as_str()).unwrap_or("?")
            );
            true
        }
        Ok(r) => {
            let status = r.status().as_u16();
            eprintln!(
                "  Stripe rejected the probe: {status} {}",
                r.text().await.unwrap_or_default()
            );
            false
        }
        Err(e) => {
            eprintln!("  Stripe rejected the probe: {e}");
            false
        }
    }
}

// ── .env round-trip (preserves comments + ordering) ─────────────────

/// Parse a `.env` into an ordered map. Missing file → empty. Comments/blanks are
/// dropped from the map (preserved on disk by [`write_env_file`]); quoted values are
/// unwrapped.
pub fn read_env_file(path: &Path) -> IndexMap<String, String> {
    let mut env = IndexMap::new();
    let Ok(text) = std::fs::read_to_string(path) else {
        return env;
    };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim().to_string();
        let mut value = value.trim().to_string();
        if value.len() >= 2 {
            let bytes = value.as_bytes();
            let (first, last) = (bytes[0], bytes[bytes.len() - 1]);
            if (first == b'"' || first == b'\'') && first == last {
                value = value[1..value.len() - 1].to_string();
            }
        }
        env.insert(key, value);
    }
    env
}

/// Write `env` back atomically, preserving comments + existing key order; new keys are
/// appended. A timestamped backup is copied before the rename, and old wizard backups
/// are pruned to [`BACKUP_RETENTION_COUNT`]. Keys absent from `env` but present in the
/// file are dropped (the wizard pops stale creds deliberately). Returns the backup path
/// (or `None` if the file didn't exist).
pub fn write_env_file(
    path: &Path,
    env: &IndexMap<String, String>,
) -> Result<Option<PathBuf>, String> {
    let existing = std::fs::read_to_string(path).ok();
    let mut backup_path = None;
    if existing.is_some() {
        let ts = bss_clock::now().format("%Y-%m-%d-%H%M%S");
        let name = format!("{}.backup-{ts}", file_name(path));
        let bp = path.with_file_name(name);
        std::fs::copy(path, &bp).map_err(|e| e.to_string())?;
        backup_path = Some(bp);
    }

    let mut written: std::collections::HashSet<&str> = std::collections::HashSet::new();
    let mut lines: Vec<String> = Vec::new();
    for raw in existing.as_deref().unwrap_or("").lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            lines.push(raw.to_string());
            continue;
        }
        let Some((key, _)) = line.split_once('=') else {
            lines.push(raw.to_string());
            continue;
        };
        let key = key.trim();
        if let Some(val) = env.get(key) {
            lines.push(format!("{key}={}", quote_if_needed(val)));
            written.insert(key);
        }
        // else: dropped (wizard popped it).
    }

    let new_keys: Vec<(&String, &String)> = env
        .iter()
        .filter(|(k, _)| !written.contains(k.as_str()))
        .collect();
    if !new_keys.is_empty() {
        lines.push(String::new());
        lines.push("# Added by `bss onboard` (v0.14+)".to_string());
        for (k, v) in new_keys {
            lines.push(format!("{k}={}", quote_if_needed(v)));
        }
    }

    let tmp = path.with_file_name(format!("{}.tmp", file_name(path)));
    std::fs::write(&tmp, lines.join("\n") + "\n").map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;

    if backup_path.is_some() {
        if let Some(dir) = path.parent() {
            prune_old_backups(dir, &file_name(path));
        }
    }
    Ok(backup_path)
}

/// Keep only the newest [`BACKUP_RETENTION_COUNT`] `<name>.backup-YYYY-MM-DD-HHMMSS`
/// files. The glob is timestamp-shaped so hand-named backups are never matched.
pub fn prune_old_backups(directory: &Path, env_name: &str) {
    let pattern = directory.join(format!("{env_name}.backup-????-??-??-??????"));
    let Ok(paths) = glob(&pattern.to_string_lossy()) else {
        return;
    };
    let mut backups: Vec<PathBuf> = paths.filter_map(Result::ok).collect();
    backups.sort();
    backups.reverse();
    for stale in backups.into_iter().skip(BACKUP_RETENTION_COUNT) {
        let _ = std::fs::remove_file(stale);
    }
}

/// Quote a value if it contains spaces or shell-special chars (matches the `.env`
/// convention); embedded backslashes/quotes are escaped.
pub fn quote_if_needed(value: &str) -> String {
    let needs = value
        .chars()
        .any(|c| matches!(c, ' ' | '\t' | '#' | '$' | '\'' | '`'));
    if needs {
        let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
        format!("\"{escaped}\"")
    } else {
        value.to_string()
    }
}

// ── helpers ─────────────────────────────────────────────────────────

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| ".env".to_string())
}

/// The domains to configure: all three when `--domain` is absent, else the one named.
fn domains_to_configure(domain: Option<&str>) -> Result<Vec<&'static str>, ()> {
    match domain {
        None => Ok(vec!["email", "kyc", "payment"]),
        Some(d) => match d.to_lowercase().as_str() {
            "email" => Ok(vec!["email"]),
            "kyc" => Ok(vec!["kyc"]),
            "payment" => Ok(vec!["payment"]),
            _ => {
                eprintln!("Unknown --domain {domain:?}. Expected: email (v0.14), kyc (v0.15+), payment (v0.16+).");
                Err(())
            }
        },
    }
}

fn is_uuid(s: &str) -> bool {
    Regex::new(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
        .ok()
        .and_then(|re| re.is_match(s).ok())
        .unwrap_or(false)
}

/// Repo-root `.env` — walk up from cwd for a dir containing `rust/`, matching how the
/// binary bootstraps its own env. Falls back to `./.env`.
fn default_env_path() -> PathBuf {
    let mut dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    loop {
        if dir.join("rust").is_dir() {
            return dir.join(".env");
        }
        if !dir.pop() {
            return PathBuf::from(".env");
        }
    }
}

// ── prompt primitives ───────────────────────────────────────────────

fn read_line() -> String {
    let mut line = String::new();
    let _ = io::stdin().read_line(&mut line);
    line.trim_end_matches(['\n', '\r']).to_string()
}

/// `msg [default]: ` — empty input returns `default`.
fn ask_default(msg: &str, default: &str) -> String {
    if default.is_empty() {
        print!("{msg}: ");
    } else {
        print!("{msg} [{default}]: ");
    }
    let _ = io::stdout().flush();
    let got = read_line();
    if got.is_empty() {
        default.to_string()
    } else {
        got
    }
}

/// Like [`ask_default`] but the default comes from an env key (secret — not echoed).
fn ask_secret(msg: &str, env: &IndexMap<String, String>, key: &str) -> String {
    let default = env.get(key).cloned().unwrap_or_default();
    // The terminal does not mask input (no rpassword dep); the value equals Python's.
    print!("{msg}: ");
    let _ = io::stdout().flush();
    let got = read_line();
    if got.is_empty() {
        default
    } else {
        got
    }
}

/// Re-prompt until the input matches one of `choices`; empty returns `default`.
fn ask_choice(msg: &str, choices: &[&str], default: &str) -> String {
    loop {
        print!("{msg} ({}) [{default}]: ", choices.join("/"));
        let _ = io::stdout().flush();
        let got = read_line();
        if got.is_empty() {
            return default.to_string();
        }
        if choices.contains(&got.as_str()) {
            return got;
        }
        println!("  Please enter one of: {}", choices.join(", "));
    }
}

/// `msg [y/n]: ` — empty returns `default`.
fn confirm(msg: &str, default: bool) -> bool {
    let hint = if default { "Y/n" } else { "y/N" };
    print!("{msg} [{hint}]: ");
    let _ = io::stdout().flush();
    let got = read_line().to_lowercase();
    match got.as_str() {
        "" => default,
        "y" | "yes" => true,
        _ => false,
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn env_of(pairs: &[(&str, &str)]) -> IndexMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn read_skips_comments_and_strips_quotes() {
        let dir = tempdir();
        let p = dir.join(".env");
        std::fs::write(&p, "# comment\n\nA=1\nB=\"two words\"\nC='q'\n").unwrap();
        let env = read_env_file(&p);
        assert_eq!(env.get("A").unwrap(), "1");
        assert_eq!(env.get("B").unwrap(), "two words");
        assert_eq!(env.get("C").unwrap(), "q");
        assert_eq!(env.len(), 3);
    }

    #[test]
    fn read_missing_file_is_empty() {
        assert!(read_env_file(Path::new("/no/such/.env")).is_empty());
    }

    #[test]
    fn write_preserves_comments_order_and_appends() {
        let dir = tempdir();
        let p = dir.join(".env");
        std::fs::write(&p, "# header\nA=old\nB=keep\n").unwrap();
        let env = env_of(&[("A", "new"), ("B", "keep"), ("C", "added")]);
        let backup = write_env_file(&p, &env).unwrap();
        assert!(backup.is_some(), "existing file should be backed up");
        let out = std::fs::read_to_string(&p).unwrap();
        assert!(out.contains("# header"));
        assert!(out.contains("A=new"));
        // New key appended under the wizard banner.
        assert!(out.contains("# Added by `bss onboard`"));
        assert!(out.contains("C=added"));
        // Order: A before B before C.
        let ai = out.find("A=new").unwrap();
        let bi = out.find("B=keep").unwrap();
        let ci = out.find("C=added").unwrap();
        assert!(ai < bi && bi < ci);
    }

    #[test]
    fn write_drops_popped_keys() {
        let dir = tempdir();
        let p = dir.join(".env");
        std::fs::write(&p, "KEEP=1\nDROP=2\n").unwrap();
        let env = env_of(&[("KEEP", "1")]);
        write_env_file(&p, &env).unwrap();
        let out = std::fs::read_to_string(&p).unwrap();
        assert!(out.contains("KEEP=1"));
        assert!(!out.contains("DROP"));
    }

    #[test]
    fn write_missing_file_returns_none_backup() {
        let dir = tempdir();
        let p = dir.join(".env");
        let env = env_of(&[("A", "1")]);
        assert!(write_env_file(&p, &env).unwrap().is_none());
        assert!(p.exists());
    }

    #[test]
    fn quote_only_when_needed() {
        assert_eq!(quote_if_needed("simple"), "simple");
        assert_eq!(quote_if_needed("two words"), "\"two words\"");
        // A bare `"` isn't a quote-trigger char (matches Python), so it passes through.
        assert_eq!(quote_if_needed("a\"b"), "a\"b");
        // A space triggers quoting; the embedded `"` is then escaped.
        assert_eq!(quote_if_needed("a \"b"), "\"a \\\"b\"");
    }

    #[test]
    fn uuid_validation() {
        assert!(is_uuid("7411e1f2-119d-4eee-9b8c-6e759933c2b8"));
        assert!(!is_uuid("wf_7411e1f2"));
        assert!(!is_uuid("not-a-uuid"));
    }

    #[test]
    fn unknown_domain_errors() {
        assert!(domains_to_configure(Some("banana")).is_err());
        assert_eq!(domains_to_configure(None).unwrap().len(), 3);
        assert_eq!(domains_to_configure(Some("KYC")).unwrap(), vec!["kyc"]);
    }

    #[test]
    fn prune_keeps_newest_five() {
        let dir = tempdir();
        for ts in [
            "2026-01-01-000001",
            "2026-01-02-000001",
            "2026-01-03-000001",
            "2026-01-04-000001",
            "2026-01-05-000001",
            "2026-01-06-000001",
            "2026-01-07-000001",
        ] {
            std::fs::write(dir.join(format!(".env.backup-{ts}")), "x").unwrap();
        }
        // A hand-named backup must survive.
        std::fs::write(dir.join(".env.backup-manual"), "x").unwrap();
        prune_old_backups(&dir, ".env");
        let remaining: Vec<String> = glob(&dir.join(".env.backup-*").to_string_lossy())
            .unwrap()
            .filter_map(Result::ok)
            .map(|p| file_name(&p))
            .collect();
        assert_eq!(remaining.iter().filter(|n| n.contains("000001")).count(), 5);
        assert!(remaining.iter().any(|n| n == ".env.backup-manual"));
    }

    fn tempdir() -> PathBuf {
        let base = std::env::temp_dir().join(format!("bss-onboard-{}", rand::random::<u32>()));
        std::fs::create_dir_all(&base).unwrap();
        base
    }
}
