//! Tier-0 FTS search over `knowledge.doc_chunk`. Port of
//! `packages/bss-knowledge/bss_knowledge/search.py`.
//!
//! Uses Postgres `tsvector` + `ts_rank` + `ts_headline`, parsed with
//! `plainto_tsquery('english', …)` for natural-language input. The SQL is
//! issued verbatim so ranking + snippet generation happen in Postgres exactly
//! as they do for the oracle; the only Rust-side logic is the kind-weight
//! re-rank multiply + stable re-sort.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::{PgPool, Row};

use crate::paths::kind_rank_weight;

/// One FTS hit. The wire shape the cockpit tool exports is [`Self::to_value`]
/// (note: `rank` is internal — the exported dict deliberately omits it).
#[derive(Debug, Clone)]
pub struct SearchHit {
    pub anchor: String,
    pub source_path: String,
    pub heading_path: String,
    pub kind: String,
    pub snippet: String,
    pub content: String,
    pub rank: f64,
}

impl SearchHit {
    /// The stable dict shape the `knowledge.search` tool returns verbatim.
    /// `content` is the FULL chunk (the LLM answers from this, not `snippet`) —
    /// the v0.20 lesson that small models don't reliably plan a search→get→answer
    /// 2-step, so inline content avoids the planning step.
    pub fn to_value(&self) -> Value {
        json!({
            "anchor": self.anchor,
            "source_path": self.source_path,
            "heading_path": self.heading_path,
            "kind": self.kind,
            "snippet": self.snippet,
            "content": self.content,
        })
    }
}

/// Tier-0 FTS search. `kinds` filters by doc kind — useful for the LLM to scope
/// to e.g. `["doctrine"]` for "is this allowed?" questions. Empty/whitespace
/// query returns no hits.
pub async fn search_fts(
    pool: &PgPool,
    query: &str,
    k: i64,
    kinds: Option<&[String]>,
) -> Result<Vec<SearchHit>, sqlx::Error> {
    if query.trim().is_empty() {
        return Ok(Vec::new());
    }

    // $1 = query (reused in ts_headline / ts_rank / WHERE), $2 = k (LIMIT),
    // $3.. = each kind. Reusing $1 is valid in Postgres.
    let mut where_kind = String::new();
    if let Some(ks) = kinds {
        if !ks.is_empty() {
            let placeholders: Vec<String> = (0..ks.len()).map(|i| format!("${}", i + 3)).collect();
            where_kind = format!("AND kind IN ({})", placeholders.join(", "));
        }
    }

    let sql = format!(
        r#"
        SELECT
            anchor,
            source_path,
            heading_path,
            kind,
            content,
            ts_headline(
                'english',
                content,
                plainto_tsquery('english', $1),
                'StartSel=‹, StopSel=›, MaxWords=120, MinWords=80, '
                || 'ShortWord=3, MaxFragments=3, FragmentDelimiter=" … "'
            ) AS snippet,
            ts_rank(content_tsv, plainto_tsquery('english', $1)) AS rank
        FROM knowledge.doc_chunk
        WHERE content_tsv @@ plainto_tsquery('english', $1)
          {where_kind}
        ORDER BY rank DESC
        LIMIT $2
        "#
    );

    let mut q = sqlx::query(&sql).bind(query).bind(k);
    if let Some(ks) = kinds {
        for kind in ks {
            q = q.bind(kind);
        }
    }
    let rows = q.fetch_all(pool).await?;

    let mut hits: Vec<SearchHit> = Vec::with_capacity(rows.len());
    for r in rows {
        let kind: String = r.try_get("kind")?;
        // `ts_rank` returns REAL (float4); widening to f64 is exact, matching
        // asyncpg decoding float4 → Python float before the weight multiply.
        let raw_rank: f32 = r.try_get("rank")?;
        let weighted = raw_rank as f64 * kind_rank_weight(&kind);
        hits.push(SearchHit {
            anchor: r.try_get("anchor")?,
            source_path: r.try_get("source_path")?,
            heading_path: r.try_get("heading_path")?,
            kind,
            snippet: r.try_get("snippet")?,
            content: r.try_get("content")?,
            rank: weighted,
        });
    }
    // Re-sort by weighted rank (Postgres sorted by raw rank). Stable sort keeps
    // the DB order among equal weighted ranks — mirrors Python's stable sort.
    hits.sort_by(|a, b| {
        b.rank
            .partial_cmp(&a.rank)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(hits)
}

/// Pull the full content of one chunk. Returns `None` if not found. `indexed_at`
/// renders via [`bss_clock::isoformat`] to match Python `datetime.isoformat()`.
pub async fn get_chunk(
    pool: &PgPool,
    anchor: &str,
    source_path: &str,
) -> Result<Option<Value>, sqlx::Error> {
    let row = sqlx::query(
        r#"
        SELECT anchor, source_path, heading_path, kind, content, indexed_at
        FROM knowledge.doc_chunk
        WHERE anchor = $1 AND source_path = $2
        "#,
    )
    .bind(anchor)
    .bind(source_path)
    .fetch_optional(pool)
    .await?;

    let Some(r) = row else { return Ok(None) };
    let indexed_at: Option<DateTime<Utc>> = r.try_get("indexed_at")?;
    Ok(Some(json!({
        "anchor": r.try_get::<String, _>("anchor")?,
        "source_path": r.try_get::<String, _>("source_path")?,
        "heading_path": r.try_get::<String, _>("heading_path")?,
        "kind": r.try_get::<String, _>("kind")?,
        "content": r.try_get::<String, _>("content")?,
        "indexed_at": indexed_at.map(bss_clock::isoformat),
    })))
}
