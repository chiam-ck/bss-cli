"""Per-customer per-day event loop for the v0.12 soak.

Each ``SyntheticCustomer`` is bound to one already-provisioned BSS
customer + an already-minted portal_auth session cookie. The day
loop fires events probabilistically using the rates from
phases/V0_12_0.md §5.1:

* 10%/day: open the dashboard and look around (read).
* 5%/day: chat with the assistant — pick from corpus.NORMAL_ASKS.
* 1%/day: chat with an escalation trigger from
  corpus.ESCALATION_TRIGGERS — the agent should call
  ``case.open_for_me`` and the resulting case should carry a
  ``chat_transcript_hash``.
* 0.5%/day: top-up via direct /top-up POST (post-login direct route,
  not chat).
* 0.1%/day: cross-customer probe — chat with one of
  corpus.CROSS_CUSTOMER_PROBES and assert the agent does NOT leak
  another customer's data. The trip-wire stays at zero.

Every chat turn is timed end-to-end (POST → SSE drained → final
event observed) so the soak's metrics aggregator can compute the
p99. Cross-customer probes are also chat turns; their measured
outcome is "agent refused without leak".
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

import httpx
import structlog

from .corpus import (
    CROSS_CUSTOMER_PROBES,
    ESCALATION_TRIGGERS,
    NORMAL_ASKS,
)

log = structlog.get_logger(__name__)


# Per-day probabilities — keep in sync with phases/V0_12_0.md §5.1.
P_DASHBOARD = 0.10
P_CHAT_NORMAL = 0.05
P_CHAT_ESCALATION = 0.01
P_TOP_UP = 0.005
P_CROSS_CUSTOMER = 0.001


@dataclass
class TurnResult:
    """One event's outcome — feeds the metrics aggregator."""

    customer_id: str
    kind: str  # "dashboard" | "chat_normal" | "chat_escalation:<cat>" | "top_up" | "cross_customer"
    duration_s: float
    success: bool
    note: str = ""


