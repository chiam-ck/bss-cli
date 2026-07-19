"""Walk INDEXED_PATHS, chunk on headings, upsert into knowledge.doc_chunk.

Three idempotency layers (cheap → expensive):

1. **mtime cache**: skip whole files whose `source_mtime` matches the
   prior run AND whose content_hash matches. Free.
2. **content_hash dedup**: hash each chunk's content; skip rows where
   hash unchanged.
3. **deterministic id**: sha256(source_path|anchor) so a re-anchored
   section (same anchor, different content) updates in place rather
   than landing a duplicate row.

Deletion: any row whose source_path is in INDEXED_PATHS but whose
(source_path, anchor) pair is NOT in the freshly chunked set is
deleted. This catches removed sections and removed files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bss_knowledge.chunker import chunk_markdown
from bss_knowledge.paths import INDEXED_PATHS, KIND_FOR_PATH

log = structlog.get_logger(__name__)


@dataclass
class ReindexReport:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_unchanged: int = 0
    files_seen: int = 0

    def total(self) -> int:
        return self.added + self.updated + self.deleted + self.skipped_unchanged


def _chunk_id(source_path: str, anchor: str) -> str:
    return hashlib.sha256(f"{source_path}|{anchor}".encode("utf-8")).hexdigest()[:32]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Indexer:
    """Operator-initiated indexer. Run via `bss admin knowledge reindex`
    or `make knowledge-reindex`."""

    def __init__(self, session: AsyncSession, repo_root: Path):
        self._session = session
        self._repo_root = repo_root.resolve()

    async def reindex(self, *, force: bool = False) -> ReindexReport:
        """Walk allowlist, chunk each file, upsert into doc_chunk.

        `force=True` re-hashes + re-upserts every chunk regardless of
        mtime/hash match. Use after schema changes or after a doctrine
        change that affects ranking weights.
        """
        report = ReindexReport()

        # Load existing rows keyed by (source_path, anchor) so we can
        # diff seen vs. existing in a single pass. Cheap: corpus size
        # is sub-200 chunks at v0.20 baseline.
        existing = await self._load_existing()
        seen_keys: set[tuple[str, str]] = set()

        for rel_path in INDEXED_PATHS:
            abs_path = self._repo_root / rel_path
            if not abs_path.exists():
                log.warning("knowledge.indexer.path_missing", path=rel_path)
                continue
            report.files_seen += 1

            stat = abs_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            text_content = abs_path.read_text(encoding="utf-8")
            chunks = chunk_markdown(rel_path, text_content)

            for chunk in chunks:
                key = (chunk.source_path, chunk.anchor)
                seen_keys.add(key)
                chash = _content_hash(chunk.content)
                prior = existing.get(key)
                if (
                    not force
                    and prior is not None
                    and prior["content_hash"] == chash
                    and prior["source_mtime"] == mtime
                ):
                    report.skipped_unchanged += 1
                    continue

                cid = _chunk_id(chunk.source_path, chunk.anchor)
                kind = KIND_FOR_PATH[chunk.source_path]

                # Upsert via ON CONFLICT (id) DO UPDATE. Embedding
                # column intentionally not touched here — Tier-1
                # embedder pass owns it. Re-anchored sections lose
                # their stale embedding via setting it back to NULL
                # (force) or keeping it (incremental — embedder
                # re-fills on next pass since content_hash changed).
                stmt = text(
                    """
                    INSERT INTO knowledge.doc_chunk
                        (id, source_path, anchor, heading_path, kind,
                         content, content_hash, source_mtime, indexed_at)
                    VALUES
                        (:id, :source_path, :anchor, :heading_path, :kind,
                         :content, :content_hash, :source_mtime, now())
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
                    """
                )
                await self._session.execute(
                    stmt,
                    {
                        "id": cid,
                        "source_path": chunk.source_path,
                        "anchor": chunk.anchor,
                        "heading_path": chunk.heading_path,
                        "kind": kind,
                        "content": chunk.content,
                        "content_hash": chash,
                        "source_mtime": mtime,
                    },
                )
                if prior is None:
                    report.added += 1
                else:
                    report.updated += 1

        # Delete rows whose key wasn't seen this run (file removed,
        # section removed, or section re-anchored — which we treat
        # as delete-and-add even though the same row would have a
        # different deterministic id).
        stale_keys = set(existing.keys()) - seen_keys
        for source_path, anchor in stale_keys:
            await self._session.execute(
                text(
                    "DELETE FROM knowledge.doc_chunk "
                    "WHERE source_path = :sp AND anchor = :a"
                ),
                {"sp": source_path, "a": anchor},
            )
            report.deleted += 1

        await self._session.commit()
        log.info(
            "knowledge.indexer.reindex.complete",
            added=report.added,
            updated=report.updated,
            deleted=report.deleted,
            skipped_unchanged=report.skipped_unchanged,
            files_seen=report.files_seen,
        )
        return report

    async def _load_existing(self) -> dict[tuple[str, str], dict]:
        rows = await self._session.execute(
            text(
                "SELECT source_path, anchor, content_hash, source_mtime "
                "FROM knowledge.doc_chunk"
            )
        )
        return {
            (r.source_path, r.anchor): {
                "content_hash": r.content_hash,
                "source_mtime": r.source_mtime,
            }
            for r in rows
        }
