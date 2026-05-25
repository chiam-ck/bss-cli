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
"""

from __future__ import annotations

from dataclasses import dataclass

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


async def ensure_e2e_promos() -> E2EPromos:
    """Create the three e2e promos if absent. Idempotent.

    * ``PROMO_E2E_PUBLIC`` — 5-use public code ``E2E_PUBLIC10``, 10% off
      the first cycle. Drives the public-code-applied-at-signup spec.
    * ``PROMO_E2E_TARGETED`` — codeless targeted promo, assigned per-spec
      via ``bss promo assign``. Drives the targeted-on-dashboard spec.
    * ``PROMO_E2E_EXHAUSTED`` — 1-use public code ``E2E_EXHAUSTED1``,
      pre-consumed in setup so the v1.1.3 degrade path triggers.

    Returns the populated dataclass so the caller has stable handles.

    .. note:: Implementation arrives with the spec that needs it
       (TDD-style — empty scaffolding here so ``pytest --collect-only``
       passes during v1.4.0 phase 1). The signature is the contract.
    """
    raise NotImplementedError(
        "ensure_e2e_promos() lands with test_promo_branches.py in v1.4.0 phase 2"
    )


async def reset_e2e_data() -> None:
    """Surgical teardown — remove ``PROMO_E2E_*`` + ``e2e-*@bss-cli.local``.

    Mirrors ``packages/bss-seed/bss_seed/demo.py::reset`` but scoped to
    the e2e prefix. Safe to run repeatedly; missing rows are no-ops.

    The Makefile target also runs ``make seed-demo-reset`` as a backstop
    in case a spec wrote artefacts outside the e2e prefix.

    .. note:: Implementation arrives with the spec that needs it.
    """
    raise NotImplementedError(
        "reset_e2e_data() lands with the first spec write in v1.4.0 phase 2"
    )
