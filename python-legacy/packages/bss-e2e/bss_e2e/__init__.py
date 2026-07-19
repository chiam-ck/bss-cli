"""bss-e2e — Playwright end-to-end suite for BSS-CLI (v1.4).

Two surfaces under one runner:

* **Self-serve portal** (``localhost:9001``) — magic-link login, KYC
  attestation, payment-method capture, plan signup, promo preview, dashboard.
* **Operator cockpit browser veneer** (``localhost:9002``) — sessions index,
  conversation view, propose-then-confirm flow, slash commands.

The suite assumes the stack is up in *mock-provider mode* via
``docker-compose.e2e.yml`` (payment=mock, kyc=prebaked, email=logging,
esim=sim). ``make e2e`` is the canonical entry point and handles bring-up,
seeding, teardown, and report archival.
"""
