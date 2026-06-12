"""Unit tests for the v0.15 eSIM provider seam."""

import pytest
from app.domain.esim_provider import (
    EsimAccessEsimProvider,
    EsimOrderResult,
    EsimProviderAdapter,
    OneGlobalEsimProvider,
    SimEsimProvider,
    select_esim_provider,
)


@pytest.mark.asyncio
async def test_sim_order_profile_returns_success_without_provider_reference():
    sim = SimEsimProvider()
    result = await sim.order_profile(iccid="89010", imsi="525010", msisdn="6591234567")
    assert isinstance(result, EsimOrderResult)
    assert result.success is True
    assert result.provider_reference is None


@pytest.mark.asyncio
async def test_sim_release_profile_is_noop():
    sim = SimEsimProvider()
    assert await sim.release_profile(iccid="89010") is None


@pytest.mark.asyncio
async def test_sim_get_activation_code_returns_synthetic_lpa():
    sim = SimEsimProvider()
    code = await sim.get_activation_code(iccid="89010")
    assert code.startswith("LPA:1$")
    assert "89010" in code


@pytest.mark.asyncio
async def test_one_global_stub_raises_on_any_call():
    p = OneGlobalEsimProvider()
    with pytest.raises(NotImplementedError, match="1GLOBAL Connect"):
        await p.order_profile(iccid="x", imsi="y", msisdn="z")
    with pytest.raises(NotImplementedError, match="1GLOBAL Connect"):
        await p.release_profile(iccid="x")
    with pytest.raises(NotImplementedError, match="1GLOBAL Connect"):
        await p.get_activation_code(iccid="x")


@pytest.mark.asyncio
async def test_esim_access_stub_raises_on_any_call():
    p = EsimAccessEsimProvider()
    with pytest.raises(NotImplementedError, match="eSIM Access"):
        await p.order_profile(iccid="x", imsi="y", msisdn="z")


def test_select_esim_provider_sim():
    p = select_esim_provider("sim")
    assert isinstance(p, SimEsimProvider)
    assert isinstance(p, EsimProviderAdapter)


def test_select_esim_provider_onbglobal_returns_stub_no_raise():
    p = select_esim_provider("onbglobal")
    assert isinstance(p, OneGlobalEsimProvider)


def test_select_esim_provider_esim_access_returns_stub_no_raise():
    p = select_esim_provider("esim_access")
    assert isinstance(p, EsimAccessEsimProvider)


def test_select_esim_provider_unknown_fails_fast():
    with pytest.raises(RuntimeError, match="Unknown BSS_ESIM_PROVIDER"):
        select_esim_provider("singpass")


def test_protocol_is_runtime_checkable():
    """Sanity: Protocol is annotated @runtime_checkable so isinstance() works."""
    assert isinstance(SimEsimProvider(), EsimProviderAdapter)
    assert isinstance(OneGlobalEsimProvider(), EsimProviderAdapter)
    assert isinstance(EsimAccessEsimProvider(), EsimProviderAdapter)
