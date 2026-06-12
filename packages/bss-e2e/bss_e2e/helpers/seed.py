"""E2E seed helpers — idempotent ``PROMO_E2E_*`` and ``e2e-*`` factories.

These layer on top of ``bss-clients`` (the doctrine-compliant write path)
and never touch the DB directly. Setup goes through services; verification
reads may use asyncpg in the spec itself but not here.

**Naming.** All e2e artefacts use the ``e2e-`` prefix on customer emails
and the ``PROMO_E2E_`` prefix on promo ids — disjoint from the operator
demo data (``*.demo@bss-cli.local`` / ``PROMO_DEMO_*``). Surgical cleanup
at teardown can target one prefix without touching the other.

**Idempotency.** Each function checks-then-creates. Re-running ``make e2e``
should be a no-op on the seed step, not a 409-Conflict cascade.

**Env wiring.** Same convention as ``bss_seed.demo`` — required env:
``BSS_DB_URL``, ``BSS_API_TOKEN``. Service URLs default to the host-side
compose ports. The seed never touches the loyalty token: targeted-assignment
in the spec uses the catalog HTTP API which delegates to loyalty server-side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import asyncpg
from bss_clients import (
    CatalogClient,
    CRMClient,
    NotFound,
    TokenAuthProvider,
)

# Module constants — promotion ids, codes, customer-email prefix. Specs
# import these rather than hardcoding strings, so a rename happens in
# exactly one place.
PROMO_PUBLIC_ID = "PROMO_E2E_PUBLIC"
PROMO_PUBLIC_CODE = "E2E_PUBLIC10"
PROMO_TARGETED_ID = "PROMO_E2E_TARGETED"
PROMO_EXHAUSTED_ID = "PROMO_E2E_EXHAUSTED"
PROMO_EXHAUSTED_CODE = "E2E_EXHAUSTED1"

CUSTOMER_EMAIL_PREFIX = "e2e-"
CUSTOMER_EMAIL_DOMAIN = "bss-cli.local"


@dataclass(frozen=True)
class E2EPromos:
    """Resolved promo ids + codes for the current run."""

    public_id: str = PROMO_PUBLIC_ID
    public_code: str = PROMO_PUBLIC_CODE
    targeted_id: str = PROMO_TARGETED_ID
    exhausted_id: str = PROMO_EXHAUSTED_ID
    exhausted_code: str = PROMO_EXHAUSTED_CODE


# ─── env wiring ──────────────────────────────────────────────────────────────


def _required_env(var: str) -> str:
    v = os.environ.get(var)
    if not v:
        raise RuntimeError(
            f"{var} is required to seed e2e data. Run inside `make e2e` or "
            f"`source .env` first."
        )
    return v


def _catalog() -> CatalogClient:
    return CatalogClient(
        base_url=os.environ.get("BSS_CATALOG_URL", "http://localhost:8001"),
        auth_provider=TokenAuthProvider(_required_env("BSS_API_TOKEN")),
    )


def _crm() -> CRMClient:
    return CRMClient(
        base_url=os.environ.get("BSS_CRM_URL", "http://localhost:8002"),
        auth_provider=TokenAuthProvider(_required_env("BSS_API_TOKEN")),
    )


async def _connect_db() -> asyncpg.Connection:
    return await asyncpg.connect(
        _required_env("BSS_DB_URL").replace("postgresql+asyncpg://", "postgresql://")
    )


# ─── promo seed ──────────────────────────────────────────────────────────────


async def ensure_e2e_promos() -> E2EPromos:
    """Create the e2e promos if absent. Idempotent.

    * ``PROMO_E2E_PUBLIC`` — multi-use public code ``E2E_PUBLIC10``,
      10% off the first cycle. Drives the public-code-applied-at-signup
      spec.
    * ``PROMO_E2E_TARGETED`` — codeless targeted promo (code derived from
      id by catalog), 20% off single cycle. Spec-specific customers are
      assigned via ``CatalogClient.assign_promotion`` at test setup.
    * ``PROMO_E2E_EXHAUSTED`` — multi-use code ``E2E_EXHAUSTED1`` with no
      configured cap. The spec drives exhaustion by claiming the code once
      successfully then again — second order is expected to degrade to
      full price via the v1.1.3 graceful-degrade path. *(Pre-exhaustion at
      seed time would require either a primer signup, which is heavy, or
      direct loyalty DB manipulation, which violates the "write through
      policy" doctrine. The spec drives the lifecycle itself.)*

    Returns the populated dataclass so callers have stable handles.

    Without loyalty wiring, catalog refuses promo creation with
    ``catalog.promotion.loyalty_not_configured`` — that's a hard failure
    for the e2e suite (the promo specs cannot run BSS-only). The Makefile
    `e2e` target ensures the override stack is up + loyalty is reachable
    before invoking pytest.
    """
    catalog = _catalog()
    handles = E2EPromos()
    specs: list[dict[str, object]] = [
        {
            "promotion_id": handles.public_id,
            "display_name": "E2E Public 10%",
            "discount_type": "percent",
            "discount_value": "10",
            "duration_kind": "single",
            "audience": "public",
            "code": handles.public_code,
            "promo_code_kind": "multi_use",
        },
        {
            "promotion_id": handles.targeted_id,
            "display_name": "E2E Targeted 20%",
            "discount_type": "percent",
            "discount_value": "20",
            "duration_kind": "single",
            "audience": "targeted",
            # No `code` — catalog derives a unique-per-customer code from id.
        },
        {
            "promotion_id": handles.exhausted_id,
            "display_name": "E2E Exhausted (degrade-to-full-price spec)",
            "discount_type": "percent",
            "discount_value": "15",
            "duration_kind": "single",
            "audience": "public",
            "code": handles.exhausted_code,
            "promo_code_kind": "multi_use",
        },
    ]
    try:
        for spec in specs:
            try:
                await catalog.get_promotion(spec["promotion_id"])  # type: ignore[arg-type]
            except NotFound:
                await catalog.create_promotion(**spec)  # type: ignore[arg-type]
    finally:
        await catalog.close()
    return handles


# ─── msisdn picker (per-spec, avoids cross-run collisions) ──────────────────


async def pick_available_msisdn() -> str:
    """Return an MSISDN currently in ``available`` state.

    The signup funnel takes ``msisdn`` as a query param and SOM tries to
    reserve that exact number at order time. If a previous run left the
    same number assigned to a subscription, SOM hard-fails with
    ``No MSISDN available matching criteria`` and the order parks after
    5 retries. Dynamic picking sidesteps that — every spec gets a fresh
    available number from the pool.

    The pool is 1000 numbers per ``make seed``; even with a few hundred
    leftover assignments the pool stays deep. ``make demo-restore``
    refills it; ``make e2e``'s teardown doesn't actively recycle but
    each spec only takes one number, so a long stand of e2e runs would
    cap out around the 1000th run — at which point ``demo-restore`` is
    the relief valve.
    """
    conn = await _connect_db()
    try:
        row = await conn.fetchrow(
            """
            SELECT msisdn
            FROM inventory.msisdn_pool
            WHERE status = 'available'
              AND (quarantine_until IS NULL OR quarantine_until < now())
            ORDER BY msisdn
            LIMIT 1
            """
        )
        if row is None:
            raise RuntimeError(
                "no available MSISDN in inventory.msisdn_pool — run "
                "`make seed` or `make demo-restore` to repopulate."
            )
        return row["msisdn"]
    finally:
        await conn.close()


# ─── customer factory (per-spec mint) ────────────────────────────────────────


async def ensure_e2e_customer(email: str, *, name: str = "E2E Test") -> str:
    """Look up or create an ``e2e-*@bss-cli.local`` customer. Returns the
    BSS customer id (``CUST-<hex>``). Idempotent on email.

    Specs that need a customer_id BEFORE driving the portal (e.g. the
    targeted-promo spec, which must call ``assign_promotion`` upfront)
    use this helper. Specs that only need a customer to exist as a
    side-effect of the signup funnel ignore it.
    """
    if not email.startswith(CUSTOMER_EMAIL_PREFIX):
        raise ValueError(
            f"e2e helpers refuse to touch non-e2e-prefixed identities: {email}"
        )
    conn = await _connect_db()
    crm = _crm()
    try:
        existing = await conn.fetchval(
            """
            SELECT c.id
            FROM crm.customer c
            JOIN crm.contact_medium cm ON cm.party_id = c.party_id
            WHERE cm.medium_type = 'email' AND cm.value = $1
            LIMIT 1
            """,
            email,
        )
        if existing:
            return existing
        created = await crm.create_customer(name=name, email=email)
        return created["id"]
    finally:
        await crm.close()
        await conn.close()


# ─── teardown (e2e prefix only — never touches operator/demo data) ──────────


async def reset_e2e_data() -> dict[str, int]:
    """Surgical teardown — remove the three ``PROMO_E2E_*`` promos and
    their eligibility rows.

    **Deliberate non-deletion of everything else.** Specs that walk the
    signup funnel produce a fan-out of CRM + payment + subscription +
    service-inventory + order_mgmt + portal_auth rows tied together by
    foreign keys spanning seven schemas. A correct surgical drop would
    have to walk the whole graph in reverse-FK order — brittle,
    duplicative of what ``make demo-restore`` (full ``reset-db``) does
    cheaply via ``DROP SCHEMA … CASCADE``. The phase doc commits to
    that being the deep-clean path.

    ``reset_e2e_data`` is the in-Makefile cheap teardown: just the
    promo rows, so the next ``ensure_e2e_promos()`` re-mints them from
    scratch instead of finding the already-existing skeletons. Every
    other artefact (orphan ``e2e-*`` customers, subscriptions, orders,
    identities) is harmless — keyed under the e2e prefix, never touched
    by operator workflows, swept by ``make demo-restore`` whenever a
    clean slate is wanted.
    """
    conn = await _connect_db()
    summary = {
        "promotions_deleted": 0,
        "eligibility_rows_deleted": 0,
    }

    try:
        for pid in (PROMO_PUBLIC_ID, PROMO_TARGETED_ID, PROMO_EXHAUSTED_ID):
            elig = await conn.execute(
                "DELETE FROM catalog.promotion_eligibility WHERE promotion_id = $1",
                pid,
            )
            try:
                summary["eligibility_rows_deleted"] += int(elig.split()[-1])
            except (ValueError, IndexError):
                pass

            tag = await conn.execute(
                "DELETE FROM catalog.promotion WHERE id = $1", pid
            )
            if tag.endswith(" 1"):
                summary["promotions_deleted"] += 1
    finally:
        await conn.close()

    return summary
