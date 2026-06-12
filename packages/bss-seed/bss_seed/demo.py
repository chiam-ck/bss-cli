"""Synced demo seed — BSS + loyalty-cli in lockstep (v1.3.1).

A SEPARATE seed from ``bss_seed.main`` (which is reference data only: agents,
SLAs, plans, msisdn / eSIM pools — works without loyalty, always). That seed
stays untouched.

What this adds, *on top* of reference data:

  - 3 demo customers (Alice / Bob / Carol Demo) in CRM. Customer ids are
    auto-minted (``CUST-<hex>``) — the stable identifier across runs is the
    ``*.demo@bss-cli.local`` email. CRM's eager-sync mirrors them into
    loyalty automatically when ``BSS_LOYALTY_API_TOKEN`` is set. Without
    loyalty: the customers are created in BSS only and the sync step is
    silently skipped (the CRM-side guard).
  - 2 demo promotions: ``PROMO_DEMO_WELCOME`` (public typed, code
    ``DEMO_WELCOME10``, multi_use, 10% off, multi 3 periods) and
    ``PROMO_DEMO_VIP`` (targeted, 20% off single, code derived from id).
    Created via the catalog HTTP API — same saga ``bss promo create`` runs,
    so loyalty's OD + promo_code register fire in lockstep. Without loyalty:
    skipped with a log line (catalog refuses ``loyalty_not_configured``).
  - Alice + Bob are assigned to the targeted VIP. v1.3.0 mints the loyalty
    offer pairing upfront (``offer.issue``) so loyalty's per-customer views
    show both customers paired with the promo immediately.

Idempotent: re-runs check existence first and skip; loyalty's deterministic
offer ids + idempotency keys make its side no-op on re-run too.

The companion ``reset`` removes everything ``seed`` creates from BOTH systems
(unassign → revoke loyalty offers + delete eligibility, drop promotions in
BSS, delete demo customers in BSS + loyalty). Surgical: only touches things
keyed on the ``*.demo@bss-cli.local`` email or ``PROMO_DEMO_*`` id, never
operator data.

Usage:
    BSS_API_TOKEN=... python -m bss_seed.demo seed \
        [--loyalty-base-url URL] [--loyalty-token TOKEN]
    BSS_API_TOKEN=... python -m bss_seed.demo reset \
        [--loyalty-base-url URL] [--loyalty-token TOKEN]
    BSS_API_TOKEN=... python -m bss_seed.demo loyalty-wipe

Loyalty wiring (``--loyalty-base-url`` + ``--loyalty-token``) is optional —
omit both for BSS-only mode. The Makefile targets (``make seed-demo`` /
``make seed-demo-reset``) read ``$BSS_LOYALTY_BASE_URL`` and
``$BSS_LOYALTY_API_TOKEN`` from the shell environment and pass them through
as CLI flags; this keeps the Python module clear of per-process token
reads (doctrine: tokens loaded once at startup, see
``packages/bss-middleware/bss_middleware/api_token.py``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import asyncpg
from bss_clients import (
    BearerAuthProvider,
    CatalogClient,
    CRMClient,
    LoyaltyClient,
    NotFound,
    PolicyViolationFromServer,
    TokenAuthProvider,
)

# ─── demo dataset ────────────────────────────────────────────────────────────

DEMO_EMAIL_DOMAIN = "demo@bss-cli.local"

# (name, stable-email — the across-runs identifier)
DEMO_CUSTOMERS: list[tuple[str, str]] = [
    ("Alice Demo", f"alice.{DEMO_EMAIL_DOMAIN}"),
    ("Bob Demo", f"bob.{DEMO_EMAIL_DOMAIN}"),
    ("Carol Demo", f"carol.{DEMO_EMAIL_DOMAIN}"),
]

DEMO_PROMO_WELCOME: dict[str, Any] = {
    "promotion_id": "PROMO_DEMO_WELCOME",
    "display_name": "Demo Welcome 10%",
    "discount_type": "percent",
    "discount_value": "10",
    "duration_kind": "multi",
    "periods_total": 3,
    "audience": "public",
    "code": "DEMO_WELCOME10",
    "promo_code_kind": "multi_use",
}

DEMO_PROMO_VIP: dict[str, Any] = {
    "promotion_id": "PROMO_DEMO_VIP",
    "display_name": "Demo VIP 20%",
    "discount_type": "percent",
    "discount_value": "20",
    "duration_kind": "single",
    "audience": "targeted",
    # No `code` — catalog derives it from the id (defaults to
    # single_use_unique_per_customer for targeted promos).
}

DEMO_VIP_ASSIGNED_EMAILS: list[str] = [
    f"alice.{DEMO_EMAIL_DOMAIN}",
    f"bob.{DEMO_EMAIL_DOMAIN}",
]


# ─── env wiring ──────────────────────────────────────────────────────────────


def _env() -> dict[str, str]:
    """Read what we need from the process env. Service URLs default to the
    host-side compose ports so ``make seed-demo`` Just Works without further
    wiring. ``BSS_DB_URL`` + ``BSS_API_TOKEN`` are required (no safe default).

    Note: loyalty wiring is intentionally NOT read here. The loyalty base url
    + token are CLI-flag inputs (see ``_cli()`` and the ``loyalty_*`` kwargs
    on ``seed()`` / ``reset()``). This keeps the loyalty token off the Python
    side of ``os.environ`` — doctrine "tokens loaded once at startup" wants
    that read at the process boundary (the Makefile shell), not scattered
    through helper modules.
    """
    out: dict[str, str] = {}
    for var in ("BSS_DB_URL", "BSS_API_TOKEN"):
        v = os.environ.get(var)
        if not v:
            print(f"ERROR: {var} is required.", file=sys.stderr)
            sys.exit(1)
        out[var] = v
    # Host-side defaults — match the existing repo-root seed scripts.
    out["BSS_CRM_URL"] = os.environ.get("BSS_CRM_URL", "http://localhost:8002")
    out["BSS_CATALOG_URL"] = os.environ.get("BSS_CATALOG_URL", "http://localhost:8001")
    # Loyalty entries default to empty; ``seed()`` / ``reset()`` overlay the
    # CLI-passed values on top before constructing the loyalty client.
    out["BSS_LOYALTY_BASE_URL"] = ""
    out["BSS_LOYALTY_API_TOKEN"] = ""
    return out


async def _connect_db(env: dict[str, str]) -> asyncpg.Connection:
    return await asyncpg.connect(
        env["BSS_DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    )


def _crm(env: dict[str, str]) -> CRMClient:
    return CRMClient(
        base_url=env["BSS_CRM_URL"], auth_provider=TokenAuthProvider(env["BSS_API_TOKEN"])
    )


def _catalog(env: dict[str, str]) -> CatalogClient:
    return CatalogClient(
        base_url=env["BSS_CATALOG_URL"], auth_provider=TokenAuthProvider(env["BSS_API_TOKEN"])
    )


def _loyalty(env: dict[str, str]) -> LoyaltyClient | None:
    """``None`` when no loyalty token → BSS-only mode."""
    if not env["BSS_LOYALTY_API_TOKEN"] or not env["BSS_LOYALTY_BASE_URL"]:
        return None
    return LoyaltyClient(
        base_url=env["BSS_LOYALTY_BASE_URL"],
        auth_provider=BearerAuthProvider(env["BSS_LOYALTY_API_TOKEN"]),
    )


# ─── customer lookup by email (the stable identifier across runs) ───────────


async def _customer_id_by_email(conn: asyncpg.Connection, email: str) -> str | None:
    return await conn.fetchval(
        """
        SELECT c.id
        FROM crm.customer c
        JOIN crm.contact_medium cm ON cm.party_id = c.party_id
        WHERE cm.medium_type = 'email' AND cm.value = $1
        LIMIT 1
        """,
        email,
    )


# ─── seed ────────────────────────────────────────────────────────────────────


async def seed(
    *,
    loyalty_base_url: str = "",
    loyalty_token: str = "",
    verbose: bool = True,
) -> dict[str, int]:
    env = _env()
    env["BSS_LOYALTY_BASE_URL"] = loyalty_base_url
    env["BSS_LOYALTY_API_TOKEN"] = loyalty_token
    crm = _crm(env)
    catalog = _catalog(env)
    loyalty = _loyalty(env)
    conn = await _connect_db(env)

    summary = {
        "customers_created": 0,
        "customers_skipped": 0,
        "promotions_created": 0,
        "promotions_skipped": 0,
        "vip_assigned": 0,
        "vip_already": 0,
    }
    email_to_id: dict[str, str] = {}

    def _log(line: str) -> None:
        if verbose:
            print(line)

    _log("── demo seed (BSS + loyalty in sync) ──")
    if loyalty is None:
        _log("  loyalty token not set — BSS-only mode (customers only)")

    try:
        # 1) customers (stable identifier = email)
        for name, email in DEMO_CUSTOMERS:
            cid = await _customer_id_by_email(conn, email)
            if cid is not None:
                summary["customers_skipped"] += 1
                _log(f"  · customer {name} <{email}> already exists → {cid}")
            else:
                created = await crm.create_customer(name=name, email=email)
                cid = created["id"]
                summary["customers_created"] += 1
                _log(f"  + customer {name} <{email}> → {cid}")
            email_to_id[email] = cid

        # 2) promotions (BSS+loyalty saga via catalog API). Skip whole lane on
        #    BSS-only mode (catalog refuses without loyalty).
        if loyalty is None:
            _log("  · promos skipped (no loyalty)")
        else:
            for spec in (DEMO_PROMO_WELCOME, DEMO_PROMO_VIP):
                try:
                    await catalog.get_promotion(spec["promotion_id"])
                    summary["promotions_skipped"] += 1
                    _log(f"  · promotion {spec['promotion_id']} already exists")
                    continue
                except NotFound:
                    pass

                try:
                    await catalog.create_promotion(**spec)
                    summary["promotions_created"] += 1
                    _log(f"  + promotion {spec['promotion_id']} ({spec['audience']})")
                except PolicyViolationFromServer as exc:
                    if exc.rule == "catalog.promotion.loyalty_not_configured":
                        _log("  · promo skipped (catalog → loyalty_not_configured)")
                        break
                    raise

            # 3) targeted assign — v1.3.0 mints the loyalty offer upfront.
            assign_ids = [email_to_id[e] for e in DEMO_VIP_ASSIGNED_EMAILS if e in email_to_id]
            if assign_ids:
                try:
                    res = await catalog.assign_promotion(
                        DEMO_PROMO_VIP["promotion_id"], customer_ids=assign_ids
                    )
                    summary["vip_assigned"] = len(res.get("eligible", []))
                    summary["vip_already"] = len(res.get("already", []))
                    for cid in res.get("eligible", []):
                        _log(f"  + assigned {cid} → PROMO_DEMO_VIP (loyalty offer minted)")
                    for cid in res.get("already", []):
                        _log(f"  · {cid} already eligible for PROMO_DEMO_VIP")
                except PolicyViolationFromServer as exc:
                    # Targeted promo wasn't created (loyalty refusal earlier) —
                    # already logged; skip silently here.
                    if exc.rule != "catalog.promotion.not_targeted":
                        raise
    finally:
        await crm.close()
        await catalog.close()
        if loyalty is not None:
            await loyalty.close()
        await conn.close()

    if verbose:
        print("\ndone:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return summary


# ─── reset (demo prefix only — never touches operator data) ─────────────────


async def reset(
    *,
    loyalty_base_url: str = "",
    loyalty_token: str = "",
    verbose: bool = True,
) -> dict[str, int]:
    """Mirror of ``seed()``. Removes:
      1. eligibility rows for the demo targeted promo (loyalty.offer.revoke + BSS delete)
      2. demo promotions in BSS
      3. demo customers in BSS (cascade through individual / contact_medium / party / interaction)
      4. demo customers in loyalty (best-effort)
    """
    env = _env()
    env["BSS_LOYALTY_BASE_URL"] = loyalty_base_url
    env["BSS_LOYALTY_API_TOKEN"] = loyalty_token
    crm = _crm(env)
    catalog = _catalog(env)
    loyalty = _loyalty(env)
    conn = await _connect_db(env)

    summary = {
        "unassigned": 0,
        "promotions_deleted": 0,
        "customers_deleted_bss": 0,
        "customers_deleted_loyalty": 0,
    }

    def _log(line: str) -> None:
        if verbose:
            print(line)

    _log("── demo reset (BSS + loyalty, demo-prefix only) ──")

    try:
        # Resolve demo customer ids by email (stable identifier).
        demo_ids: list[str] = []
        for _, email in DEMO_CUSTOMERS:
            cid = await _customer_id_by_email(conn, email)
            if cid is not None:
                demo_ids.append(cid)

        # 1) unassign the targeted VIP via the API path so loyalty's
        #    offer.revoke fires (v1.3.1).
        if loyalty is not None and demo_ids:
            try:
                res = await catalog.unassign_promotion(
                    DEMO_PROMO_VIP["promotion_id"], customer_ids=demo_ids
                )
                summary["unassigned"] = len(res.get("removed", []))
                for cid in res.get("removed", []):
                    _log(f"  - unassigned {cid} from PROMO_DEMO_VIP (loyalty cleared)")
            except (NotFound, PolicyViolationFromServer) as exc:
                _log(f"  · unassign skipped: {getattr(exc, 'rule', type(exc).__name__)}")

        # 2) drop demo promotions in BSS (catalog has no "delete promotion"
        #    verb; demo-prefix raw delete is surgical and safe).
        for spec in (DEMO_PROMO_WELCOME, DEMO_PROMO_VIP):
            await conn.execute(
                "DELETE FROM catalog.promotion_eligibility WHERE promotion_id = $1",
                spec["promotion_id"],
            )
            tag = await conn.execute(
                "DELETE FROM catalog.promotion WHERE id = $1", spec["promotion_id"]
            )
            if tag.endswith(" 1"):
                summary["promotions_deleted"] += 1
                _log(f"  - promotion {spec['promotion_id']} deleted (BSS)")

        # 3) drop demo customers in BSS. Real customers are soft-archived;
        #    these are clearly-tagged demo rows so a surgical raw delete is OK.
        for cid in demo_ids:
            party = await conn.fetchval(
                "SELECT party_id FROM crm.customer WHERE id = $1", cid
            )
            if party is None:
                continue
            await conn.execute("DELETE FROM crm.interaction WHERE customer_id = $1", cid)
            await conn.execute("DELETE FROM crm.customer WHERE id = $1", cid)
            await conn.execute("DELETE FROM crm.contact_medium WHERE party_id = $1", party)
            await conn.execute("DELETE FROM crm.individual WHERE party_id = $1", party)
            await conn.execute("DELETE FROM crm.party WHERE id = $1", party)
            summary["customers_deleted_bss"] += 1
            _log(f"  - customer {cid} deleted (BSS)")

        # 4) loyalty-side customer delete (best-effort). loyalty may or may not
        #    expose a delete tool; treat any refusal as "leave it" and move on.
        if loyalty is not None and demo_ids:
            for cid in demo_ids:
                try:
                    await loyalty._call(  # noqa: SLF001 — admin-style direct call
                        "customer.delete",
                        {"customer_id": cid},
                        idempotency_key=f"DEMO-RESET-CUST-{cid}",
                    )
                    summary["customers_deleted_loyalty"] += 1
                    _log(f"  - customer {cid} deleted (loyalty)")
                except (NotFound, PolicyViolationFromServer):
                    pass
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _log(f"  · loyalty delete {cid} skipped ({type(exc).__name__})")
    finally:
        await crm.close()
        await catalog.close()
        if loyalty is not None:
            await loyalty.close()
        await conn.close()

    if verbose:
        print("\ndone:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return summary


async def wipe_loyalty(*, verbose: bool = True) -> dict[str, int]:
    """Full wipe of loyalty's data (truncate ``loyalty.*`` + ``audit.*`` schemas,
    then re-stamp ``alembic_version`` to the migration head).

    Companion to ``make reset-db`` on the BSS side — these two together restore
    a clean slate across both systems. Used by ``make demo-restore``.

    Resolves the loyalty DB url from (in order):
      1. ``LOYALTY_DB_URL`` env var (explicit operator-supplied).
      2. The ``LOYALTY_DB_URL`` env on the running ``loyalty-http`` container.
    Fails clearly if neither is available — no silent guessing about which DB
    to nuke.

    Re-stamps alembic from the latest migration file's ``revision: str = "..."``
    line (path: ``~/claude/loyalty-cli/packages/loyalty-store-postgres/migrations/versions/``
    by default; override with ``LOYALTY_MIGRATIONS_DIR``). Falls back to leaving
    the version table empty (loyalty still boots; ``loyalty doctor`` will say
    ``no alembic_version row``) when the migrations dir isn't reachable.
    """
    import re
    import subprocess
    from pathlib import Path

    def _log(line: str) -> None:
        if verbose:
            print(line)

    db_url = os.environ.get("LOYALTY_DB_URL")
    if not db_url:
        try:
            out = subprocess.run(
                [
                    "docker", "inspect", "loyalty-http",
                    "--format",
                    "{{range .Config.Env}}{{println .}}{{end}}",
                ],
                capture_output=True, text=True, check=True,
            ).stdout
            for line in out.splitlines():
                if line.startswith("LOYALTY_DB_URL="):
                    db_url = line.split("=", 1)[1]
                    break
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    if not db_url:
        print(
            "ERROR: LOYALTY_DB_URL not set and `docker inspect loyalty-http` "
            "couldn't read it. Set it explicitly or start loyalty first.",
            file=sys.stderr,
        )
        sys.exit(1)

    summary = {"tables_truncated": 0, "alembic_stamped": 0}

    _log("── loyalty wipe (truncate loyalty + audit schemas) ──")
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('loyalty','audit')"
        )
        if not rows:
            _log("  (no tables in loyalty/audit schemas — loyalty not migrated?)")
            return summary
        tables = ", ".join(
            f'"{r["table_schema"]}"."{r["table_name"]}"' for r in rows
        )
        await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        summary["tables_truncated"] = len(rows)
        _log(f"  - truncated {len(rows)} tables across loyalty + audit")

        # Re-stamp alembic head from the latest migration file's revision id.
        migrations_dir = Path(
            os.environ.get(
                "LOYALTY_MIGRATIONS_DIR",
                str(Path.home() / "claude/loyalty-cli/packages/loyalty-store-postgres/migrations/versions"),
            )
        )
        head_rev: str | None = None
        if migrations_dir.is_dir():
            migration_files = sorted(p for p in migrations_dir.glob("*.py") if not p.name.startswith("_"))
            if migration_files:
                text = migration_files[-1].read_text(encoding="utf-8")
                m = re.search(r'^revision(?:\s*:\s*\w+)?\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
                if m:
                    head_rev = m.group(1)
        if head_rev:
            await conn.execute(
                "INSERT INTO loyalty.alembic_version (version_num) VALUES ($1)", head_rev
            )
            summary["alembic_stamped"] = 1
            _log(f"  - stamped alembic head: {head_rev}")
        else:
            _log(
                f"  · couldn't resolve head revision from {migrations_dir} — "
                "leaving alembic_version empty (loyalty doctor will flag)"
            )
    finally:
        await conn.close()

    if verbose:
        print("\ndone:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    return summary


def _parse_loyalty_flags(args: list[str]) -> tuple[str, str, list[str]]:
    """Pull ``--loyalty-base-url`` / ``--loyalty-token`` out of ``args``.

    Returns ``(base_url, token, remaining_args)``. The two flags are
    independent and both default to empty (BSS-only mode). The shell side
    (Makefile target ``seed-demo`` / ``seed-demo-reset``) reads the env
    vars and passes them as flags — Python never reads the loyalty token
    from ``os.environ``.
    """
    base_url = ""
    token = ""
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--loyalty-base-url" and i + 1 < len(args):
            base_url = args[i + 1]
            i += 2
        elif args[i] == "--loyalty-token" and i + 1 < len(args):
            token = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return base_url, token, remaining


def _cli() -> None:
    """Entry points: ``python -m bss_seed.demo seed | reset | loyalty-wipe``.

    Optional flags (``seed`` and ``reset`` only):
      ``--loyalty-base-url URL``  — loyalty-http base URL
      ``--loyalty-token TOKEN``   — loyalty admin bearer

    Both default to empty → BSS-only mode (customer half runs, promo lane
    skips). Pass them via ``make seed-demo`` (which threads
    ``$BSS_LOYALTY_BASE_URL`` and ``$BSS_LOYALTY_API_TOKEN`` through from
    the shell).
    """
    loyalty_base_url, loyalty_token, rest = _parse_loyalty_flags(sys.argv[1:])
    cmd = rest[0] if rest else "seed"
    if cmd == "seed":
        asyncio.run(
            seed(loyalty_base_url=loyalty_base_url, loyalty_token=loyalty_token)
        )
    elif cmd == "reset":
        asyncio.run(
            reset(loyalty_base_url=loyalty_base_url, loyalty_token=loyalty_token)
        )
    elif cmd == "loyalty-wipe":
        # ``loyalty-wipe`` resolves the DB URL itself (env or docker inspect
        # of loyalty-http); no token needed since it talks straight to Postgres.
        asyncio.run(wipe_loyalty())
    else:
        print(
            f"unknown subcommand {cmd!r}; use seed | reset | loyalty-wipe",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    _cli()
