"""Tier-0 FTS search over knowledge.doc_chunk.

Uses Postgres `tsvector` + `ts_rank` + `ts_headline`. The query is
parsed with `plainto_tsquery` (English config) for natural-language
input — operators type "rotate cockpit token" not boolean expressions.

Returns a stable shape that the cockpit tool exports verbatim:

  [
    {
      "anchor":      "84-rotate-api-tokens",
      "source_path": "docs/HANDBOOK.md",
      "heading_path": "Part 8 → 8.4 Rotate API tokens",
      "kind":        "handbook",
      "snippet":     "...generate the new token. ...",
    },
    ...
  ]

`get_chunk(anchor, source_path)` returns the full content of one
chunk, used by the LLM to pull the rest of a section after a search
hit's snippet.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bss_knowledge.paths import KIND_RANK_WEIGHTS


@dataclass(frozen=True)
class SearchHit:
    anchor: str
    source_path: str
    heading_path: str
    kind: str
    snippet: str
    content: str
    rank: float

    def to_dict(self) -> dict:
        return {
            "anchor": self.anchor,
            "source_path": self.source_path,
            "heading_path": self.heading_path,
            "kind": self.kind,
            # `snippet` is a short ts_headline excerpt; useful for the
            # operator-side CLI debug view and for ranking display.
            "snippet": self.snippet,
            # `content` is the FULL chunk — the LLM should answer from
            # this, not from `snippet`. v0.20 lesson: small models
            # (Gemma 4 26B and below) don't reliably plan a
            # search→get→answer 2-step. Returning content inline
            # avoids the planning step entirely.
            "content": self.content,
        }


async def search_fts(
    session: AsyncSession,
    *,
    query: str,
    k: int = 3,
    kinds: list[str] | None = None,
) -> list[SearchHit]:
    """Tier-0 FTS search. `kinds` filters by doc kind — useful for the
    LLM to scope to e.g. ["doctrine"] for "is this allowed?" questions.
    """
    if not query.strip():
        return []

    # Build the kinds-filter SQL fragment. We can't bind a list directly
    # into a fragment that interpolates into the WHERE — bind each kind
    # as a parameter and use IN.
    params: dict = {"query": query, "k": k}
    where_kind = ""
    if kinds:
        # Map each kind to a numbered param to keep the query parameterised.
        kind_params = []
        for i, kind in enumerate(kinds):
            pname = f"kind_{i}"
            params[pname] = kind
            kind_params.append(f":{pname}")
        where_kind = f"AND kind IN ({', '.join(kind_params)})"

    stmt = text(
        f"""
        SELECT
            anchor,
            source_path,
            heading_path,
            kind,
            content,
            ts_headline(
                'english',
                content,
                plainto_tsquery('english', :query),
                'StartSel=‹, StopSel=›, MaxWords=120, MinWords=80, '
                || 'ShortWord=3, MaxFragments=3, FragmentDelimiter=" … "'
            ) AS snippet,
            ts_rank(content_tsv, plainto_tsquery('english', :query)) AS rank
        FROM knowledge.doc_chunk
        WHERE content_tsv @@ plainto_tsquery('english', :query)
          {where_kind}
        ORDER BY rank DESC
        LIMIT :k
        """
    )
    rows = await session.execute(stmt, params)

    hits: list[SearchHit] = []
    for r in rows:
        # Apply kind weight (Tier-0 uses it as a soft re-rank too).
        weighted_rank = float(r.rank) * KIND_RANK_WEIGHTS.get(r.kind, 1.0)
        hits.append(
            SearchHit(
                anchor=r.anchor,
                source_path=r.source_path,
                heading_path=r.heading_path,
                kind=r.kind,
                snippet=r.snippet,
                content=r.content,
                rank=weighted_rank,
            )
        )
    # Re-sort by weighted rank (Postgres sorted by raw rank).
    hits.sort(key=lambda h: h.rank, reverse=True)
    return hits


async def get_chunk(
    session: AsyncSession,
    *,
    anchor: str,
    source_path: str,
) -> dict | None:
    """Pull the full content of one chunk. Returns None if not found."""
    row = (
        await session.execute(
            text(
                """
                SELECT anchor, source_path, heading_path, kind,
                       content, indexed_at
                FROM knowledge.doc_chunk
                WHERE anchor = :anchor AND source_path = :source_path
                """
            ),
            {"anchor": anchor, "source_path": source_path},
        )
    ).first()
    if row is None:
        return None
    return {
        "anchor": row.anchor,
        "source_path": row.source_path,
        "heading_path": row.heading_path,
        "kind": row.kind,
        "content": row.content,
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
    }
