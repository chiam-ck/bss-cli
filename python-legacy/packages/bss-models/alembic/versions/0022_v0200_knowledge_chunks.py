"""v0.20.0: knowledge.doc_chunk + pgvector activation.

Activates the long-reserved ``knowledge`` schema (named in CLAUDE.md
since v0.1 frozen tech stack) and lands the table that backs the
v0.20 cockpit knowledge tool.

The table is search-shaped from the start: ``content_tsv`` is a
GENERATED column over ``content`` (English config), backed by a GIN
index that powers Tier-0 FTS search via ``ts_rank`` + ``ts_headline``.
The ``embedding vector(1024)`` column is added in this same migration
so the Tier-1 hybrid path doesn't need a second migration — the
column is NULL until the embedder pass populates it. HNSW partial
index lives only on rows where the embedding is present, so Tier-0-
only deployments pay nothing for it.

``id`` is a deterministic hash of ``(source_path, anchor)`` so a
re-index of an unchanged section is idempotent (the indexer dedupes
on ``content_hash`` first; this is the second-line guard).

``content_hash`` (sha256 of content) drives idempotent upserts.
``source_mtime`` lets the indexer skip on file mtime match without
hashing every chunk on every run.

The ``CREATE EXTENSION IF NOT EXISTS vector`` activation is
idempotent — operators who pre-installed pgvector (per the
prerequisite in phases/V0_20_0.md) re-run it cheaply; operators who
forgot the prereq see a clear ``could not open extension control
file`` error pointing at docs/runbooks/knowledge-indexer.md.

The bundled-mode Postgres image swap (postgres:16-alpine →
pgvector/pgvector:pg16) is documented in the v0.20 phase doc and the
new knowledge-indexer runbook. BYOI operators run
``CREATE EXTENSION IF NOT EXISTS vector`` once on their target
Postgres before ``make migrate``.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "knowledge"


def upgrade() -> None:
    # The knowledge schema has been reserved in CLAUDE.md frozen tech
    # stack since v0.1; v0.20 is its first activation.
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # Activate pgvector. Idempotent. Fails loud if the extension isn't
    # available — see docs/runbooks/knowledge-indexer.md for the bundled-
    # mode image swap and the BYOI one-liner.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "doc_chunk",
        # Deterministic hash of (source_path, anchor) — idempotent reindex.
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.Column("anchor", sa.Text, nullable=False),
        # Human-readable trail for citations (e.g. "Part 8 → 8.4 → Rotate").
        sa.Column("heading_path", sa.Text, nullable=False),
        # Tagged for kinds-filtered search; one of:
        #   handbook, doctrine, runbook, architecture,
        #   decisions, tool_surface, roadmap, contributing.
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        # GENERATED tsvector. English config covers the doc corpus
        # (intentionally NOT 'simple' — we want stemming + stopword
        # filtering for natural-language search). Indexer can re-run
        # against an existing table without recomputing this column.
        sa.Column(
            "content_tsv",
            sa.dialects.postgresql.TSVECTOR,
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        # sha256(content) — drives idempotent upserts.
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("source_mtime", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "indexed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=SCHEMA,
    )

    # Tier-1 (hybrid) embedding. NULL until the embedder pass runs;
    # vector(1024) is the OpenAI/OpenRouter `text-embedding-3-small`-
    # equivalent dimension. Local sentence-transformer fallback
    # (768-dim mpnet) is padded to 1024 by the embedder so the column
    # shape stays stable; doctrine note in knowledge-indexer.md.
    #
    # Added via raw SQL because Alembic's type system doesn't know
    # pgvector's `vector` type. Adding via ALTER TABLE keeps the
    # migration declarative + idempotent (an ADD COLUMN IF NOT EXISTS
    # would be slightly safer but Postgres syntax requires the column
    # not exist; the migration framework handles re-running on its own
    # via the alembic_version table).
    op.execute(
        f'ALTER TABLE "{SCHEMA}".doc_chunk '
        f"ADD COLUMN embedding vector(1024)"
    )

    # GIN index backs Tier-0 FTS. Without this, ts_rank does a sequential
    # scan and the cockpit's "look this up in the handbook" pattern
    # is unusably slow even at our small corpus size.
    op.execute(
        f'CREATE INDEX ix_doc_chunk_content_tsv '
        f'ON "{SCHEMA}".doc_chunk USING GIN (content_tsv)'
    )

    # HNSW index for Tier-1 cosine similarity. Partial — only indexed
    # rows where the embedding has been computed. Tier-0-only deployments
    # never populate the column and pay zero index cost. m=16, ef_construction=64
    # are pgvector defaults; tunable later if recall suffers at scale.
    op.execute(
        f'CREATE INDEX ix_doc_chunk_embedding_hnsw '
        f'ON "{SCHEMA}".doc_chunk USING hnsw (embedding vector_cosine_ops) '
        f"WHERE embedding IS NOT NULL"
    )

    # Lookup by (source_path, anchor) is the get-by-citation path used by
    # knowledge.get and the citation guard. PK is hash(source_path, anchor)
    # but searching by the natural composite key is cheaper than recomputing
    # the hash in the SELECT clause.
    op.create_index(
        "ix_doc_chunk_source_anchor",
        "doc_chunk",
        ["source_path", "anchor"],
        unique=True,
        schema=SCHEMA,
    )

    # Lookup by source_path alone — used during reindex to find rows that
    # need deletion (source file removed) or update (source file mtime
    # changed but content_hash matches multiple chunks). Not unique.
    op.create_index(
        "ix_doc_chunk_source_path",
        "doc_chunk",
        ["source_path"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_doc_chunk_source_path", table_name="doc_chunk", schema=SCHEMA)
    op.drop_index("ix_doc_chunk_source_anchor", table_name="doc_chunk", schema=SCHEMA)
    op.execute(f'DROP INDEX IF EXISTS "{SCHEMA}".ix_doc_chunk_embedding_hnsw')
    op.execute(f'DROP INDEX IF EXISTS "{SCHEMA}".ix_doc_chunk_content_tsv')
    op.drop_table("doc_chunk", schema=SCHEMA)
    # Note: we intentionally do NOT drop the `vector` extension on
    # downgrade. Other databases on the same Postgres instance may
    # depend on it, and pgvector is harmless when no table uses it.
    # We also do NOT drop the schema — same reason; cheap to keep.
