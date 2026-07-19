//! `bss admin knowledge ...` — operator-driven doc-corpus indexer + FTS debug
//! surface (v0.20+). Port of `cli/bss_cli/commands/admin_knowledge.py`.
//!
//! Three subcommands: `reindex` (walk `INDEXED_PATHS`, chunk on headings, upsert
//! into `knowledge.doc_chunk`), `search` (Tier-0 FTS debug — the same shape the
//! cockpit's `knowledge.search` tool returns), and `list` (paginated browse over
//! `doc_chunk`). Reindex is operator-initiated by doctrine — no file-watcher in
//! the cockpit container; the corpus changes with PRs.
//!
//! Talks to Postgres directly via `bss-db`'s pool (like `external-calls`) — the
//! knowledge index has no owning service HTTP surface.

use std::path::PathBuf;
use std::process::ExitCode;

use bss_knowledge::{search_fts, Indexer, INDEXED_PATHS};
use clap::{Args, Subcommand};
use sqlx::Row;

#[derive(Args)]
pub struct KnowledgeArgs {
    #[command(subcommand)]
    command: KnowledgeCommand,
}

#[derive(Subcommand)]
enum KnowledgeCommand {
    /// Walk INDEXED_PATHS, chunk on headings, upsert into knowledge.doc_chunk.
    Reindex {
        /// Re-upsert all chunks regardless of mtime/hash match.
        #[arg(long)]
        force: bool,
    },
    /// Tier-0 FTS search debug surface (same shape as the cockpit's knowledge.search).
    Search {
        /// Natural-language search query.
        query: String,
        /// Top-K hits to return.
        #[arg(long, default_value_t = 3)]
        k: i64,
        /// Filter by doc kind (handbook, doctrine, runbook, architecture, decisions,
        /// tool_surface, roadmap, contributing).
        #[arg(long)]
        kind: Option<String>,
    },
    /// Paginated browse over knowledge.doc_chunk.
    List {
        #[arg(long, default_value_t = 50)]
        limit: i64,
        #[arg(long)]
        kind: Option<String>,
    },
}

pub async fn run(args: KnowledgeArgs) -> ExitCode {
    match args.command {
        KnowledgeCommand::Reindex { force } => reindex(force).await,
        KnowledgeCommand::Search { query, k, kind } => search(&query, k, kind).await,
        KnowledgeCommand::List { limit, kind } => list_chunks(limit, kind).await,
    }
}

/// `BSS_DB_URL` or the operator-facing "source the .env" message + exit 1 (Python
/// raises `RuntimeError`).
fn db_url() -> Result<String, ExitCode> {
    match std::env::var("BSS_DB_URL") {
        Ok(u) if !u.is_empty() => Ok(u),
        _ => {
            eprintln!(
                "BSS_DB_URL is not set. Source the repo .env (`set -a; source .env; set +a`) \
                 or export it explicitly before running this command."
            );
            Err(ExitCode::from(1))
        }
    }
}

/// Walk up from cwd until a `Cargo.lock` — the Rust workspace root (crate dirs don't
/// carry their own lockfile), where the indexed docs (`CLAUDE.md`, `docs/`, …) live.
/// Works from the repo root, a sub-dir, or a packaged install pointing at a checkout.
/// (Pre-2.0-flip this anchored on `pyproject.toml`, which the flip moved to
/// `python-legacy/`.)
fn repo_root() -> Result<PathBuf, ExitCode> {
    let start = match std::env::current_dir() {
        Ok(d) => d,
        Err(_) => {
            eprintln!("Could not locate repo root (no Cargo.lock found)");
            return Err(ExitCode::from(1));
        }
    };
    let mut dir = start;
    loop {
        if dir.join("Cargo.lock").exists() {
            return Ok(dir);
        }
        if !dir.pop() {
            eprintln!("Could not locate repo root (no Cargo.lock found)");
            return Err(ExitCode::from(1));
        }
    }
}

