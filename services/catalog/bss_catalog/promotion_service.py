"""Promotion service — the v1.1 create saga + reads (catalog side).

Catalog owns the *money terms* (the ``promotion`` row) and the link to
loyalty-cli, which owns the *entitlement* (OfferDefinition + codes/offers).
``create_promotion`` is a two-system saga; this service is the only place
that holds the ``LoyaltyClient``.

Saga ordering (BSS row first, loyalty next, BSS confirm last) makes a crash
harmless: a live code/offer does nothing until the promotion row is
``active``, so a half-failed saga never leaves a usable code pointing at
missing money terms. A retry with the same ``promotion_id`` resumes from
``pending_link`` — the loyalty calls carry ``Idempotency-Key=promotion_id``
so they replay rather than duplicate.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog
from bss_clients import LoyaltyClient, NotFound, PolicyViolationFromServer
from bss_clock import now as clock_now
from sqlalchemy.ext.asyncio import AsyncSession

from bss_catalog.policies import PolicyViolation
from bss_catalog.promotion_repository import PromotionRepository
from bss_catalog.repository import CatalogRepository
from bss_catalog.services import _check_admin
from bss_models import apply_discount, discount_label
from bss_models.catalog import Promotion

log = structlog.get_logger()

_DISCOUNT_TYPES = {"percent", "absolute"}
_DURATION_KINDS = {"single", "multi", "perpetual"}
# loyalty PromoCodeKind (verified against :8080/openapi.json).
_PROMO_CODE_KINDS = {
    "single_use_unique_per_customer",
    "single_use_shared",
    "multi_use",
}


def _offer_definition_id_for(promotion_id: str) -> str:
    """Deterministic loyalty OD id for a promotion. Deterministic so a saga
    retry re-registers the same OD (idempotent) and ``reconcile`` can relink.
    """
    return f"OD_{promotion_id}"


def _discount_periods_total(duration_kind: str, periods_total: int | None) -> int:
    """Number of periods the discount applies, as the subscription counter sees it.

    single = 1 (activation period only); multi = N; perpetual = -1 (sentinel,
    never decrements). The renewal loop decrements while > 0 and treats -1 as
    "always discounted".
    """
    if duration_kind == "perpetual":
        return -1
    if duration_kind == "multi":
        return periods_total or 0
    return 1  # single


class PromotionService:
    def __init__(
        self,
        session: AsyncSession,
        repo: PromotionRepository,
        loyalty: LoyaltyClient,
        actor: str,
    ) -> None:
        self._session = session
        self._repo = repo
        self._loyalty = loyalty
        self._actor = actor

    # ── reads ────────────────────────────────────────────────────────────

    async def get(self, promotion_id: str) -> Promotion | None:
        return await self._repo.get(promotion_id)

    async def get_by_offer_definition_id(self, offer_definition_id: str) -> Promotion | None:
        return await self._repo.get_by_offer_definition_id(offer_definition_id)

    async def list_promotions(
        self, *, state: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Promotion]:
        return await self._repo.list(state=state, limit=limit, offset=offset)

    async def validate_for_order(self, *, code: str, offering_id: str) -> dict:
        """Resolve a typed code against an offering and compose the effective price.

        Pure read — never consumes the code (loyalty is the hard gate at claim,
        Flow 2). Returns ``{valid, reason, offer_definition_id, discount_*,
        duration_kind, periods_total, base, effective, label}``. ``valid=False``
        with a ``reason`` instead of raising, so the portal can show an inline
        error and the order proceeds at full price.
        """
        result: dict = {
            "valid": False,
            "code": code,
            "offering_id": offering_id,
            "reason": None,
            "offer_definition_id": None,
            "discount_type": None,
            "discount_value": None,
            "duration_kind": None,
            "periods_total": None,
            "discount_periods_total": None,
            "base": None,
            "effective": None,
            "label": None,
        }

        # 1. resolve code → OfferDefinition (loyalty read; no consume)
        try:
            shown = await self._loyalty.show_promo_code(code)
        except NotFound:
            result["reason"] = "unknown_code"
            return result
        except PolicyViolationFromServer as exc:
            result["reason"] = exc.rule
            return result
        od_id = shown.get("offer_definition_id")
        if not od_id:
            result["reason"] = "unlinked_code"
            return result

        # 2. money terms by OD
        promo = await self._repo.get_by_offer_definition_id(od_id)
        if promo is None or promo.state != "active":
            result["reason"] = "no_active_promotion"
            return result

        # 3-5. applicability + window + compose (shared with assigned-offer path)
        composed = await self._compose(promo, offering_id)
        if composed.get("reason"):
            result["reason"] = composed["reason"]
            return result
        result.update(valid=True, offer_definition_id=od_id, **composed["terms"])
        return result

    async def _compose(self, promo: Promotion, offering_id: str) -> dict:
        """Apply a promotion's discount to an offering's lowest-active base.

        Returns ``{"reason": <str>}`` when the promo can't apply (not applicable
        to the offering, outside its window, or the offering has no active
        price), else ``{"terms": {...}}`` with the composed money + display terms.
        Shared by the typed-code (validate_for_order) and assigned-offer paths.
        """
        if promo.applicable_offering_ids and offering_id not in promo.applicable_offering_ids:
            return {"reason": "not_applicable_to_offering"}
        now = clock_now()
        if promo.valid_from and now < promo.valid_from:
            return {"reason": "not_yet_valid"}
        if promo.valid_to and now >= promo.valid_to:
            return {"reason": "expired"}
        try:
            price = await CatalogRepository(self._session).get_active_price(offering_id, at=now)
        except PolicyViolation:
            return {"reason": "offering_not_priced"}
        base = Decimal(price.amount)
        return {
            "terms": {
                "discount_type": promo.discount_type,
                "discount_value": promo.discount_value,
                "duration_kind": promo.duration_kind,
                "periods_total": promo.periods_total,
                # Discounted-period count the subscription counter starts at:
                # single = 1 (activation only), multi = N, perpetual = -1 sentinel.
                "discount_periods_total": _discount_periods_total(
                    promo.duration_kind, promo.periods_total
                ),
                "base": base,
                "effective": apply_discount(promo.discount_type, promo.discount_value, base),
                "label": discount_label(
                    promo.discount_type, promo.discount_value, promo.currency
                ),
            }
        }

    async def resolve_assigned_offer(self, *, customer_id: str, offering_id: str) -> dict:
        """Targeted path (Flow 3): pick the best applicable assigned offer.

        Scans the customer's ``issued``/``claimed`` loyalty offers, keeps those
        whose promotion is active + applicable to ``offering_id`` + in-window,
        and returns the one with the lowest effective price (most discount). No
        consume — the entitlement is advanced/redeemed later at activation.
        ``{valid: False, reason: "no_applicable_offer"}`` when none match.
        """
        rows: list[dict] = []
        for state in ("issued", "claimed"):
            resp = await self._loyalty.list_offers(customer_id=customer_id, state=state)
            rows.extend(resp.get("rows", []))

        best: dict | None = None
        for row in rows:
            od_id = row.get("offer_definition_id")
            if not od_id:
                continue
            promo = await self._repo.get_by_offer_definition_id(od_id)
            if promo is None or promo.state != "active":
                continue
            composed = await self._compose(promo, offering_id)
            if composed.get("reason"):
                continue
            candidate = {
                "valid": True,
                "offer_id": row.get("offer_id"),
                "offer_state": row.get("state"),
                "offer_definition_id": od_id,
                **composed["terms"],
            }
            if best is None or candidate["effective"] < best["effective"]:
                best = candidate
        return best or {"valid": False, "reason": "no_applicable_offer"}

    async def preview_promo(self, *, code: str, offering_id: str) -> dict:
        """Portal-facing live preview — the display subset of validate_for_order."""
        r = await self.validate_for_order(code=code, offering_id=offering_id)
        return {
            "valid": r["valid"],
            "code": code,
            "offering_id": offering_id,
            "label": r["label"],
            "base": r["base"],
            "effective": r["effective"],
            "reason": r["reason"],
        }

    async def list_customer_offers(
        self, *, customer_id: str, state: str | None = None
    ) -> list[dict]:
        """Read-proxy over loyalty ``offer.list`` enriched with BSS money terms.

        Powers the account-dashboard "🎁 you have an offer" block. loyalty is the
        assignment ledger; BSS attaches the discount label per offer's OD.
        """
        resp = await self._loyalty.list_offers(customer_id=customer_id, state=state)
        out: list[dict] = []
        for row in resp.get("rows", []):
            od_id = row.get("offer_definition_id")
            promo = (
                await self._repo.get_by_offer_definition_id(od_id) if od_id else None
            )
            entry: dict = {
                "offer_id": row.get("offer_id"),
                "state": row.get("state"),
                "offer_definition_id": od_id,
                "promotion": None,
            }
            if promo is not None:
                entry["promotion"] = {
                    "promotion_id": promo.id,
                    "discount_type": promo.discount_type,
                    "discount_value": str(promo.discount_value),
                    "duration_kind": promo.duration_kind,
                    "periods_total": promo.periods_total,
                    "label": discount_label(
                        promo.discount_type, promo.discount_value, promo.currency
                    ),
                }
            out.append(entry)
        return out

    # ── targeted assignment (the "simulator" loop) ─────────────────────────

    async def assign_targeted(
        self,
        *,
        promotion_id: str,
        customer_ids: list[str],
        source: dict | None = None,
    ) -> dict:
        """Issue a codeless offer to each chosen customer (Flow 3).

        loyalty has no bulk tool — assignment is a per-customer ``offer.issue``
        loop, and *this loop is the simulator*. Each offer id is deterministic
        (``OFF_<promotion>_<customer>``) and doubles as the idempotency key, so
        re-running the assignment is safe. A customer who already has the offer
        is reported under ``skipped`` rather than failing the whole batch.
        """
        _check_admin(self._actor)
        promo = await self._repo.get(promotion_id)
        if promo is None or promo.state != "active" or not promo.offer_definition_id:
            raise PolicyViolation(
                rule="catalog.promotion.not_active",
                message=f"Promotion {promotion_id} is not active/linked; cannot assign",
                context={
                    "promotion_id": promotion_id,
                    "state": promo.state if promo else None,
                },
            )
        src = source or {"type": "gift", "issued_by": self._actor}

        issued: list[str] = []
        skipped: list[dict] = []
        for customer_id in customer_ids:
            offer_id = f"OFF_{promotion_id}_{customer_id}"
            try:
                await self._loyalty.issue_offer(
                    offer_id=offer_id,
                    offer_definition_id=promo.offer_definition_id,
                    customer_id=customer_id,
                    source=src,
                    idempotency_key=offer_id,
                )
                issued.append(customer_id)
            except PolicyViolationFromServer as exc:
                skipped.append({"customer_id": customer_id, "reason": exc.rule})

        log.info(
            "catalog.promotion.assigned",
            promotion_id=promotion_id,
            issued=len(issued),
            skipped=len(skipped),
            actor=self._actor,
        )
        return {
            "promotion_id": promotion_id,
            "offer_definition_id": promo.offer_definition_id,
            "issued": issued,
            "skipped": skipped,
        }

    # ── create saga ────────────────────────────────────────────────────────

    async def create_promotion(
        self,
        *,
        promotion_id: str,
        discount_type: str,
        discount_value: Decimal,
        duration_kind: str,
        currency: str = "SGD",
        code: str | None = None,
        promo_code_kind: str | None = None,
        applicable_offering_ids: list[str] | None = None,
        periods_total: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        display_name: str | None = None,
    ) -> Promotion:
        """Create money terms + register the loyalty entitlement (two-system saga).

        ``code`` set = non-targeted (a typed, shared/multi-use code);
        ``code`` None = codeless targeted promo (assigned later via offer.issue).
        """
        _check_admin(self._actor)
        self._validate(
            discount_type=discount_type,
            discount_value=discount_value,
            duration_kind=duration_kind,
            periods_total=periods_total,
            code=code,
            promo_code_kind=promo_code_kind,
        )

        existing = await self._repo.get(promotion_id)
        if existing is not None and existing.state != "pending_link":
            raise PolicyViolation(
                rule="catalog.promotion.already_exists",
                message=f"Promotion {promotion_id} already exists (state={existing.state})",
                context={"promotion_id": promotion_id, "state": existing.state},
            )
        if existing is None and code is not None:
            clash = await self._repo.get_by_code(code)
            if clash is not None:
                raise PolicyViolation(
                    rule="catalog.promotion.code_in_use",
                    message=f"Promo code {code} is already bound to promotion {clash.id}",
                    context={"code": code, "promotion_id": clash.id},
                )

        # ── step 1: write (or resume) the pending_link row ──────────────
        if existing is None:
            promo = Promotion(
                id=promotion_id,
                code=code,
                offer_definition_id=None,
                discount_type=discount_type,
                discount_value=discount_value,
                currency=currency,
                applicable_offering_ids=applicable_offering_ids,
                duration_kind=duration_kind,
                periods_total=periods_total,
                valid_from=valid_from,
                valid_to=valid_to,
                state="pending_link",
                created_by=self._actor,
            )
            self._session.add(promo)
            await self._session.commit()
            log.info("catalog.promotion.pending", promotion_id=promotion_id, actor=self._actor)
        else:
            promo = existing  # resume a half-finished saga

        # ── steps 2-3: register the loyalty entitlement ─────────────────
        od_id = _offer_definition_id_for(promotion_id)
        try:
            await self._loyalty.register_offer_definition(
                definition_id=od_id,
                display_name=display_name or promotion_id,
                idempotency_key=promotion_id,
            )
            if code is not None:
                await self._loyalty.register_promo_code(
                    code=code,
                    offer_definition_id=od_id,
                    kind=promo_code_kind,
                    idempotency_key=promotion_id,
                )
        except PolicyViolationFromServer as exc:
            # Leave the row pending_link (harmless — no live entitlement points
            # at it yet) and surface as a catalog policy violation so the
            # middleware renders the standard 422 envelope.
            raise PolicyViolation(
                rule="catalog.promotion.loyalty_refused",
                message=f"loyalty refused: {exc.detail}",
                context={"promotion_id": promotion_id, "loyalty_rule": exc.rule},
            ) from exc

        # ── step 4: confirm the link ────────────────────────────────────
        promo.offer_definition_id = od_id
        promo.state = "active"
        await self._session.commit()
        log.info(
            "catalog.promotion.created",
            promotion_id=promotion_id,
            offer_definition_id=od_id,
            code=code,
            actor=self._actor,
        )
        await self._session.refresh(promo)
        return promo

    # ── validation ───────────────────────────────────────────────────────

    @staticmethod
    def _validate(
        *,
        discount_type: str,
        discount_value: Decimal,
        duration_kind: str,
        periods_total: int | None,
        code: str | None,
        promo_code_kind: str | None,
    ) -> None:
        if discount_type not in _DISCOUNT_TYPES:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_discount_type",
                message=f"discount_type must be one of {sorted(_DISCOUNT_TYPES)}",
                context={"discount_type": discount_type},
            )
        if discount_value <= 0:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_discount_value",
                message="discount_value must be positive",
                context={"discount_value": str(discount_value)},
            )
        if discount_type == "percent" and discount_value > 100:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_discount_value",
                message="percent discount cannot exceed 100",
                context={"discount_value": str(discount_value)},
            )
        if duration_kind not in _DURATION_KINDS:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_duration_kind",
                message=f"duration_kind must be one of {sorted(_DURATION_KINDS)}",
                context={"duration_kind": duration_kind},
            )
        if duration_kind == "multi":
            if periods_total is None or periods_total < 2:
                raise PolicyViolation(
                    rule="catalog.promotion.invalid_periods_total",
                    message="multi-period promo requires periods_total >= 2",
                    context={"periods_total": periods_total},
                )
        elif periods_total is not None:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_periods_total",
                message=f"{duration_kind} promo must not set periods_total",
                context={"duration_kind": duration_kind, "periods_total": periods_total},
            )
        if code is not None and promo_code_kind not in _PROMO_CODE_KINDS:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_promo_code_kind",
                message=f"a coded promo requires promo_code_kind in {sorted(_PROMO_CODE_KINDS)}",
                context={"promo_code_kind": promo_code_kind},
            )
