# Knowledge indexer (handbook RAG, v0.20+)

> **Audience:** operators with admin access running BSS-CLI v0.20+.
> Backs the cockpit's `knowledge.search` / `knowledge.get` tools that
> read the indexed doc corpus (HANDBOOK + CLAUDE + runbooks + ARCHITECTURE
> + DECISIONS + TOOL_SURFACE + ROADMAP + CONTRIBUTING). Operator-initiated;
> no file-watcher in containers.

## When to run

- **First time activating v0.20.** After the schema is applied (`bss admin
  migrate` — the frozen baseline already includes the `knowledge` schema +
  pgvector; the old Alembic `0022`/`0023` are collapsed into it, see
  [`rust-schema-baseline.md`](rust-schema-baseline.md)), run `bss admin
  knowledge reindex` once to populate `knowledge.doc_chunk`.
- **After a docs PR merges to main.** The corpus is frozen between
  reindex runs; new sections won't be citable until the next reindex.
  In a typical small-MVNO workflow, an operator runs `make knowledge-reindex`
  weekly (or after any PR they merge that touches `docs/`).
- **After a doctrine change** (e.g. tightened CLAUDE.md anti-pattern,
  new escalation category) — the indexer should run before the
  cockpit can cite the new state.
- **After a `git pull` on a long-running deployment** that picks up
  doc changes from upstream.

You do NOT need to reindex after:
- Code changes that don't touch `docs/`, `CLAUDE.md`, or the other
  indexed files.
- A schema migration (the embedding column in `doc_chunk` is gated
  on `BSS_KNOWLEDGE_BACKEND=hybrid`; FTS rows survive migration).
- A `make seed` run.

## Prerequisites — pgvector on Postgres

The v0.20 migration `0022` runs `CREATE EXTENSION IF NOT EXISTS vector`.
Stock `postgres:16` and `postgres:16-alpine` images do **not** include
the extension; activation requires either an image swap (bundled mode)
or a one-time `CREATE EXTENSION` (BYOI).

> [!info] **Same-major image swap is data-safe.** The data directory
> format is identical between official `postgres:16` and
> `pgvector/pgvector:pg16` — both are upstream Postgres 16 with
> different extension bundles. The host bind-mount (e.g.
> `/var/lib/postgresql/data`) is preserved across the container
> recreate; no `pg_dump` / `pg_restore` needed. Rollback (image: back
> to `postgres:16`) is reversible in 30 seconds — pgvector simply
> becomes unavailable, and that's fine because Tier-0 FTS doesn't
> need it.

### Bundled mode — image swap

Edit your bundled `postgres` service in `docker-compose.yml` (or
`docker-compose.infra.yml`):

```yaml
postgres:
  image: pgvector/pgvector:pg16    # was: postgres:16-alpine
```

Then:

```bash
# 1. Backup the data directory (cheap insurance; takes seconds).
sudo cp -a /var/lib/postgresql/data /var/lib/postgresql/data.backup-$(date +%Y%m%d)

# 2. Stop cleanly so Postgres flushes WAL.
docker compose stop postgres

# 3. Pull + start.
docker compose pull postgres
docker compose up -d postgres

# 4. Watch the boot log. You want:
#    "database system is ready to accept connections"
#    You do NOT want: "incompatible data directory" or "FATAL".
docker compose logs -f postgres
# Ctrl-C once ready.

# 5. Activate pgvector (idempotent).
docker exec postgres psql -U postgres -d bss \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 6. Bounce the BSS service containers — their connection pools went
#    stale during the Postgres restart and will 500 on first request
#    if you skip this.
docker compose restart \
    catalog crm payment com som subscription mediation rating provisioning-sim \
    portal-self-serve portal-csr

# 7. (Optional) Refresh collation versions silently if you crossed
#    glibc majors (postgres:16-alpine often ships glibc 2.41 vs the
#    Debian-based pgvector image's 2.36):
docker exec postgres psql -U postgres -d postgres \
    -c "ALTER DATABASE bss REFRESH COLLATION VERSION;"

# 8. Apply the schema (baseline already includes knowledge + pgvector).
bss admin migrate

# 9. Index the corpus.
bss admin knowledge reindex
```

### BYOI mode — one-time CREATE EXTENSION

```bash
# Run on the host that owns your Postgres (NOT the bss-cli compose host).
psql -h <pg-host> -U postgres -d bss -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Then back on the bss-cli host:
bss admin migrate
bss admin knowledge reindex
```

If your shared Postgres serves multiple databases (e.g. metabase, n8n),
pgvector is harmless to those — the extension is per-database, and
adding it to `bss` only touches `bss`.

### Pre-swap verification

Run this on the existing Postgres before swapping:

```bash
docker exec postgres psql -U postgres -c "SELECT version();"
# Expected: "PostgreSQL 16.x ..."
# If 15 or earlier → STOP. Same-major swap only.
# If 17+ → STOP. pgvector/pgvector:pg16 only ships 16.x.
```

## Running the reindex

```bash
make knowledge-reindex
# OR equivalently:
bss admin knowledge reindex
```

Output looks like:

```
✓ reindex complete  files=25  added=372  updated=0  deleted=0  skipped=0
```

Subsequent runs (idempotent — `mtime` + `content_hash` dedup):

```
✓ reindex complete  files=25  added=0  updated=0  deleted=0  skipped=372
```

When a doc changes:

```
✓ reindex complete  files=25  added=2  updated=5  deleted=0  skipped=365
```

`--force` re-upserts every chunk regardless of mtime/hash match —
useful after a chunker change or a kind-rank-weight tweak:

