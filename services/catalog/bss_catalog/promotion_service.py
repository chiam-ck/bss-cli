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
from bss_clients import ClientError, LoyaltyClient, NotFound, PolicyViolationFromServer
from bss_clock import now as clock_now
from bss_models import apply_discount, discount_label
from bss_models.catalog import Promotion
from sqlalchemy.ext.asyncio import AsyncSession

from bss_catalog.policies import PolicyViolation
from bss_catalog.promotion_repository import PromotionRepository
from bss_catalog.repository import CatalogRepository
from bss_catalog.services import _check_admin

log = structlog.get_logger()

_DISCOUNT_TYPES = {"percent", "absolute"}
_DURATION_KINDS = {"single", "multi", "perpetual"}
_AUDIENCES = {"public", "targeted"}
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

    async def validate_for_order(
        self, *, code: str, offering_id: str, customer_id: str | None = None
    ) -> dict:
        """Resolve a typed code against an offering and compose the effective price.

        Pure read — never consumes the code (loyalty is the hard gate at claim).
        A **targeted** code is gated on eligibility: it's only valid for a
        customer with a ``promotion_eligibility`` row (so an unadvertised code
        leaking doesn't let just anyone redeem it). Returns ``{valid, reason,
        ...terms}``; ``valid=False`` with a ``reason`` rather than raising, so the
        portal shows an inline note and the order proceeds at full price.
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
            "name": None,
        }

        # loyalty disabled → no promos exist; order proceeds at full price.
        if self._loyalty is None:
            result["reason"] = "loyalty_not_configured"
            return result

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

        # 3. targeted codes are eligibility-gated (BSS is the gate; loyalty has
        # no per-customer binding). Public codes skip this.
        # v1.3.0 — when eligible, also pull the upfront-minted loyalty offer id
        # so COM can advance_to_claimed at activation instead of mint-and-claim.
        loyalty_offer_id: str | None = None
        if promo.audience == "targeted":
            if customer_id is None or not await self._repo.is_eligible(promo.id, customer_id):
                result["reason"] = "not_eligible"
                return result
            loyalty_offer_id = await self._repo.get_loyalty_offer_id(
                promotion_id=promo.id, customer_id=customer_id
            )

        # 4-6. applicability + window + compose
        composed = await self._compose(promo, offering_id)
        if composed.get("reason"):
            result["reason"] = composed["reason"]
            return result
        result.update(
            valid=True,
            offer_definition_id=od_id,
            loyalty_offer_id=loyalty_offer_id,
            **composed["terms"],
        )
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
                "name": promo.name,  # friendly label (None → UI uses label)
            }
        }

    async def _consumed_offer_definitions(self, customer_id: str) -> set[str]:
        """OfferDefinition ids the customer has already claimed/redeemed in loyalty.

        Used to drop already-used promos from the dashboard + auto-apply: a
        targeted code is single-use-per-customer by default, so once consumed it
        must not show as available again. (For a multi_use targeted promo this is
        conservative — it would hide after first use; targeted promos are
        single-use by default and BSS doesn't store the kind, so we accept that.)
        One loyalty call per customer.
        """
        if self._loyalty is None:
            return set()
        try:
            # loyalty caps offer.list at limit=100; a customer holding >100
            # offers is not a real scenario for this gate.
            resp = await self._loyalty.list_offers(customer_id=customer_id, limit=100)
        except ClientError:
            # A loyalty hiccup must not 500 the dashboard — degrade to "no
            # known usage" (shows eligible promos; loyalty still gates at claim).
            log.warning("catalog.promotion.consumed_check_failed", customer_id=customer_id)
            return set()
        return {
            row.get("offer_definition_id")
            for row in resp.get("rows", [])
            if row.get("state") in {"claimed", "redeemed"} and row.get("offer_definition_id")
        }

    async def resolve_eligible_promo(self, *, customer_id: str, offering_id: str) -> dict:
        """Auto-apply path (v1.1.1): the best *targeted* promo this customer is
        eligible for and that applies to ``offering_id``.

        Returns the promo's **code** (COM claims by code at activation, same as a
        typed code) + the composed terms, picking the lowest effective price.
        ``{valid: False, reason: "no_eligible_promo"}`` when none apply.
        """
        if self._loyalty is None:
            return {"valid": False, "reason": "loyalty_not_configured"}
        consumed = await self._consumed_offer_definitions(customer_id)
        best: dict | None = None
        for promo in await self._repo.list_eligible_promotions(customer_id):
            if promo.offer_definition_id in consumed:
                continue  # already used this single-use targeted promo
            composed = await self._compose(promo, offering_id)
            if composed.get("reason"):
                continue
            # v1.3.0 — carry the upfront-minted loyalty offer id so COM can use
            # ``advance_to_claimed`` at activation. NULL for pre-v1.3.0 rows; COM
            # then falls back to claim-by-code transparently.
            loyalty_offer_id = await self._repo.get_loyalty_offer_id(
                promotion_id=promo.id, customer_id=customer_id
            )
            candidate = {
                "valid": True,
                "code": promo.code,
                "promotion_id": promo.id,
                "offer_definition_id": promo.offer_definition_id,
                "loyalty_offer_id": loyalty_offer_id,
                **composed["terms"],
            }
            if best is None or candidate["effective"] < best["effective"]:
                best = candidate
        return best or {"valid": False, "reason": "no_eligible_promo"}

    async def preview_promo(
        self, *, code: str, offering_id: str, customer_id: str | None = None
    ) -> dict:
        """Portal-facing live preview — the display subset of validate_for_order.
        Passes customer_id so a targeted code is eligibility-gated in preview too."""
        r = await self.validate_for_order(
            code=code, offering_id=offering_id, customer_id=customer_id
        )
        return {
            "valid": r["valid"],
            "code": code,
            "offering_id": offering_id,
            "label": r["label"],
            "name": r["name"],
            "base": r["base"],
            "effective": r["effective"],
            "reason": r["reason"],
        }

    async def list_customer_offers(
        self, *, customer_id: str, state: str | None = None
    ) -> list[dict]:
        """Targeted promotions this customer is eligible for (dashboard 🎁 read).

        v1.1.1 — a pure BSS eligibility query; no loyalty call (targeted promos
        are eligibility rows, not issued offers). ``state`` is accepted for
        backwards compatibility and ignored.
        """
        if self._loyalty is None:
            return []
        consumed = await self._consumed_offer_definitions(customer_id)
        out: list[dict] = []
        for promo in await self._repo.list_eligible_promotions(customer_id):
            if promo.offer_definition_id in consumed:
                continue  # already used → don't show as available
            out.append(
                {
                    "promotion_id": promo.id,
                    "code": promo.code,
                    "offer_definition_id": promo.offer_definition_id,
                    "state": "eligible",
                    "promotion": {
                        "promotion_id": promo.id,
                        "name": promo.name,
                        "discount_type": promo.discount_type,
                        "discount_value": str(promo.discount_value),
                        "duration_kind": promo.duration_kind,
                        "periods_total": promo.periods_total,
                        "label": discount_label(
                            promo.discount_type, promo.discount_value, promo.currency
                        ),
                    },
                }
            )
        return out

    # ── targeted assignment (eligibility list) ─────────────────────────────

    async def assign_targeted(
        self,
        *,
        promotion_id: str,
        customer_ids: list[str],
    ) -> dict:
        """Add customers to a targeted promotion's eligibility list.

        v1.3.0 — *also* mints the customer↔offer pairing in loyalty upfront
        (``offer.issue``), so loyalty's per-customer views reflect the
        assignment immediately. The loyalty offer id is stamped on the BSS
        eligibility row; COM uses it at activation via
        ``advance_to_claimed`` (the path retired in v1.1.1 is restored for the
        targeted lane only). Public typed codes are unaffected — they're
        mint-and-claimed by code at activation.

        Idempotent across both sides: a customer already on the eligibility list
        is reported under ``already`` and loyalty is not re-called; the loyalty
        ``offer.issue`` itself is keyed by the deterministic offer id so re-runs
        are no-ops on loyalty's side too.

        Loyalty refusal / outage at issue time DOES NOT block the eligibility
        write — the BSS row is added with ``loyalty_offer_id=NULL`` and an
        ``catalog.promotion.loyalty_issue_failed_degrade`` warning is logged.
        At activation COM falls back to claim-by-code for that row (transparent
        backstop, same as a pre-v1.3.0 row).
        """
        _check_admin(self._actor)
        promo = await self._repo.get(promotion_id)
        if promo is None or promo.state != "active" or promo.audience != "targeted":
            raise PolicyViolation(
                rule="catalog.promotion.not_targeted",
                message=(
                    f"Promotion {promotion_id} is not an active targeted promo; "
                    "cannot add eligibility"
                ),
                context={
                    "promotion_id": promotion_id,
                    "state": promo.state if promo else None,
                    "audience": promo.audience if promo else None,
                },
            )

        eligible: list[str] = []
        already: list[str] = []
        for customer_id in customer_ids:
            if await self._repo.is_eligible(promotion_id, customer_id):
                already.append(customer_id)
                continue

            # v1.3.0 — mint the loyalty offer upfront. Deterministic id doubles
            # as the idempotency key so re-runs are safe on loyalty's side.
            offer_id = f"OFF-{customer_id}-{promotion_id}"
            loyalty_offer_id: str | None = None
            if self._loyalty is not None and promo.offer_definition_id:
                try:
                    await self._loyalty.issue_offer(
                        offer_id=offer_id,
                        offer_definition_id=promo.offer_definition_id,
                        customer_id=customer_id,
                        source={"type": "campaign", "campaign_id": promotion_id},
                        idempotency_key=offer_id,
                    )
                    loyalty_offer_id = offer_id
                except (ClientError, PolicyViolationFromServer) as exc:
                    # Degrade: row goes in without loyalty_offer_id; COM falls back
                    # to claim-by-code at activation. The eligibility is still
                    # honoured BSS-side; we just lose the upfront visibility win.
                    log.warning(
                        "catalog.promotion.loyalty_issue_failed_degrade",
                        promotion_id=promotion_id,
                        customer_id=customer_id,
                        offer_id=offer_id,
                        error=str(exc),
                    )

            await self._repo.add_eligibility(
                promotion_id=promotion_id,
                customer_id=customer_id,
                created_by=self._actor,
                loyalty_offer_id=loyalty_offer_id,
            )
            eligible.append(customer_id)
        await self._session.commit()

        log.info(
            "catalog.promotion.eligibility_added",
            promotion_id=promotion_id,
            added=len(eligible),
            already=len(already),
            actor=self._actor,
        )
        return {
            "promotion_id": promotion_id,
            "code": promo.code,
            "eligible": eligible,
            "already": already,
        }

    async def unassign_targeted(
        self,
        *,
        promotion_id: str,
        customer_ids: list[str],
    ) -> dict:
        """Remove customers from a targeted promo's eligibility list (v1.3.1).

        The mirror of ``assign_targeted``: delete the BSS eligibility row AND
        ``offer.revoke`` the upfront-minted loyalty offer. Cheap "delete the
        row" stopped being enough once v1.3.0 made the loyalty pairing the
        source of truth on the loyalty side too — without the revoke loyalty
        keeps showing the customer as paired (audit drift). Idempotent — a
        customer not on the list is reported under ``not_eligible``.

        Loyalty refusal / outage at revoke time does NOT block the eligibility
        delete (degrade pattern, mirrors assign). The BSS-side gate is closed
        regardless; loyalty drifts and a ``loyalty_revoke_failed_drift``
        warning is logged for the operator to reconcile later.
        """
        _check_admin(self._actor)
        promo = await self._repo.get(promotion_id)
        if promo is None or promo.audience != "targeted":
            raise PolicyViolation(
                rule="catalog.promotion.not_targeted",
                message=(
                    f"Promotion {promotion_id} is not a targeted promo; "
                    "cannot remove eligibility"
                ),
                context={
                    "promotion_id": promotion_id,
                    "audience": promo.audience if promo else None,
                },
            )

        removed: list[str] = []
        not_eligible: list[str] = []
        for customer_id in customer_ids:
            loyalty_offer_id = await self._repo.remove_eligibility(
                promotion_id=promotion_id, customer_id=customer_id
            )
            if loyalty_offer_id == "__not_found__":
                not_eligible.append(customer_id)
                continue
            # BSS gate closed. Terminal-transition the upfront-minted loyalty
            # offer (if we have an id — pre-v1.3.0 rows + degraded assigns
            # have None here; nothing to expire/revoke).
            #
            # Loyalty FSM: ``issued → expired`` (never-claimed offer, the
            # typical unassign case) vs ``claimed → revoked`` (order failed
            # path, owned by COM). We try ``expire`` first; if loyalty refuses
            # because the offer is already past issued (the customer used the
            # promo to order), we fall back to ``revoke`` to handle the
            # post-claim unassign case.
            if loyalty_offer_id and self._loyalty is not None:
                try:
                    await self._loyalty.expire_offer(
                        offer_id=loyalty_offer_id,
                        idempotency_key=f"{loyalty_offer_id}:expire:unassign",
                    )
                except PolicyViolationFromServer as exc:
                    if exc.rule == "offer.expire.illegal_state":
                        try:
                            await self._loyalty.revoke_offer(
                                offer_id=loyalty_offer_id,
                                reason="operator_action",
                                idempotency_key=f"{loyalty_offer_id}:revoke:unassign",
                            )
                        except (ClientError, PolicyViolationFromServer) as exc2:
                            log.warning(
                                "catalog.promotion.loyalty_revoke_failed_drift",
                                promotion_id=promotion_id,
                                customer_id=customer_id,
                                loyalty_offer_id=loyalty_offer_id,
                                error=str(exc2),
                            )
                    else:
                        log.warning(
                            "catalog.promotion.loyalty_expire_failed_drift",
                            promotion_id=promotion_id,
                            customer_id=customer_id,
                            loyalty_offer_id=loyalty_offer_id,
                            error=str(exc),
                        )
                except ClientError as exc:
                    log.warning(
                        "catalog.promotion.loyalty_expire_failed_drift",
                        promotion_id=promotion_id,
                        customer_id=customer_id,
                        loyalty_offer_id=loyalty_offer_id,
                        error=str(exc),
                    )
            removed.append(customer_id)
        await self._session.commit()

        log.info(
            "catalog.promotion.eligibility_removed",
            promotion_id=promotion_id,
            removed=len(removed),
            not_eligible=len(not_eligible),
            actor=self._actor,
        )
        return {
            "promotion_id": promotion_id,
            "code": promo.code,
            "removed": removed,
            "not_eligible": not_eligible,
        }

    # ── exhaust (operator-initiated terminal stop) ─────────────────────────

    async def exhaust_promotion(self, *, promotion_id: str) -> Promotion:
        """Flip an ``active`` promotion to ``exhausted`` — terminal for new
        orders, leaves the row in place for audit (v1.4.1).

        ``validate_for_order`` and ``resolve_eligible_promo`` already reject
        non-``active`` rows (line 143, 363 above), so a code that hits the
        signup funnel after exhaustion is silently treated as no-discount and
        the order proceeds at full price. No loyalty-side call — loyalty's
        ``offer_definition`` doesn't have a "deactivate" verb, and the BSS
        state gate is sufficient at the validate seam.

        **Note on the v1.1.3 graceful-degrade path:** that fix protects
        against loyalty refusing a claim at ``service_order.completed`` for
        an order whose discount snapshot was already in flight. Exhausting
        a promo via this verb stops new orders from picking up the discount
        but does NOT retroactively poison in-flight orders — those still
        claim normally against loyalty. The v1.1.3 fix and this verb cover
        different timing windows of the same "promo is no longer good"
        concern.

        Idempotent: re-calling on an already-exhausted promo returns the
        row unchanged. Refuses to flip pending_link / retired rows since
        those represent half-built / archived states with different
        semantics.
        """
        _check_admin(self._actor)
        promo = await self._repo.get(promotion_id)
        if promo is None:
            # The route handler returns 404 for None to keep the verb's
            # surface symmetric with GET /promotion/{id}. We raise a
            # marker PolicyViolation so the service-layer contract stays
            # "mutations raise, reads return None" — the route catches
            # this specific rule and re-maps to HTTPException(404).
            raise PolicyViolation(
                rule="catalog.promotion.not_found",
                message=f"Promotion {promotion_id} not found",
                context={"promotion_id": promotion_id},
            )
        if promo.state == "exhausted":
            # Idempotent — operator can re-run without surprise.
            return promo
        if promo.state != "active":
            raise PolicyViolation(
                rule="catalog.promotion.exhaust.not_active",
                message=(
                    f"Promotion {promotion_id} is in state {promo.state!r}; "
                    "only ``active`` promos can be exhausted."
                ),
                context={
                    "promotion_id": promotion_id,
                    "state": promo.state,
                },
            )
        promo.state = "exhausted"
        await self._session.commit()
        log.info(
            "catalog.promotion.exhausted",
            promotion_id=promotion_id,
            actor=self._actor,
        )
        return promo

    # ── create saga ────────────────────────────────────────────────────────

    async def create_promotion(
        self,
        *,
        promotion_id: str,
        discount_type: str,
        discount_value: Decimal,
        duration_kind: str,
        audience: str = "public",
        currency: str = "SGD",
        code: str | None = None,
        promo_code_kind: str | None = None,
        applicable_offering_ids: list[str] | None = None,
        periods_total: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        display_name: str | None = None,
    ) -> Promotion:
        """Create money terms + register the loyalty code (two-system saga).

        Both audiences register a real loyalty code (v1.1.1):
        - ``public`` — advertised; anyone may type the code.
        - ``targeted`` — not advertised; auto-applied only for customers added
          via ``assign_targeted``. If no ``code`` is given, one is derived from
          the promotion id (it's BSS-internal). Defaults to a one-per-customer
          kind so each eligible customer can use it once.
        """
        _check_admin(self._actor)
        if self._loyalty is None:
            raise PolicyViolation(
                rule="catalog.promotion.loyalty_not_configured",
                message="Promotions require loyalty-cli (BSS_LOYALTY_API_TOKEN is unset)",
                context={"promotion_id": promotion_id},
            )

        # Targeted promos still need a loyalty code; derive sensible defaults so
        # the operator doesn't have to invent an internal code/kind.
        if audience == "targeted":
            code = code or promotion_id
            promo_code_kind = promo_code_kind or "single_use_unique_per_customer"

        self._validate(
            discount_type=discount_type,
            discount_value=discount_value,
            duration_kind=duration_kind,
            periods_total=periods_total,
            audience=audience,
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
                name=display_name,  # friendly label for customer display
                audience=audience,
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
        # loyalty's idempotency cache dedupes on (actor, Idempotency-Key) WITHOUT
        # the tool name — so each saga step needs its own key, or the second
        # call replays the first's cached result. Suffix per step; a retried
        # saga still replays each step correctly.
        try:
            await self._loyalty.register_offer_definition(
                definition_id=od_id,
                display_name=display_name or promotion_id,
                idempotency_key=f"{promotion_id}:od",
            )
            if code is not None:
                await self._loyalty.register_promo_code(
                    code=code,
                    offer_definition_id=od_id,
                    kind=promo_code_kind,
                    idempotency_key=f"{promotion_id}:code",
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
        audience: str,
        code: str | None,
        promo_code_kind: str | None,
    ) -> None:
        if audience not in _AUDIENCES:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_audience",
                message=f"audience must be one of {sorted(_AUDIENCES)}",
                context={"audience": audience},
            )
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
        # Every promotion (both audiences) registers a loyalty code now.
        if not code:
            raise PolicyViolation(
                rule="catalog.promotion.requires_code",
                message="a promotion requires a code (targeted codes are derived if omitted)",
                context={"audience": audience},
            )
        if promo_code_kind not in _PROMO_CODE_KINDS:
            raise PolicyViolation(
                rule="catalog.promotion.invalid_promo_code_kind",
                message=f"promo_code_kind must be one of {sorted(_PROMO_CODE_KINDS)}",
                context={"promo_code_kind": promo_code_kind},
            )