async fn reindex(force: bool) -> ExitCode {
    let url = match db_url() {
        Ok(u) => u,
        Err(code) => return code,
    };
    let root = match repo_root() {
        Ok(r) => r,
        Err(code) => return code,
    };
    let pool = match bss_db::connect(&url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("failed to connect to BSS_DB_URL: {e}");
            return ExitCode::from(1);
        }
    };
    let report = match Indexer::new(pool.clone(), &root).reindex(force).await {
        Ok(r) => r,
        Err(e) => {
            pool.close().await;
            eprintln!("reindex failed: {e}");
            return ExitCode::from(1);
        }
    };
    pool.close().await;
    // `[green]✓[/]` → the glyph; the rest is plain text. Double spaces between fields.
    println!(
        "✓ reindex complete  files={}  added={}  updated={}  deleted={}  skipped={}",
        report.files_seen, report.added, report.updated, report.deleted, report.skipped_unchanged
    );
    ExitCode::SUCCESS
}

async fn search(query: &str, k: i64, kind: Option<String>) -> ExitCode {
    let url = match db_url() {
        Ok(u) => u,
        Err(code) => return code,
    };
    let pool = match bss_db::connect(&url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("failed to connect to BSS_DB_URL: {e}");
            return ExitCode::from(1);
        }
    };
    // Python passes `[kind] if kind else None` — the option is single-valued.
    let kinds = kind.map(|k| vec![k]);
    let hits = match search_fts(&pool, query, k, kinds.as_deref()).await {
        Ok(h) => h,
        Err(e) => {
            pool.close().await;
            eprintln!("search failed: {e}");
            return ExitCode::from(1);
        }
    };
    pool.close().await;

    if hits.is_empty() {
        // `{query!r}` — Python repr, single-quoted.
        println!("No hits for query='{query}'");
        return ExitCode::SUCCESS;
    }

    // Python renders a `rich.Table`; the box-drawing chrome is a documented CLI
    // seam — the per-row cell values match Python.
    println!("knowledge.search — {} hit(s)", hits.len());
    println!("rank   kind  source  anchor  snippet");
    for h in &hits {
        // `ts_headline` wraps matches in ‹…›; Python swaps them for rich bold markup
        // that renders invisibly. In this text seam we drop the markers so the cell
        // shows the same visible words. Then Python's `[:240]` char cap.
        let snippet: String = h
            .snippet
            .replace(['‹', '›'], "")
            .chars()
            .take(240)
            .collect();
        println!(
            "{:.3}  {}  {}  {}  {}",
            h.rank, h.kind, h.source_path, h.anchor, snippet
        );
    }
    ExitCode::SUCCESS
}

async fn list_chunks(limit: i64, kind: Option<String>) -> ExitCode {
    let url = match db_url() {
        Ok(u) => u,
        Err(code) => return code,
    };
    let pool = match bss_db::connect(&url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("failed to connect to BSS_DB_URL: {e}");
            return ExitCode::from(1);
        }
    };

    let where_clause = if kind.is_some() {
        "WHERE kind = $2"
    } else {
        ""
    };
    let sql = format!(
        "SELECT source_path, anchor, kind, heading_path, LEFT(content_hash, 8) AS hash8 \
         FROM knowledge.doc_chunk {where_clause} \
         ORDER BY source_path, anchor LIMIT $1"
    );
    let mut q = sqlx::query(&sql).bind(limit);
    if let Some(kd) = &kind {
        q = q.bind(kd);
    }
    let rows = match q.fetch_all(&pool).await {
        Ok(r) => r,
        Err(e) => {
            pool.close().await;
            eprintln!("list failed: {e}");
            return ExitCode::from(1);
        }
    };
    pool.close().await;

    println!("knowledge.doc_chunk ({} row(s))", rows.len());
    println!("source  anchor  kind  heading_path  hash8");
    for r in &rows {
        let source_path: String = r.get("source_path");
        let anchor: String = r.get("anchor");
        let kind: String = r.get("kind");
        let heading_path: String = r.get("heading_path");
        let hash8: String = r.get("hash8");
        println!("{source_path}  {anchor}  {kind}  {heading_path}  {hash8}");
    }
    println!("allowlist: {} files", INDEXED_PATHS.len());
    ExitCode::SUCCESS
}
