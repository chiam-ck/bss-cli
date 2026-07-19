//! Walk `INDEXED_PATHS`, chunk on headings, upsert into `knowledge.doc_chunk`.
//! Port of `packages/bss-knowledge/bss_knowledge/indexer.py`.
//!
//! Three idempotency layers (cheap → expensive):
//! 1. **mtime cache** — skip files whose `source_mtime` + `content_hash` match.
//! 2. **content_hash dedup** — skip rows whose hash is unchanged.
//! 3. **deterministic id** — `sha256(source_path|anchor)[:32]` so a re-anchored
//!    section updates in place rather than landing a duplicate.
//!
//! Deletion: any `(source_path, anchor)` present in the DB but not in the freshly
//! chunked set is removed (catches removed sections + removed files).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use sha2::{Digest, Sha256};
use sqlx::PgPool;

use crate::chunker::chunk_markdown;
use crate::paths::{kind_for, INDEXED_PATHS};

/// Counters for a reindex run.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ReindexReport {
    pub added: u64,
    pub updated: u64,
    pub deleted: u64,
    pub skipped_unchanged: u64,
    pub files_seen: u64,
}

impl ReindexReport {
    pub fn total(&self) -> u64 {
        self.added + self.updated + self.deleted + self.skipped_unchanged
    }
}

fn chunk_id(source_path: &str, anchor: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("{source_path}|{anchor}").as_bytes());
    hex::encode(hasher.finalize())[..32].to_string()
}

fn content_hash(content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    hex::encode(hasher.finalize())
}

struct Existing {
    content_hash: String,
    source_mtime: Option<DateTime<Utc>>,
}

/// Operator-initiated indexer. Run via `bss admin knowledge reindex`.
pub struct Indexer {
    pool: PgPool,
    repo_root: PathBuf,
}

impl Indexer {
    pub fn new(pool: PgPool, repo_root: impl AsRef<Path>) -> Self {
        let repo_root = repo_root
            .as_ref()
            .canonicalize()
            .unwrap_or_else(|_| repo_root.as_ref().to_path_buf());
        Self { pool, repo_root }
    }

    /// Walk the allowlist, chunk each file, upsert into `doc_chunk`. `force`
    /// re-hashes + re-upserts every chunk regardless of the mtime/hash cache.
    pub async fn reindex(&self, force: bool) -> Result<ReindexReport, sqlx::Error> {
        let mut report = ReindexReport::default();
        let existing = self.load_existing().await?;
        let mut seen_keys: HashSet<(String, String)> = HashSet::new();

        let mut tx = self.pool.begin().await?;

        for rel_path in INDEXED_PATHS {
            let abs_path = self.repo_root.join(rel_path);
            if !abs_path.exists() {
                tracing::warn!(path = rel_path, "knowledge.indexer.path_missing");
                continue;
            }
            report.files_seen += 1;

            let meta = std::fs::metadata(&abs_path).map_err(sqlx_io)?;
            let mtime: Option<DateTime<Utc>> = meta.modified().ok().map(DateTime::<Utc>::from);
            let text_content = std::fs::read_to_string(&abs_path).map_err(sqlx_io)?;
            let chunks = chunk_markdown(rel_path, &text_content);

            for chunk in chunks {
                let key = (chunk.source_path.clone(), chunk.anchor.clone());
                seen_keys.insert(key.clone());
                let chash = content_hash(&chunk.content);
                let prior = existing.get(&key);
                if !force {
                    if let Some(p) = prior {
                        if p.content_hash == chash && p.source_mtime == mtime {
                            report.skipped_unchanged += 1;
                            continue;
                        }
                    }
                }

                let cid = chunk_id(&chunk.source_path, &chunk.anchor);
                let kind = kind_for(&chunk.source_path).unwrap_or("runbook");

                // Embedding column intentionally not touched here — the Tier-1
                // embedder pass owns it; reset to NULL only when content changed.
                sqlx::query(
                    r#"
                    INSERT INTO knowledge.doc_chunk
                        (id, source_path, anchor, heading_path, kind,
                         content, content_hash, source_mtime, indexed_at)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (id) DO UPDATE SET
                        heading_path = EXCLUDED.heading_path,
                        kind = EXCLUDED.kind,
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        source_mtime = EXCLUDED.source_mtime,
                        indexed_at = now(),
                        embedding = CASE
                            WHEN knowledge.doc_chunk.content_hash = EXCLUDED.content_hash
                            THEN knowledge.doc_chunk.embedding
                            ELSE NULL
                        END
                    "#,
                )
                .bind(&cid)
                .bind(&chunk.source_path)
                .bind(&chunk.anchor)
                .bind(&chunk.heading_path)
                .bind(kind)
                .bind(&chunk.content)
                .bind(&chash)
                .bind(mtime)
                .execute(&mut *tx)
                .await?;

                if prior.is_none() {
                    report.added += 1;
                } else {
                    report.updated += 1;
                }
            }
        }

        // Delete rows whose key wasn't seen this run (file removed, section
        // removed, or section re-anchored — treated as delete-and-add).
        for (source_path, anchor) in existing.keys() {
            if seen_keys.contains(&(source_path.clone(), anchor.clone())) {
                continue;
            }
            sqlx::query("DELETE FROM knowledge.doc_chunk WHERE source_path = $1 AND anchor = $2")
                .bind(source_path)
                .bind(anchor)
                .execute(&mut *tx)
                .await?;
            report.deleted += 1;
        }

        tx.commit().await?;
        tracing::info!(
            added = report.added,
            updated = report.updated,
            deleted = report.deleted,
            skipped_unchanged = report.skipped_unchanged,
            files_seen = report.files_seen,
            "knowledge.indexer.reindex.complete"
        );
        Ok(report)
    }

    async fn load_existing(&self) -> Result<HashMap<(String, String), Existing>, sqlx::Error> {
        let rows = sqlx::query(
            "SELECT source_path, anchor, content_hash, source_mtime FROM knowledge.doc_chunk",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut out = HashMap::new();
        for r in rows {
            use sqlx::Row;
            let key = (
                r.try_get::<String, _>("source_path")?,
                r.try_get::<String, _>("anchor")?,
            );
            out.insert(
                key,
                Existing {
                    content_hash: r.try_get("content_hash")?,
                    source_mtime: r.try_get("source_mtime")?,
                },
            );
        }
        Ok(out)
    }
}

/// Wrap a filesystem error as a sqlx error so `reindex` has one error type.
fn sqlx_io(e: std::io::Error) -> sqlx::Error {
    sqlx::Error::Io(e)
}
