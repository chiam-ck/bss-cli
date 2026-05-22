#!/usr/bin/env python3
"""Seed a targeted (codeless) promo campaign — repeatable demo data (v1.1).

The "simulator" the v1.1 plan calls for: create one targeted promotion, then
pre-assign it to a chosen set of customers via the catalog promo surface
(which composes over loyalty-cli). Each assigned offer auto-applies at the
customer's next order and shows on their dashboard — no typed code.

Runs from the host against the published service ports (not inside a
container). Idempotent: a re-run reuses the existing promotion and re-issues
to any customers who don't already hold the offer (loyalty reports them as
skipped). Mirrors the bss-seed posture — deterministic, re-runnable demo data.

Usage:
    BSS_API_TOKEN=... python seed_targeted_campaign.py
    python seed_targeted_campaign.py --promo PROMO_VIP_DEMO --count 5
    python seed_targeted_campaign.py --customers CUST-001,CUST-007

Env:
    BSS_API_TOKEN     perimeter token (required)
    BSS_CATALOG_URL   default http://localhost:8001
    BSS_CRM_URL       default http://localhost:8002
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from bss_clients import (
    CatalogClient,
    CRMClient,
    PolicyViolationFromServer,
    TokenAuthProvider,
    set_context,
)

_DEMO_CUSTOMERS = [
    ("Ada Lovelace", "ada.vip@example.sg"),
    ("Grace Hopper", "grace.vip@example.sg"),
    ("Alan Turing", "alan.vip@example.sg"),
]


async def _ensure_customers(crm: CRMClient, count: int) -> list[str]:
    """Return ``count`` customer ids, creating demo customers if too few exist."""
    existing = await crm.list_customers()
    ids = [c["id"] for c in existing][:count]
    i = 0
    while len(ids) < count and i < len(_DEMO_CUSTOMERS):
        name, email = _DEMO_CUSTOMERS[i]
        i += 1
        try:
            created = await crm.create_customer(name=name, email=email)
            ids.append(created["id"])
        except PolicyViolationFromServer as exc:
            print(f"  · skip create {email}: {exc.rule}", file=sys.stderr)
    return ids


async def _ensure_promotion(catalog: CatalogClient, promotion_id: str) -> None:
    """Create the targeted (codeless) promo, tolerating an existing one."""
    try:
        promo = await catalog.create_promotion(
            promotion_id=promotion_id,
            discount_type="percent",
            discount_value="20",
            duration_kind="single",
            audience="targeted",  # v1.1.1 — eligibility-gated code, not codeless
            display_name="VIP Welcome",  # friendly name shown to customers
        )
        print(f"✓ created promotion {promo['id']} (OD={promo.get('offerDefinitionId')})")
    except PolicyViolationFromServer as exc:
        if exc.rule == "catalog.promotion.already_exists":
            print(f"· promotion {promotion_id} already exists — reusing")
        else:
            raise


async def run(promotion_id: str, customer_ids: list[str], count: int) -> int:
    token = os.environ.get("BSS_API_TOKEN", "")
    if not token:
        print("ERROR: BSS_API_TOKEN is not set", file=sys.stderr)
        return 2

    auth = TokenAuthProvider(token)
    catalog = CatalogClient(
        base_url=os.environ.get("BSS_CATALOG_URL", "http://localhost:8001"),
        auth_provider=auth,
    )
    crm = CRMClient(
        base_url=os.environ.get("BSS_CRM_URL", "http://localhost:8002"),
        auth_provider=auth,
    )
    # Stamp an operator actor so the catalog admin gate + audit trail are clean.
    set_context(actor="seed-targeted-campaign", channel="seed", request_id="")

    try:
        await _ensure_promotion(catalog, promotion_id)

        targets = customer_ids or await _ensure_customers(crm, count)
        if not targets:
            print("ERROR: no customers to target", file=sys.stderr)
            return 1

        result = await catalog.assign_promotion(promotion_id, customer_ids=targets)
        eligible = result.get("eligible", [])
        already = result.get("already", [])
        print(
            f"✓ eligibility for {promotion_id}: {len(eligible)} added, {len(already)} already"
        )
        for cid in eligible:
            print(f"  · added   {cid}")
        for cid in already:
            print(f"  · already {cid}")
        return 0
    finally:
        await catalog.close()
        await crm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promo", default="PROMO_VIP_DEMO", help="Promotion id.")
    parser.add_argument(
        "--customers",
        default="",
        help="Comma-separated customer ids. Omit to auto-discover/create.",
    )
    parser.add_argument(
        "--count", type=int, default=3, help="How many customers to target when auto-discovering."
    )
    args = parser.parse_args()
    customer_ids = [c.strip() for c in args.customers.split(",") if c.strip()]
    raise SystemExit(asyncio.run(run(args.promo, customer_ids, args.count)))


if __name__ == "__main__":
    main()
