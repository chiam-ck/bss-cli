"""v1.1 — PromotionService.create_promotion two-system saga.

Runs against the live dev DB (like the other catalog write tests): unique
promotion ids + finally-cleanup. The LoyaltyClient is faked so the saga
logic is tested without touching the real loyalty-cli (no entitlement-ledger
pollution) — the LoyaltyClient's own contract is covered in bss-clients.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from bss_catalog.policies import PolicyViolation
from bss_catalog.promotion_repository import PromotionRepository
from bss_catalog.promotion_service import PromotionService
from bss_clients import NotFound, PolicyViolationFromServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _pid(prefix: str = "PROMO") -> str:
    return f"{prefix}_TEST_{uuid.uuid4().hex[:8].upper()}"


class FakeLoyalty:
    """In-memory stand-in for the loyalty-cli HTTP surface.

    Remembers registered codes→OD so show_promo_code resolves like the real
    service. ``refuse`` makes the first OD register raise; ``issue_refuse_for``
    is a set of customer ids whose offer.issue is refused (already-issued sim).
    """

    def __init__(self, *, refuse: bool = False, issue_refuse_for: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._refuse = refuse
        self._issue_refuse_for = issue_refuse_for or set()
        self.codes: dict[str, str] = {}  # code -> offer_definition_id
        self.list_rows: list[dict] = []

    async def register_offer_definition(self, **kwargs):
        self.calls.append(("register_offer_definition", kwargs))
        if self._refuse:
            raise PolicyViolationFromServer(
                rule="offer_definition.register.duplicate",
                message="offer definition already exists",
                context={"source": "loyalty"},
            )
        return {"id": kwargs["definition_id"]}

    async def register_promo_code(self, **kwargs):
        self.calls.append(("register_promo_code", kwargs))
        self.codes[kwargs["code"]] = kwargs["offer_definition_id"]
        return {"code": kwargs["code"]}

    async def show_promo_code(self, code: str):
        self.calls.append(("show_promo_code", {"code": code}))
        if code not in self.codes:
            raise NotFound(f"no such code {code}")
        return {"offer_definition_id": self.codes[code], "state": "active"}

    async def issue_offer(self, **kwargs):
        self.calls.append(("issue_offer", kwargs))
        if kwargs["customer_id"] in self._issue_refuse_for:
            raise PolicyViolationFromServer(
                rule="offer.issue.already_issued",
                message="customer already has this offer",
                context={"source": "loyalty"},
            )
        return {"offer_id": kwargs["offer_id"], "state": "issued"}

    async def expire_offer(self, **kwargs):
        self.calls.append(("expire_offer", kwargs))
        return {"offer_id": kwargs["offer_id"], "state": "expired"}

    async def revoke_offer(self, **kwargs):
        self.calls.append(("revoke_offer", kwargs))
        return {"offer_id": kwargs["offer_id"], "state": "revoked"}

    async def list_offers(self, **kwargs):
        self.calls.append(("list_offers", kwargs))
        state = kwargs.get("state")
        rows = (
            [r for r in self.list_rows if r.get("state") == state]
            if state is not None
            else self.list_rows
        )
        return {"rows": rows, "limit": 50, "offset": 0, "has_more": False}

    def called(self, name: str) -> list[dict]:
        return [args for n, args in self.calls if n == name]


async def _cleanup(session: AsyncSession, *promotion_ids: str):
    # eligibility rows FK to promotion → delete them first
    await session.execute(
        text("DELETE FROM catalog.promotion_eligibility WHERE promotion_id = ANY(:ids)"),
        {"ids": list(promotion_ids)},
    )
    await session.execute(
        text("DELETE FROM catalog.promotion WHERE id = ANY(:ids)"),
        {"ids": list(promotion_ids)},
    )
    await session.commit()


def _svc(session, loyalty, actor="admin"):
    return PromotionService(session, PromotionRepository(session), loyalty, actor)


class TestCreatePromotionTargeted:
    async def test_targeted_promo_derives_and_registers_a_code(
        self, db_session: AsyncSession
    ):
        # v1.1.1 — targeted promos are eligibility-gated CODES (not codeless).
        # No code given → one is derived from the id; it IS registered in loyalty.
        pid = _pid("PROMO_VIP")
        loyalty = FakeLoyalty()
        try:
            promo = await _svc(db_session, loyalty).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                audience="targeted",
            )
            assert promo.state == "active"
            assert promo.audience == "targeted"
            assert promo.code == pid  # derived
            assert promo.offer_definition_id == f"OD_{pid}"
            # both audiences register a code now
            reg = loyalty.called("register_promo_code")
            assert len(reg) == 1
            assert reg[0]["code"] == pid
            assert reg[0]["kind"] == "single_use_unique_per_customer"  # targeted default
        finally:
            await _cleanup(db_session, pid)


class TestCreatePromotionNonTargeted:
    async def test_coded_promo_registers_code(self, db_session: AsyncSession):
        pid = _pid("PROMO_SUMMER")
        code = f"SUMMER_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            promo = await _svc(db_session, loyalty).create_promotion(
                promotion_id=pid,
                discount_type="absolute",
                discount_value=Decimal("5.00"),
                duration_kind="multi",
                periods_total=3,
                code=code,
                promo_code_kind="multi_use",
            )
            assert promo.state == "active"
            assert promo.code == code
            assert promo.periods_total == 3
            reg = loyalty.called("register_promo_code")
            assert len(reg) == 1
            assert reg[0]["code"] == code
            assert reg[0]["offer_definition_id"] == f"OD_{pid}"
            assert reg[0]["kind"] == "multi_use"
            # regression guard: OD-register and code-register must use DISTINCT
            # idempotency keys (loyalty dedupes on (actor,key) without the tool
            # name; a shared key replays the OD result and silently skips the code).
            od_key = loyalty.called("register_offer_definition")[0]["idempotency_key"]
            assert reg[0]["idempotency_key"] != od_key
        finally:
            await _cleanup(db_session, pid)


class TestSagaFailureLeavesPendingLink:
    async def test_loyalty_refusal_translates_and_row_stays_pending(
        self, db_session: AsyncSession
    ):
        pid = _pid("PROMO_FAIL")
        loyalty = FakeLoyalty(refuse=True)
        try:
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, loyalty).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                    audience="targeted",
                )
            assert exc.value.rule == "catalog.promotion.loyalty_refused"
            # row written but NOT confirmed — harmless, reconcilable
            row = await PromotionRepository(db_session).get(pid)
            assert row is not None
            assert row.state == "pending_link"
            assert row.offer_definition_id is None
        finally:
            await _cleanup(db_session, pid)

    async def test_retry_resumes_pending_link_to_active(self, db_session: AsyncSession):
        pid = _pid("PROMO_RESUME")
        try:
            # first attempt fails at loyalty
            with pytest.raises(PolicyViolation):
                await _svc(db_session, FakeLoyalty(refuse=True)).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("15"),
                    duration_kind="single",
                    audience="targeted",
                )
            # retry with a healthy loyalty resumes the same row (no duplicate)
            healthy = FakeLoyalty()
            promo = await _svc(db_session, healthy).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("15"),
                duration_kind="single",
                audience="targeted",
            )
            assert promo.state == "active"
            assert promo.offer_definition_id == f"OD_{pid}"
            assert len(await PromotionRepository(db_session).list(limit=1000)) >= 1
        finally:
            await _cleanup(db_session, pid)


class TestCreatePromotionGuards:
    async def test_active_promo_rejects_duplicate(self, db_session: AsyncSession):
        pid = _pid("PROMO_DUP")
        try:
            await _svc(db_session, FakeLoyalty()).create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
                audience="targeted",
            )
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(
                    promotion_id=pid,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                    audience="targeted",
                )
            assert exc.value.rule == "catalog.promotion.already_exists"
        finally:
            await _cleanup(db_session, pid)

    async def test_duplicate_code_rejected(self, db_session: AsyncSession):
        pid1, pid2 = _pid("PROMO_C1"), _pid("PROMO_C2")
        code = f"DUP_{uuid.uuid4().hex[:6].upper()}"
        try:
            await _svc(db_session, FakeLoyalty()).create_promotion(
                promotion_id=pid1,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
                code=code,
                promo_code_kind="single_use_shared",
            )
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(
                    promotion_id=pid2,
                    discount_type="percent",
                    discount_value=Decimal("10"),
                    duration_kind="single",
                    code=code,
                    promo_code_kind="single_use_shared",
                )
            assert exc.value.rule == "catalog.promotion.code_in_use"
        finally:
            await _cleanup(db_session, pid1, pid2)

    @pytest.mark.parametrize(
        "kwargs,expected_rule",
        [
            (dict(discount_type="bogus", discount_value=Decimal("10"), duration_kind="single"),
             "catalog.promotion.invalid_discount_type"),
            (dict(discount_type="percent", discount_value=Decimal("150"), duration_kind="single"),
             "catalog.promotion.invalid_discount_value"),
            (dict(discount_type="percent", discount_value=Decimal("0"), duration_kind="single"),
             "catalog.promotion.invalid_discount_value"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="multi"),
             "catalog.promotion.invalid_periods_total"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="single", periods_total=3),
             "catalog.promotion.invalid_periods_total"),
            (dict(discount_type="percent", discount_value=Decimal("10"), duration_kind="single", code="X"),
             "catalog.promotion.invalid_promo_code_kind"),
        ],
    )
    async def test_validation_rejects(self, db_session: AsyncSession, kwargs, expected_rule):
        pid = _pid("PROMO_VAL")
        try:
            with pytest.raises(PolicyViolation) as exc:
                await _svc(db_session, FakeLoyalty()).create_promotion(promotion_id=pid, **kwargs)
            assert exc.value.rule == expected_rule
        finally:
            await _cleanup(db_session, pid)

    async def test_non_admin_actor_rejected(self, db_session: AsyncSession):
        pid = _pid("PROMO_AUTH")
        with pytest.raises(PolicyViolation) as exc:
            await _svc(db_session, FakeLoyalty(), actor="anonymous").create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("10"),
                duration_kind="single",
            )
        assert exc.value.rule == "catalog.admin_only"


class TestLoyaltyDisabled:
    """loyalty is OPTIONAL — when the client is None the promo subsystem is OFF
    and core flows still work (orders proceed at full price)."""

    async def test_validate_returns_not_configured(self, db_session: AsyncSession):
        r = await _svc(db_session, None).validate_for_order(code="X", offering_id="PLAN_M")
        assert r["valid"] is False
        assert r["reason"] == "loyalty_not_configured"

    async def test_resolve_eligible_not_configured(self, db_session: AsyncSession):
        r = await _svc(db_session, None).resolve_eligible_promo(
            customer_id="CUST-1", offering_id="PLAN_M"
        )
        assert r["valid"] is False
        assert r["reason"] == "loyalty_not_configured"

    async def test_customer_offers_empty(self, db_session: AsyncSession):
        assert await _svc(db_session, None).list_customer_offers(customer_id="CUST-1") == []

    async def test_create_rejected(self, db_session: AsyncSession):
        with pytest.raises(PolicyViolation) as exc:
            await _svc(db_session, None).create_promotion(
                promotion_id=_pid(), discount_type="percent",
                discount_value=Decimal("10"), duration_kind="single", audience="targeted",
            )
        assert exc.value.rule == "catalog.promotion.loyalty_not_configured"


class TestValidateForOrder:
    async def test_valid_percent_composes_on_base(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_OK")
        code = f"OK_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is True
            assert r["offer_definition_id"] == f"OD_{pid}"
            assert r["base"] > 0
            # effective = 20% off the base the catalog actually returned
            assert r["effective"] == (r["base"] * Decimal("0.80")).quantize(Decimal("0.01"))
            assert r["label"] == "20% off"
        finally:
            await _cleanup(db_session, pid)

    async def test_unknown_code(self, db_session: AsyncSession):
        r = await _svc(db_session, FakeLoyalty()).validate_for_order(
            code="NOPE", offering_id="PLAN_M"
        )
        assert r["valid"] is False
        assert r["reason"] == "unknown_code"

    async def test_not_applicable_to_offering(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_NA")
        code = f"NA_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                applicable_offering_ids=["PLAN_S"],
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is False
            assert r["reason"] == "not_applicable_to_offering"
        finally:
            await _cleanup(db_session, pid)

    async def test_expired_window(self, db_session: AsyncSession):
        pid = _pid("PROMO_VAL_EXP")
        code = f"EXP_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="percent",
                discount_value=Decimal("20"),
                duration_kind="single",
                valid_to=datetime.now(timezone.utc) - timedelta(days=1),
                code=code,
                promo_code_kind="multi_use",
            )
            r = await svc.validate_for_order(code=code, offering_id="PLAN_M")
            assert r["valid"] is False
            assert r["reason"] == "expired"
        finally:
            await _cleanup(db_session, pid)

    async def test_preview_returns_display_subset(self, db_session: AsyncSession):
        pid = _pid("PROMO_PREVIEW")
        code = f"PV_{uuid.uuid4().hex[:6].upper()}"
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await svc.create_promotion(
                promotion_id=pid,
                discount_type="absolute",
                discount_value=Decimal("5.00"),
                duration_kind="single",
                code=code,
                promo_code_kind="multi_use",
            )
            p = await svc.preview_promo(code=code, offering_id="PLAN_M")
            assert set(p) == {"valid", "code", "offering_id", "label", "name", "base", "effective", "reason"}
            assert p["valid"] is True
            assert p["label"] == "SGD 5.00 off"
            assert p["effective"] == p["base"] - Decimal("5.00")
        finally:
            await _cleanup(db_session, pid)


async def _create_targeted(svc, pid, value="20"):
    return await svc.create_promotion(
        promotion_id=pid,
        discount_type="percent",
        discount_value=Decimal(value),
        duration_kind="single",
        audience="targeted",
    )


class TestAssignTargeted:
    async def test_adds_eligibility_idempotently(self, db_session: AsyncSession):
        pid = _pid("PROMO_ASSIGN")
        try:
            svc = _svc(db_session, FakeLoyalty())
            await _create_targeted(svc, pid)
            res = await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-1", "CUST-2"])
            assert set(res["eligible"]) == {"CUST-1", "CUST-2"}
            assert res["already"] == []
            # re-run → both already eligible (idempotent)
            res2 = await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-1", "CUST-3"])
            assert res2["eligible"] == ["CUST-3"]
            assert res2["already"] == ["CUST-1"]
        finally:
            await _cleanup(db_session, pid)

    async def test_mints_loyalty_offer_upfront_v130(self, db_session: AsyncSession):
        """v1.3.0 — assign_targeted issues the loyalty offer upfront so the
        customer↔offer pairing exists in loyalty immediately. The deterministic
        offer id is stored on the eligibility row, and at activation COM picks
        it up via resolve_eligible_promo's ``loyaltyOfferId`` to advance."""
        pid = _pid("PROMO_PAIR")
        try:
            loy = FakeLoyalty()
            svc = _svc(db_session, loy)
            await _create_targeted(svc, pid)
            await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-101"])
            # FakeLoyalty saw an offer.issue call for that customer
            issues = loy.called("issue_offer")
            assert len(issues) == 1
            call = issues[0]
            assert call["customer_id"] == "CUST-101"
            assert call["offer_id"] == f"OFF-CUST-101-{pid}"
            assert call["offer_definition_id"]  # set from the promo's OD
            assert call["source"]["type"] == "campaign"
            assert call["source"]["campaign_id"] == pid
            # Deterministic offer id doubles as the idempotency key — re-runs are
            # no-ops on loyalty.
            assert call["idempotency_key"] == call["offer_id"]
            # The eligibility row stamped the loyalty_offer_id (carried into
            # resolve_eligible_promo so COM can advance_to_claimed).
            offer_id = await svc._repo.get_loyalty_offer_id(
                promotion_id=pid, customer_id="CUST-101"
            )
            assert offer_id == f"OFF-CUST-101-{pid}"
        finally:
            await _cleanup(db_session, pid)

    async def test_unassign_expires_issued_offer(self, db_session: AsyncSession):
        """v1.3.1 — unassign expires (not revokes) the upfront-minted offer.
        Loyalty FSM: ``issued → expired`` is the terminal lane for a never-
        claimed offer; ``offer.revoke`` is illegal from ``issued`` (that's
        the ``claimed → revoked`` lane owned by COM)."""
        pid = _pid("PROMO_UNASSIGN")
        try:
            loy = FakeLoyalty()
            svc = _svc(db_session, loy)
            await _create_targeted(svc, pid)
            await svc.assign_targeted(
                promotion_id=pid, customer_ids=["CUST-301", "CUST-302"]
            )

            res = await svc.unassign_targeted(
                promotion_id=pid, customer_ids=["CUST-301", "CUST-999"]
            )
            assert res["removed"] == ["CUST-301"]
            assert res["not_eligible"] == ["CUST-999"]  # idempotent

            # offer.expire was called against the issued offer (revoke not used).
            expires = loy.called("expire_offer")
            assert len(expires) == 1
            assert expires[0]["offer_id"] == f"OFF-CUST-301-{pid}"
            assert loy.called("revoke_offer") == []
            # BSS state matches: CUST-301 gone, CUST-302 still eligible.
            assert not await svc._repo.is_eligible(pid, "CUST-301")
            assert await svc._repo.is_eligible(pid, "CUST-302")
        finally:
            await _cleanup(db_session, pid)

    async def test_unassign_falls_back_to_revoke_when_offer_already_claimed(
        self, db_session: AsyncSession
    ):
        """v1.3.1 — if the customer already claimed the offer (placed an
        order with the promo), the offer is in ``claimed`` state and
        ``offer.expire`` refuses with ``illegal_state``. Catalog falls back
        to ``offer.revoke`` (the ``claimed → revoked`` lane)."""
        pid = _pid("PROMO_UNASSIGN_CLAIMED")

        class ClaimedLoyalty(FakeLoyalty):
            async def expire_offer(self, **kwargs):
                self.calls.append(("expire_offer", kwargs))
                raise PolicyViolationFromServer(
                    rule="offer.expire.illegal_state",
                    message="offer is in claimed state",
                    context={"source": "loyalty"},
                )

        try:
            loy = ClaimedLoyalty()
            svc = _svc(db_session, loy)
            await _create_targeted(svc, pid)
            await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-501"])
            res = await svc.unassign_targeted(
                promotion_id=pid, customer_ids=["CUST-501"]
            )
            assert res["removed"] == ["CUST-501"]
            # Tried expire first, then fell back to revoke.
            assert len(loy.called("expire_offer")) == 1
            revokes = loy.called("revoke_offer")
            assert len(revokes) == 1
            assert revokes[0]["reason"] == "operator_action"
        finally:
            await _cleanup(db_session, pid)

    async def test_unassign_loyalty_failure_does_not_block_delete(
        self, db_session: AsyncSession
    ):
        """v1.3.1 — degrade: a loyalty refusal/outage at expire+revoke time
        does NOT block the BSS-side row delete. The gate must close even when
        loyalty drifts; the operator reconciles later (warning logged)."""
        pid = _pid("PROMO_UNASSIGN_DEGRADE")

        class RaisingLoyalty(FakeLoyalty):
            async def expire_offer(self, **kwargs):
                self.calls.append(("expire_offer", kwargs))
                # Some other refusal that's NOT illegal_state → no revoke fallback.
                raise PolicyViolationFromServer(
                    rule="offer.expire.not_found",
                    message="offer missing",
                    context={"source": "loyalty"},
                )

        try:
            svc = _svc(db_session, RaisingLoyalty())
            await _create_targeted(svc, pid)
            await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-404"])
            res = await svc.unassign_targeted(
                promotion_id=pid, customer_ids=["CUST-404"]
            )
            assert res["removed"] == ["CUST-404"]
            assert not await svc._repo.is_eligible(pid, "CUST-404")
        finally:
            await _cleanup(db_session, pid)

    async def test_loyalty_issue_failure_degrades_to_claim_by_code(
        self, db_session: AsyncSession
    ):
        """v1.3.0 — if loyalty refuses the upfront ``offer.issue`` (outage or
        policy refusal), the eligibility is still written with
        ``loyalty_offer_id=NULL`` and COM transparently falls back to
        claim-by-code at activation. We never block the assignment on loyalty."""
        pid = _pid("PROMO_PAIR_FAIL")
        try:
            loy = FakeLoyalty(issue_refuse_for={"CUST-202"})
            svc = _svc(db_session, loy)
            await _create_targeted(svc, pid)
            res = await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-202"])
            # eligibility still recorded
            assert res["eligible"] == ["CUST-202"]
            # ...but with no loyalty_offer_id (the degrade signal)
            offer_id = await svc._repo.get_loyalty_offer_id(
                promotion_id=pid, customer_id="CUST-202"
            )
            assert offer_id is None
        finally:
            await _cleanup(db_session, pid)

    async def test_rejects_public_or_missing_promo(self, db_session: AsyncSession):
        # missing promo
        with pytest.raises(PolicyViolation) as exc:
            await _svc(db_session, FakeLoyalty()).assign_targeted(
                promotion_id=_pid("PROMO_GHOST"), customer_ids=["CUST-1"]
            )
        assert exc.value.rule == "catalog.promotion.not_targeted"
        # a public promo can't take eligibility
        pid = _pid("PROMO_PUB")
        code = f"PUB_{uuid.uuid4().hex[:6].upper()}"
        try:
            svc = _svc(db_session, FakeLoyalty())
            await svc.create_promotion(
                promotion_id=pid, discount_type="percent", discount_value=Decimal("10"),
                duration_kind="single", code=code, promo_code_kind="multi_use",
            )
            with pytest.raises(PolicyViolation) as exc:
                await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-1"])
            assert exc.value.rule == "catalog.promotion.not_targeted"
        finally:
            await _cleanup(db_session, pid)


