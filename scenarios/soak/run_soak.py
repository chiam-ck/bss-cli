"""v0.12 14-day soak runner — argparse entrypoint.

Designed for two invocations:

    # Full soak (commit the report).
    uv run python -m scenarios.soak.run_soak --customers 100 --days 14

    # Smoke (validates wiring without burning the budget).
    uv run python -m scenarios.soak.run_soak --customers 2 --days 1

Pre-conditions: the v0.12 docker compose stack must be up, the DB
must be migrated + seeded with the catalog. The runner does not
manage the stack.

Steps:

1. **Setup phase** — for each synthetic customer:
   * Create the customer record + KYC + COF + order via the
     orchestrator's bss-clients (real HTTP, fast).
   * Wait for the order to complete so the subscription is active.
   * Mint a portal_auth identity + verified linked-customer session
     directly in the DB.

2. **Run phase** — for each simulated day, in parallel across all
   customers, fire ``run_one_day`` on the SyntheticCustomer. After
   every day, advance every service's frozen clock by 24h.

3. **Report phase** — sample the after-DB snapshot, render the
   markdown report, write it to ``soak/report-v0.12.md`` (or the
   path the caller passes via ``--report``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Make sure repo root is on sys.path so a direct `python -m` invocation
# resolves the local package layout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bss_orchestrator.clients import get_clients  # noqa: E402

from .metrics import SoakRunMetrics, render_report, snapshot_db  # noqa: E402
from .synthetic_customer import SyntheticCustomer, TurnResult  # noqa: E402

log = structlog.get_logger(__name__)


# Service base URLs the runner uses. Default to the docker-compose
# host port mappings; override via env if running against a different
# topology.
PORTAL_BASE = os.environ.get(
    "BSS_SOAK_PORTAL_BASE", "http://localhost:9001"
)
SERVICE_BASES = {
    "crm": os.environ.get("BSS_SOAK_CRM_BASE", "http://localhost:8002"),
    "catalog": os.environ.get("BSS_SOAK_CATALOG_BASE", "http://localhost:8001"),
    "payment": os.environ.get("BSS_SOAK_PAYMENT_BASE", "http://localhost:8003"),
    "com": os.environ.get("BSS_SOAK_COM_BASE", "http://localhost:8004"),
    "subscription": os.environ.get(
        "BSS_SOAK_SUBSCRIPTION_BASE", "http://localhost:8006"
    ),
}


# ─── Setup helpers ──────────────────────────────────────────────────


async def _provision_customer(
    seq: int, *, plan_id: str = "PLAN_M"
) -> str | None:
    """Provision one BSS customer through the standard signup chain.
    Returns the customer_id or None on failure."""
    clients = get_clients()
    try:
        cust = await clients.crm.create_customer(
            name=f"Soak Customer {seq}",
            email=f"soak-{seq:03d}@example.test",
            phone=f"+6590{seq:05d}",
        )
        cust_id = cust["id"]

        await clients.crm.attest_kyc(
            cust_id,
            provider="myinfo",
            attestation_token="KYC-PREBAKED-001",
        )
        # Add card. The mock tokenizer accepts any Luhn-valid PAN.
        await clients.payment.create_payment_method(
            customer_id=cust_id,
            card_token=f"tok_soak_{seq}",
            last4="4242",
            brand="visa",
        )
        order = await clients.com.create_order(
            customer_id=cust_id,
            offering_id=plan_id,
        )
        await clients.com.submit_order(order["id"])
        # Wait for activation. SOM finishes in 1-2s; cap at 10s.
        for _ in range(40):
            o = await clients.com.get_order(order["id"])
            if o.get("state") == "completed":
                break
            await asyncio.sleep(0.25)
        else:
            log.warning("soak.activation_timeout", customer_id=cust_id)
            return None
        return cust_id
    except Exception as exc:  # noqa: BLE001
        log.warning("soak.provision_failed", seq=seq, error=str(exc))
        return None


async def _mint_session(db_url: str, *, email: str, customer_id: str) -> str | None:
    """Create a verified linked-customer portal_auth session and
    return the session_id. Idempotent on email — re-running the soak
    against a dirty DB will fail to seed (use ``make reset-db``)."""
    from bss_portal_auth.test_helpers import create_test_session

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            sess, _ = await create_test_session(
                db, email=email, customer_id=customer_id, verified=True
            )
            await db.commit()
            return sess.id
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "soak.mint_session_failed",
            email=email,
            customer_id=customer_id,
            error=str(exc),
        )
        return None
    finally:
        await engine.dispose()


async def _setup_synthetic_customers(
    n: int, *, db_url: str
) -> list[SyntheticCustomer]:
    """Provision n customers + sessions in parallel batches.

    Batched at 10 to avoid hammering the COM/SOM pipeline; the soak
    is a load test of the chat surface, not the activation path."""
    out: list[SyntheticCustomer] = []
    rng_root = random.Random(0xBA5511)
    for batch_start in range(0, n, 10):
        seqs = list(range(batch_start, min(batch_start + 10, n)))
        cust_ids = await asyncio.gather(
            *(_provision_customer(seq) for seq in seqs)
        )
        for seq, cust_id in zip(seqs, cust_ids):
            if cust_id is None:
                continue
            email = f"soak-{seq:03d}@example.test"
            sid = await _mint_session(
                db_url, email=email, customer_id=cust_id
            )
            if sid is None:
                continue
            out.append(
                SyntheticCustomer(
                    customer_id=cust_id,
                    session_cookie=sid,
                    portal_base=PORTAL_BASE,
                    rng=random.Random(rng_root.getrandbits(64)),
                )
            )
    log.info("soak.customers_ready", count=len(out), requested=n)
    return out


# ─── Day loop + clock advance ───────────────────────────────────────


async def _advance_clocks(client: httpx.AsyncClient, *, duration: str = "1d") -> None:
    """Advance every BSS service's frozen clock by ``duration``.

    The clock router is mounted on every service at
    ``/admin-api/v1/clock/advance``. Failures are logged and tolerated
    — the soak proceeds with whichever services moved."""
    token = os.environ.get("BSS_API_TOKEN", "")
    headers = {"X-BSS-API-Token": token} if token else {}
    for name, base in SERVICE_BASES.items():
        try:
            r = await client.post(
                f"{base}/admin-api/v1/clock/advance",
                json={"duration": duration},
                headers=headers,
                timeout=5.0,
            )
            if r.status_code >= 400:
                log.warning(
                    "soak.clock_advance_failed",
                    service=name,
                    status=r.status_code,
                    body=r.text[:120],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "soak.clock_advance_error",
                service=name,
                error=str(exc),
            )


async def _freeze_clocks_at(client: httpx.AsyncClient, when_iso: str) -> None:
    """Pin every BSS service's clock to the same ``at`` so the soak
    starts deterministic. Best-effort."""
    token = os.environ.get("BSS_API_TOKEN", "")
    headers = {"X-BSS-API-Token": token} if token else {}
    for name, base in SERVICE_BASES.items():
        try:
            await client.post(
                f"{base}/admin-api/v1/clock/freeze",
                json={"at": when_iso},
                headers=headers,
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass


async def _run_day(
    customers: list[SyntheticCustomer],
    *,
    day_index: int,
    client: httpx.AsyncClient,
) -> list[TurnResult]:
    coros = [c.run_one_day(client, day_index=day_index) for c in customers]
    grouped = await asyncio.gather(*coros, return_exceptions=False)
    flat: list[TurnResult] = []
    for batch in grouped:
        flat.extend(batch)
    return flat


# ─── Entrypoint ─────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scenarios.soak.run_soak",
        description="v0.12 14-day soak runner.",
    )
    p.add_argument("--customers", type=int, default=100)
    p.add_argument("--days", type=int, default=14)
    p.add_argument(
        "--report",
        type=Path,
        default=_REPO_ROOT / "soak" / "report-v0.12.md",
    )
    p.add_argument(
        "--start-iso",
        default="2026-05-01T00:00:00+00:00",
        help="Frozen clock starting moment.",
    )
    p.add_argument(
        "--notes",
        default="",
        help="Operator notes appended to the report.",
    )
    p.add_argument(
        "--write-report",
        action="store_true",
        default=True,
        help="Write the report to --report (default true).",
    )
    p.add_argument(
        "--no-write-report",
        action="store_false",
        dest="write_report",
        help="Skip writing the report (smoke test mode).",
    )
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()

    db_url = os.environ.get("BSS_DB_URL", "")
    if not db_url:
        print("BSS_DB_URL must be set. Did you `set -a; source .env; set +a`?")
        return 2

    metrics = SoakRunMetrics(
        customers=args.customers,
        days=args.days,
        started_at=datetime.now(timezone.utc),
    )

    async with httpx.AsyncClient() as admin_client:
        # Pin every service's clock to the same start moment so day-N
        # advances are uniform across the topology.
        await _freeze_clocks_at(admin_client, args.start_iso)

        log.info(
            "soak.snapshot_before.start",
            customers=args.customers,
            days=args.days,
        )
        metrics.snapshot_before = await snapshot_db(db_url)

        log.info("soak.provisioning.start")
        customers = await _setup_synthetic_customers(
            args.customers, db_url=db_url
        )
        if not customers:
            print("No customers provisioned. Check the stack.")
            return 3

        log.info("soak.run.start", customer_count=len(customers))
        async with httpx.AsyncClient() as portal_client:
            for day in range(args.days):
                day_t0 = time.perf_counter()
                results = await _run_day(
                    customers, day_index=day, client=portal_client
                )
                metrics.record(results)
                await _advance_clocks(admin_client, duration="1d")
                log.info(
                    "soak.day.done",
                    day_index=day,
                    events=len(results),
                    successes=sum(1 for r in results if r.success),
                    failures=sum(1 for r in results if not r.success),
                    wall_s=round(time.perf_counter() - day_t0, 2),
                )

        log.info("soak.snapshot_after.start")
        metrics.snapshot_after = await snapshot_db(db_url)
        metrics.ended_at = datetime.now(timezone.utc)

    report = render_report(metrics, notes=args.notes)
    if args.write_report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Wrote {args.report}")
    print(report)
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
