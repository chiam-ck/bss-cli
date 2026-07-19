#!/usr/bin/env python3
"""Backfill: register every existing BSS customer into loyalty (v1.1.1).

New customers are mirrored into loyalty's registry at create time (CRM →
loyalty `customer.register`). This one-shot reconciles the customers that
existed before that sync — and any that drifted because a create-time register
failed (it's best-effort). Idempotent: re-registering an existing loyalty
customer is a safe replay.

Runs from the host against the published ports.

Usage:
    BSS_API_TOKEN=... BSS_LOYALTY_API_TOKEN=... python backfill_loyalty_customers.py

Env:
    BSS_API_TOKEN          perimeter token for CRM (required)
    BSS_LOYALTY_API_TOKEN  loyalty bearer token (required)
    BSS_CRM_URL            default http://localhost:8002
    BSS_LOYALTY_BASE_URL   default http://localhost:8080
"""

from __future__ import annotations

import asyncio
import os
import sys

from bss_clients import (
    BearerAuthProvider,
    ClientError,
    CRMClient,
    LoyaltyClient,
    TokenAuthProvider,
    set_context,
)


async def run() -> int:
    api_token = os.environ.get("BSS_API_TOKEN", "")
    loyalty_token = os.environ.get("BSS_LOYALTY_API_TOKEN", "")
    if not api_token or not loyalty_token:
        print("ERROR: BSS_API_TOKEN and BSS_LOYALTY_API_TOKEN must be set", file=sys.stderr)
        return 2

    crm = CRMClient(
        base_url=os.environ.get("BSS_CRM_URL", "http://localhost:8002"),
        auth_provider=TokenAuthProvider(api_token),
    )
    loyalty = LoyaltyClient(
        base_url=os.environ.get("BSS_LOYALTY_BASE_URL", "http://localhost:8080"),
        auth_provider=BearerAuthProvider(loyalty_token),
    )
    set_context(actor="backfill-loyalty-customers", channel="seed", request_id="")

    registered = failed = 0
    page = 200
    offset = 0
    try:
        while True:
            customers = await crm.list_customers(limit=page, offset=offset)
            if not customers:
                break
            for c in customers:
                cid = c.get("id")
                if not cid:
                    continue
                try:
                    await loyalty.register_customer(customer_id=cid)
                    registered += 1
                except ClientError as exc:
                    failed += 1
                    print(f"  · failed {cid}: {exc.detail}", file=sys.stderr)
            if len(customers) < page:
                break
            offset += page
        print(f"✓ done — {registered} registered, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        await crm.close()
        await loyalty.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
