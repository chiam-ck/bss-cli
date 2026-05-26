"""Per-customer chat caps — hourly rate + monthly cost (v0.12 PR5).

Two caps, two storage shapes:

* **Hourly rate cap** — in-memory sliding window of recent request
  timestamps per customer. Single-process; abstracted behind
  ``_HourlyWindow`` so v1.x can swap to Redis if scale demands.

* **Monthly cost cap** — DB-backed via ``audit.chat_usage``. One row
  per (customer_id, period_yyyymm); cost rolled up from the
  OpenRouter response's token counts × per-model rate.

The orchestrator process holds the only DB connection here — no
domain service owns ``audit.chat_usage`` because the table aggregates
costs across CRM / subscription / payment writes that the chat
surface drives. The connection is lazy (created on first use) and
scoped to two SQL operations: SELECT cost_cents and the atomic
INSERT … ON CONFLICT DO UPDATE.

Doctrine (per phases/V0_12_0.md trap section): **fail closed**. If
``check_caps`` raises (DB unreachable, etc.), the chat route must
refuse to call astream_once — a cap that doesn't enforce is worse
than no cap.

Cost accounting derives from token counts × ``MODEL_RATES_USD_PER_M_TOK``.
OpenRouter's response body does not surface a cost-cents number
directly; tokens × rate is the deterministic path. v0.10.0–v1.5
shipped ``google/gemma-4-26b-a4b-it``; v1.5.1+ defaults to
``deepseek/deepseek-v4-pro``. Add new models to the rate table when
swapping; unknown models fall back to a conservative ceiling so cap
enforcement stays honest (see ``_FALLBACK_RATE``).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import settings

log = structlog.get_logger(__name__)


# ─── Per-model OpenRouter rates ──────────────────────────────────────


# Sourced from openrouter.ai/models page, in USD per 1M tokens
# (input, output). Approximate; updated when the headline model swaps.
# Rate accuracy is OK for cap enforcement — the goal is to bound a
# runaway customer at ~$2/month, not to bill perfectly.
MODEL_RATES_USD_PER_M_TOK: dict[str, tuple[float, float]] = {
    "google/gemma-4-26b-a4b-it": (0.15, 0.50),
    # v1.5.1 — placeholder rates for the new default; verify against
    # openrouter.ai/models headline pricing on next billing review.
    # Slightly-too-high is the safe direction (cap enforcement, not
    # billing accuracy).
    "deepseek/deepseek-v4-pro": (1.00, 4.00),
}


# v1.5.1 — conservative fallback used when (a) the request's model isn't
# in MODEL_RATES_USD_PER_M_TOK AND (b) the configured ``settings.llm_model``
# isn't either. Pre-v1.5.1, the fallback recursively looked up
# ``settings.llm_model`` and raised KeyError when that was also unknown,
# which broke customer-chat cap accounting on any unconfigured swap.
_FALLBACK_RATE: tuple[float, float] = (2.00, 8.00)


def _cost_cents_for_turn(
    *, model: str, prompt_tok: int, completion_tok: int
) -> int:
    """Convert token counts to integer cents. Unknown model → falls
    back to the configured headline model's rate so we never under-
    count; logs a warning so the rate table can be updated. If even
    the configured headline isn't in the table, falls back to the
    conservative ``_FALLBACK_RATE`` ceiling — caps still enforce
    correctly, just with extra headroom for the operator to notice
    the missing entry."""
    rates = MODEL_RATES_USD_PER_M_TOK.get(model)
    if rates is None:
        log.warning("chat_caps.unknown_model", model=model)
        rates = MODEL_RATES_USD_PER_M_TOK.get(
            settings.llm_model, _FALLBACK_RATE
        )
        if rates is _FALLBACK_RATE:
            log.warning(
                "chat_caps.configured_model_also_unknown",
                configured_model=settings.llm_model,
                using_fallback_rate=_FALLBACK_RATE,
            )
    in_rate, out_rate = rates
    usd = (prompt_tok / 1_000_000) * in_rate + (completion_tok / 1_000_000) * out_rate
    # Round up so partial cents always count toward the cap.
    return max(0, int(usd * 100 + 0.999))


# ─── CapStatus ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapStatus:
    """Result of ``check_caps``. The chat route inspects ``allowed``
    first; on False it consumes ``reason`` (one of
    ``"hourly_rate_cap"`` / ``"monthly_cost_cap"``) and ``retry_at``
    (when the customer can try again).
    """

    allowed: bool
    reason: Optional[str] = None
    retry_at: Optional[datetime] = None


# ─── Hourly in-memory sliding window ─────────────────────────────────


class _HourlyWindow:
    """Single-process sliding-window counter keyed by customer_id.

    asyncio-safe: a single asyncio.Lock guards mutations. Single-
    process only — v1.x replaces with a memcached/Redis abstraction
    when horizontal scale demands.
    """

    def __init__(self, window: timedelta = timedelta(hours=1)) -> None:
        self._window = window
        self._buckets: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def record(self, key: str, when: datetime) -> int:
        async with self._lock:
            bucket = self._buckets[key]
            bucket.append(when)
            self._prune(bucket, when)
            return len(bucket)

    async def count(self, key: str, *, now: datetime) -> int:
        async with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                return 0
            self._prune(bucket, now)
            return len(bucket)

    def _prune(self, bucket: deque[datetime], now: datetime) -> None:
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


_per_customer = _HourlyWindow(timedelta(hours=1))
_per_ip = _HourlyWindow(timedelta(hours=1))


# ─── Lazy async engine for audit.chat_usage ──────────────────────────


_engine: AsyncEngine | None = None
_engine_lock = asyncio.Lock()


async def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        if not settings.db_url:
            raise RuntimeError(
                "chat_caps requires BSS_DB_URL to be set — the orchestrator "
                "writes audit.chat_usage rows directly."
            )
        _engine = create_async_engine(
            settings.db_url, pool_size=2, max_overflow=2, pool_pre_ping=True
        )
        return _engine


async def _close_engine() -> None:
    """Test hook — disposes the cached engine so a fresh one is
    constructed on the next call. Production callers don't need to
    invoke this."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def _period_yyyymm(when: datetime) -> int:
    return when.year * 100 + when.month