class TestResolveEligiblePromo:
    async def test_picks_best_eligible_promo_and_returns_code(self, db_session: AsyncSession):
        # two targeted promos, customer eligible for both; lowest effective wins
        pid_lo, pid_hi = _pid("PROMO_LO"), _pid("PROMO_HI")
        try:
            svc = _svc(db_session, FakeLoyalty())
            await _create_targeted(svc, pid_lo, value="40")
            await _create_targeted(svc, pid_hi, value="10")
            await svc.assign_targeted(promotion_id=pid_lo, customer_ids=["CUST-1"])
            await svc.assign_targeted(promotion_id=pid_hi, customer_ids=["CUST-1"])
            r = await svc.resolve_eligible_promo(customer_id="CUST-1", offering_id="PLAN_M")
            assert r["valid"] is True
            assert r["promotion_id"] == pid_lo  # 40% off beats 10%
            assert r["code"] == pid_lo  # COM claims by this code
            assert r["discount_periods_total"] == 1
        finally:
            await _cleanup(db_session, pid_lo, pid_hi)

    async def test_no_eligible_promo(self, db_session: AsyncSession):
        r = await _svc(db_session, FakeLoyalty()).resolve_eligible_promo(
            customer_id="CUST-NONE", offering_id="PLAN_M"
        )
        assert r["valid"] is False
        assert r["reason"] == "no_eligible_promo"

    async def test_already_used_promo_excluded(self, db_session: AsyncSession):
        # eligible, but the customer already redeemed it → not offered again
        pid = _pid("PROMO_USED")
        loyalty = FakeLoyalty()
        try:
            svc = _svc(db_session, loyalty)
            await _create_targeted(svc, pid)
            await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-1"])
            # customer has a redeemed offer for this promo's OD
            loyalty.list_rows = [
                {"offer_id": "OFF-X", "state": "redeemed", "offer_definition_id": f"OD_{pid}"}
            ]
            r = await svc.resolve_eligible_promo(customer_id="CUST-1", offering_id="PLAN_M")
            assert r["valid"] is False  # already used → not auto-applied
            offers = await svc.list_customer_offers(customer_id="CUST-1")
            assert offers == []  # and not shown on the dashboard
        finally:
            await _cleanup(db_session, pid)


class TestListCustomerOffers:
    async def test_lists_eligible_promotions(self, db_session: AsyncSession):
        pid = _pid("PROMO_LIST")
        try:
            svc = _svc(db_session, FakeLoyalty())
            await _create_targeted(svc, pid, value="30")
            await svc.assign_targeted(promotion_id=pid, customer_ids=["CUST-1"])
            offers = await svc.list_customer_offers(customer_id="CUST-1")
            assert len(offers) == 1
            assert offers[0]["promotion"]["promotion_id"] == pid
            assert offers[0]["promotion"]["label"] == "30% off"
            # a different customer sees nothing
            assert await svc.list_customer_offers(customer_id="CUST-OTHER") == []
        finally:
            await _cleanup(db_session, pid)