```bash
bss admin knowledge reindex --force
```

## Search debug surface

```bash
bss admin knowledge search "rotate cockpit token"
bss admin knowledge search "prebaked KYC env flag" --kind doctrine
bss admin knowledge search "roaming exhausted" --k 5
```

Returns the same shape the cockpit's `knowledge.search` tool returns
(rank-ordered hits with kind, source path, anchor, snippet). Useful
for verifying citation quality before accepting an LLM reply.

```bash
bss admin knowledge list                    # Browse all chunks
bss admin knowledge list --kind handbook    # Just handbook chunks
bss admin knowledge list --limit 200
```

## Search backend modes

`BSS_KNOWLEDGE_BACKEND=fts` (default) — Postgres FTS only. Zero deps
beyond the `pgvector` extension being installable (the migration
adds the column even when unused). Recall is fine for sub-1MB
corpus sizes; semantic queries (paraphrases) sometimes miss the
right chunk.

`BSS_KNOWLEDGE_BACKEND=hybrid` — pgvector cosine similarity + FTS
re-rank. Requires `BSS_KNOWLEDGE_EMBEDDER` set (`openrouter` re-uses
`BSS_LLM_API_KEY`; `local` runs a sentence-transformer offline).
First-time embedding pass takes ~5–10 seconds for the v0.20 corpus
(~370 chunks); subsequent reindexes only re-embed changed chunks.

To switch:

```bash
# Edit .env:
BSS_KNOWLEDGE_BACKEND=hybrid

# Re-embed the corpus (existing FTS rows survive; embedding column
# fills in place):
bss admin knowledge reindex --force

# Restart orchestrator + cockpit so they pick up the new backend.
docker compose restart portal-csr
```

## Troubleshooting

### "could not open extension control file vector.control"

`bss admin migrate` failed because pgvector isn't on the target Postgres.
See [Prerequisites](#prerequisites--pgvector-on-postgres) above.

### "BSS_DB_URL is not set"

The CLI couldn't read your environment. Source `.env` in the same
shell:

```bash
set -a && source .env && set +a
```

Or run via `make`:

```bash
make knowledge-reindex
```

### Reindex "files_seen=0"

The CLI ran but found no docs. Check that you're in the repo root:

```bash
pwd                            # Should end in /bss-cli
ls docs/HANDBOOK.md            # Should exist
ls CLAUDE.md                   # Should exist
```

The indexer walks up from CWD looking for `pyproject.toml`; if you
run from outside the repo it raises a clear error.

### Cockpit says "I don't have a citation for that"

The citation guard tripped because your reply made a handbook claim
without firing `knowledge.search`. Check:

1. Is the corpus indexed? `bss admin knowledge list --limit 5` should
   show ≥4 rows.
2. Does the corpus have a relevant section? `bss admin knowledge
   search "<query>"` should return at least one hit.
3. If both are healthy and the LLM still skips the tool, that's a
   prompt-quality regression — file an issue. The fix is in
   `bss_cockpit.prompts._COCKPIT_INVARIANTS`, not in the regex
   (don't relax the guard).

### "knowledge.search returns no hits"

Either the corpus doesn't have a relevant section (genuine miss —
say so explicitly to the operator) or the FTS query is too narrow.
Try `--k 10` to see if the section is just lower-ranked. For
paraphrased queries, switch to hybrid mode (above) — embeddings
catch semantic matches that FTS misses.

### Embedding pass fails (hybrid mode)

Most common cause: `BSS_LLM_API_KEY` doesn't have the embeddings
permission. OpenRouter free-tier keys generally support embeddings;
verify at openrouter.ai/keys. As a fallback, switch to local
embeddings (`BSS_KNOWLEDGE_EMBEDDER=local`) — first run downloads
a ~150MB model.

## What's NOT in the index

By **explicit doctrine** (`make doctrine-check` rule 16):

- `phases/V0_*.md` — historical build plans, mislead the LLM.
- Anything outside the `INDEXED_PATHS` allowlist in
  `packages/bss-knowledge/bss_knowledge/paths.py`. Adding a new path
  requires a doctrine review and a DECISIONS.md entry.

By **convention** (no doctrine rule, just sensible defaults):

- `tests/`, `scenarios/`, code itself.
- `.env.example` — the handbook covers env vars; pointing the LLM at
  two sources of truth invites drift.
- README.md — light overlap with the handbook intro; we keep the
  handbook authoritative.

## Forensic visibility

Each cockpit turn that calls `knowledge.search` or `knowledge.get`
records the resolved citations into `audit.chat_usage.citations`
(jsonb). Query forensically:

```sql
SELECT customer_id, period_start, citations
FROM audit.chat_usage
WHERE jsonb_array_length(citations) > 0
ORDER BY period_start DESC
LIMIT 20;
```

This is also how you'd catch "the cockpit is citing the same broken
section repeatedly" — high-frequency anchors point at sections that
need rewriting.

## See also

- [HANDBOOK §3.4](../HANDBOOK.md#34-make-targets) — `make
  knowledge-reindex` target.
- [HANDBOOK §3.3](../HANDBOOK.md#33-environment-variables--full-catalogue)
  — `BSS_KNOWLEDGE_*` env vars.
- [HANDBOOK §10.1](../HANDBOOK.md#101-glossary) — knowledge tool
  glossary entry.
- [phases/V0_20_0.md](../../phases/V0_20_0.md) — implementation plan
  + design decisions.
- [TOOL_SURFACE.md "Knowledge tools (v0.20+, operator_cockpit only)"](../../TOOL_SURFACE.md)
  — the formal tool contract.