async def _read_month_cost_cents(customer_id: str, period: int) -> int:
    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT cost_cents FROM audit.chat_usage "
                    "WHERE customer_id = :cid AND period_yyyymm = :p"
                ),
                {"cid": customer_id, "p": period},
            )
        ).first()
        return int(row[0]) if row is not None else 0


# ─── Public API ──────────────────────────────────────────────────────


async def check_caps(customer_id: str, *, now: datetime | None = None) -> CapStatus:
    """Return ``CapStatus(allowed=...)``. Fail closed on any error.

    Hourly rate cap is checked first (cheap, in-memory). If it
    passes, the monthly cost cap is checked against the DB row.

    Args:
        customer_id: ``CUST-NNN``.
        now: clock override for tests; defaults to ``bss_clock.now()``.

    Returns:
        ``CapStatus(allowed=True)`` when both caps are under their
        limits. Otherwise ``CapStatus(allowed=False, reason=<...>,
        retry_at=<...>)``.

    Raises:
        Never — exceptions are caught and converted to
        ``CapStatus(allowed=False, reason="cap_check_failed")`` so
        the chat route always refuses on uncertainty (fail closed).
    """
    try:
        from bss_clock import now as clock_now

        now = now or clock_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        per_customer = await _per_customer.count(customer_id, now=now)
        if per_customer >= settings.chat_rate_per_customer_per_hour:
            return CapStatus(
                allowed=False,
                reason="hourly_rate_cap",
                retry_at=now + timedelta(hours=1),
            )

        period = _period_yyyymm(now)
        cost_cents = await _read_month_cost_cents(customer_id, period)
        if cost_cents >= settings.chat_cost_cap_per_customer_per_month_cents:
            # Retry at the start of next month.
            if now.month == 12:
                next_period = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_period = now.replace(month=now.month + 1, day=1)
            next_period = next_period.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return CapStatus(
                allowed=False,
                reason="monthly_cost_cap",
                retry_at=next_period,
            )

        return CapStatus(allowed=True)
    except Exception as exc:  # noqa: BLE001 — fail closed
        log.error(
            "chat_caps.check_failed",
            customer_id=customer_id,
            error=str(exc),
        )
        return CapStatus(allowed=False, reason="cap_check_failed")


async def record_chat_turn(
    *,
    customer_id: str,
    prompt_tok: int,
    completion_tok: int,
    model: str | None = None,
    now: datetime | None = None,
) -> None:
    """Increment the customer's hourly counter and monthly cost row.

    Two writes:
    1. ``_per_customer.record`` — appends ``now`` to the in-memory
       sliding window so subsequent ``check_caps`` calls see it.
    2. ``audit.chat_usage`` — INSERT … ON CONFLICT DO UPDATE atomic
       upsert keyed on (customer_id, period_yyyymm).

    Errors are logged and swallowed: a missed accounting row is
    cheaper than a chat that errors after the LLM already responded.
    The hourly window is in-memory and cannot fail.

    Args:
        customer_id: ``CUST-NNN``.
        prompt_tok: prompt tokens consumed.
        completion_tok: completion tokens emitted.
        model: model identifier used for this turn. Defaults to
            ``settings.llm_model``.
        now: clock override for tests.
    """
    from bss_clock import now as clock_now

    now = now or clock_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cost_cents = _cost_cents_for_turn(
        model=model or settings.llm_model,
        prompt_tok=prompt_tok,
        completion_tok=completion_tok,
    )
    period = _period_yyyymm(now)

    await _per_customer.record(customer_id, now)

    try:
        engine = await _get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO audit.chat_usage
                        (customer_id, period_yyyymm, requests_count,
                         cost_cents, last_updated)
                    VALUES (:cid, :p, 1, :cost, :now)
                    ON CONFLICT (customer_id, period_yyyymm) DO UPDATE
                        SET requests_count = audit.chat_usage.requests_count + 1,
                            cost_cents = audit.chat_usage.cost_cents + :cost,
                            last_updated = :now
                    """
                ),
                {"cid": customer_id, "p": period, "cost": cost_cents, "now": now},
            )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "chat_caps.record_failed",
            customer_id=customer_id,
            cost_cents=cost_cents,
            period=period,
            error=str(exc),
        )


async def record_ip_request(ip: str, *, now: datetime | None = None) -> bool:
    """Loose per-IP rate cap. Returns True when the IP is still
    under its hourly ceiling, False when tripped.

    The per-customer cap is the real gate; this exists so a
    pre-login attacker (or a customer hopping between accounts)
    can't burn the monthly cost cap on every account by spamming
    requests.
    """
    from bss_clock import now as clock_now

    now = now or clock_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    count = await _per_ip.record(ip, now)
    return count <= settings.chat_rate_per_ip_per_hour