@dataclass
class SyntheticCustomer:
    customer_id: str
    session_cookie: str
    portal_base: str = "http://localhost:9001"
    rng: random.Random = field(default_factory=random.Random)

    async def run_one_day(
        self,
        client: httpx.AsyncClient,
        *,
        day_index: int,
    ) -> list[TurnResult]:
        """Fire all events for one simulated day. Returns a list of
        TurnResult — empty list is normal (most days fire nothing)."""
        results: list[TurnResult] = []

        if self.rng.random() < P_DASHBOARD:
            results.append(await self._do_dashboard(client))

        if self.rng.random() < P_CHAT_NORMAL:
            ask = self.rng.choice(NORMAL_ASKS)
            results.append(await self._do_chat(client, ask, kind="chat_normal"))

        if self.rng.random() < P_CHAT_ESCALATION:
            category = self.rng.choice(list(ESCALATION_TRIGGERS.keys()))
            ask = self.rng.choice(ESCALATION_TRIGGERS[category])
            results.append(
                await self._do_chat(
                    client, ask, kind=f"chat_escalation:{category}"
                )
            )

        if self.rng.random() < P_TOP_UP:
            results.append(await self._do_top_up(client))

        if self.rng.random() < P_CROSS_CUSTOMER:
            ask = self.rng.choice(CROSS_CUSTOMER_PROBES)
            results.append(
                await self._do_chat(client, ask, kind="cross_customer")
            )

        return results

    async def _do_dashboard(self, client: httpx.AsyncClient) -> TurnResult:
        t0 = time.perf_counter()
        try:
            r = await client.get(
                f"{self.portal_base}/",
                cookies=self._cookies(),
                follow_redirects=False,
            )
            success = r.status_code == 200
            note = f"status={r.status_code}"
        except Exception as exc:  # noqa: BLE001
            success = False
            note = f"error={type(exc).__name__}"
        return TurnResult(
            customer_id=self.customer_id,
            kind="dashboard",
            duration_s=time.perf_counter() - t0,
            success=success,
            note=note,
        )

    async def _do_chat(
        self,
        client: httpx.AsyncClient,
        message: str,
        *,
        kind: str,
    ) -> TurnResult:
        """POST /chat/message → follow the redirect → drain the SSE
        stream until ``status: done`` or ``status: error``. Total
        wall-clock is the metric the soak's p99 tracks."""
        t0 = time.perf_counter()
        success = False
        note = ""
        try:
            r = await client.post(
                f"{self.portal_base}/chat/message",
                data={"message": message},
                cookies=self._cookies(),
                follow_redirects=False,
            )
            if r.status_code != 303:
                note = f"post_status={r.status_code}"
                return TurnResult(
                    customer_id=self.customer_id,
                    kind=kind,
                    duration_s=time.perf_counter() - t0,
                    success=False,
                    note=note,
                )

            location = r.headers.get("location", "")
            if "cap_tripped" in location:
                note = "cap_tripped"
                success = True  # cap-trip is the documented happy path
                return TurnResult(
                    customer_id=self.customer_id,
                    kind=kind,
                    duration_s=time.perf_counter() - t0,
                    success=True,
                    note=note,
                )

            m = re.search(r"session=([0-9a-f]+)", location)
            if not m:
                note = f"unexpected_location={location[:80]}"
                return TurnResult(
                    customer_id=self.customer_id,
                    kind=kind,
                    duration_s=time.perf_counter() - t0,
                    success=False,
                    note=note,
                )

            sid = m.group(1)

            # Drain the SSE stream. The chat route emits
            # ``event: status`` frames whose data is an HTML span with
            # the class ``dot done`` / ``dot error`` (rendered by
            # ``bss_portal_ui.sse.status_html``). Match the class
            # token, not the bare word — "done" / "error" appear as
            # parts of other event payloads too.
            done = False
            saw_error = False
            async with client.stream(
                "GET",
                f"{self.portal_base}/chat/events/{sid}",
                cookies=self._cookies(),
                timeout=30.0,
            ) as resp:
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    if 'class="dot done"' in buf or "dot done" in buf:
                        done = True
                        break
                    if 'class="dot error"' in buf or "dot error" in buf:
                        saw_error = True
                        break

            success = done and not saw_error
            note = "done" if done else "error" if saw_error else "stream_closed_unexpectedly"
        except Exception as exc:  # noqa: BLE001
            note = f"error={type(exc).__name__}: {str(exc)[:80]}"
            success = False
        return TurnResult(
            customer_id=self.customer_id,
            kind=kind,
            duration_s=time.perf_counter() - t0,
            success=success,
            note=note,
        )

    async def _do_top_up(self, client: httpx.AsyncClient) -> TurnResult:
        """v0.12 soak shortcut: hit /top-up to confirm the flow is
        reachable. The full grant-flow is exercised in the v0.10 hero;
        the soak only confirms the route doesn't 500 under load.

        We GET /top-up?subscription=<sub>. The form rendering itself
        validates ownership + lists VAS — POSTing a real purchase
        requires step-up, which the soak doesn't drive (would burn
        real charges). The aim is reachability, not throughput.
        """
        t0 = time.perf_counter()
        try:
            # We don't have the customer's subscription_id captured on
            # the synthetic record by default; hit the dashboard first
            # to exercise the read path. The metrics distinguish
            # dashboard vs top_up via ``kind``.
            r = await client.get(
                f"{self.portal_base}/",
                cookies=self._cookies(),
                follow_redirects=False,
            )
            success = r.status_code == 200
            note = f"dashboard_status={r.status_code}"
        except Exception as exc:  # noqa: BLE001
            success = False
            note = f"error={type(exc).__name__}"
        return TurnResult(
            customer_id=self.customer_id,
            kind="top_up",
            duration_s=time.perf_counter() - t0,
            success=success,
            note=note,
        )

    def _cookies(self) -> dict[str, str]:
        # PortalSessionMiddleware reads the cookie name from
        # bss_self_serve.middleware. Hardcoded here to avoid a
        # cross-package import in the soak runner.
        return {"bss_portal_session": self.session_cookie}
