"""`bss admin knowledge ...` — operator-driven indexer + search debug surface.

v0.20+. Three subcommands:

* ``reindex [--force]`` — walk INDEXED_PATHS, chunk on headings, upsert
  into ``knowledge.doc_chunk``. Idempotent. ``--force`` re-upserts even
  when content_hash + source_mtime match (use after schema changes or
  ranking-weight adjustments).
* ``search <query> [--k 3] [--kind handbook]`` — Tier-0 FTS debug surface.
  Mirrors what the cockpit's ``knowledge.search`` tool returns — useful
  for verifying citation quality before accepting the LLM's reply.
* ``list [--limit 50] [--kind handbook]`` — paginated browse over
  ``knowledge.doc_chunk`` (anchor + kind + heading_path + content_hash
  + indexed_at).

Reindex is operator-initiated by doctrine (no file-watcher in the
cockpit container; the doc corpus changes with PRs).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from bss_knowledge import INDEXED_PATHS, Indexer, search_fts
from rich import print as rprint
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .._runtime import run_async

app = typer.Typer(
    help="Operator-driven doc-corpus indexer + search debug surface (v0.20+).",
    no_args_is_help=True,
)


def _repo_root() -> Path:
    """Walk up until we find pyproject.toml — works whether `bss admin
    knowledge reindex` runs from repo root, a sub-dir, or a packaged
    install pointing at a checkout via env."""
    p = Path.cwd().resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root (no pyproject.toml found)")


def _db_url() -> str:
    import os
    url = os.environ.get("BSS_DB_URL", "")
    if not url:
        raise RuntimeError(
            "BSS_DB_URL is not set. Source the repo .env (`set -a; source .env; set +a`) "
            "or export it explicitly before running this command."
        )
    return url


@app.command("reindex")
def reindex(
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-upsert all chunks regardless of mtime/hash match."),
    ] = False,
) -> None:
    """Walk INDEXED_PATHS, chunk on headings, upsert into knowledge.doc_chunk."""

    async def _do() -> None:
        engine = create_async_engine(_db_url())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                idx = Indexer(s, _repo_root())
                report = await idx.reindex(force=force)
        finally:
            await engine.dispose()

        rprint(
            f"[green]✓[/] reindex complete  "
            f"files={report.files_seen}  "
            f"added={report.added}  updated={report.updated}  "
            f"deleted={report.deleted}  skipped={report.skipped_unchanged}"
        )

    run_async(_do())


@app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Natural-language search query.")],
    k: Annotated[int, typer.Option("--k", help="Top-K hits to return.")] = 3,
    kind: Annotated[
        Optional[str],
        typer.Option(
            "--kind",
            help="Filter by doc kind (handbook, doctrine, runbook, "
                 "architecture, decisions, tool_surface, roadmap, contributing). "
                 "Repeat for multiple kinds (use --kind handbook --kind doctrine).",
        ),
    ] = None,
) -> None:
    """Tier-0 FTS search debug surface — returns the same shape the
    cockpit's knowledge.search tool returns."""

    async def _do() -> None:
        engine = create_async_engine(_db_url())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                hits = await search_fts(
                    s, query=query, k=k, kinds=[kind] if kind else None
                )
        finally:
            await engine.dispose()

        if not hits:
            rprint(f"[yellow]No hits for query={query!r}[/]")
            return

        table = Table(title=f"knowledge.search — {len(hits)} hit(s)")
        table.add_column("rank", justify="right")
        table.add_column("kind", style="cyan")
        table.add_column("source", style="dim")
        table.add_column("anchor")
        table.add_column("snippet", max_width=80)
        for h in hits:
            table.add_row(
                f"{h.rank:.3f}",
                h.kind,
                h.source_path,
                h.anchor,
                # ts_headline returns ‹match› brackets; strip them for table
                # readability while keeping them in the structured payload.
                h.snippet.replace("‹", "[bold]").replace("›", "[/]")[:240],
            )
        rprint(table)

    run_async(_do())


@app.command("list")
def list_chunks(
    limit: Annotated[int, typer.Option("--limit")] = 50,
    kind: Annotated[Optional[str], typer.Option("--kind")] = None,
) -> None:
    """Paginated browse over knowledge.doc_chunk."""

    async def _do() -> None:
        engine = create_async_engine(_db_url())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                params: dict = {"limit": limit}
                where = ""
                if kind:
                    where = "WHERE kind = :kind"
                    params["kind"] = kind
                rows = await s.execute(
                    text(
                        f"SELECT source_path, anchor, kind, heading_path, "
                        f"       LEFT(content_hash, 8) AS hash8, indexed_at "
                        f"FROM knowledge.doc_chunk {where} "
                        f"ORDER BY source_path, anchor LIMIT :limit"
                    ),
                    params,
                )
                results = list(rows)
        finally:
            await engine.dispose()

        table = Table(title=f"knowledge.doc_chunk ({len(results)} row(s))")
        table.add_column("source")
        table.add_column("anchor")
        table.add_column("kind", style="cyan")
        table.add_column("heading_path", max_width=60)
        table.add_column("hash8", style="dim")
        for r in results:
            table.add_row(r.source_path, r.anchor, r.kind, r.heading_path, r.hash8)
        rprint(table)
        rprint(f"[dim]allowlist: {len(INDEXED_PATHS)} files[/]")

    run_async(_do())
