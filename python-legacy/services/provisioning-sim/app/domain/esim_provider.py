"""eSIM provider adapter — v0.15 seam for SM-DP+ integration.

The Protocol defines the production-shaped surface (`order_profile`,
`release_profile`, `get_activation_code`). v0.15 ships ``SimEsimProvider``
only — a near-no-op that defers timing and fault injection to the worker's
existing logic, preserving v0.13/v0.14 behavior exactly. ``OneGlobalEsimProvider``
and ``EsimAccessEsimProvider`` are stubs that raise on first call so the env
var ``BSS_ESIM_PROVIDER=onbglobal`` (or ``=esim_access``) can be set in advance
of v0.16+ integration without breaking startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_V016_POINTER = (
    "v0.16+: %s adapter requires NDA + production credentials. "
    "Set BSS_ESIM_PROVIDER=sim until the integration ships."
)


@dataclass(frozen=True)
class EsimOrderResult:
    success: bool
    provider_reference: str | None = None


@runtime_checkable
class EsimProviderAdapter(Protocol):
    async def order_profile(
        self, *, iccid: str, imsi: str, msisdn: str
    ) -> EsimOrderResult: ...

    async def release_profile(self, *, iccid: str) -> None: ...

    async def get_activation_code(self, *, iccid: str) -> str: ...


class SimEsimProvider:
    """v0.15 sim implementation. Behavior owned by the worker's existing
    TASK_DURATIONS + fault-injection dispatch — this provider is a logical
    hook that returns success without contributing latency. Swapping to a
    real provider in v0.16+ will replace both the synthetic sleep and the
    fault simulation with real HTTP latency and real errors.
    """

    async def order_profile(
        self, *, iccid: str, imsi: str, msisdn: str
    ) -> EsimOrderResult:
        return EsimOrderResult(success=True, provider_reference=None)

    async def release_profile(self, *, iccid: str) -> None:
        return None

    async def get_activation_code(self, *, iccid: str) -> str:
        return f"LPA:1$rsp.example/{iccid}"


class OneGlobalEsimProvider:
    async def order_profile(
        self, *, iccid: str, imsi: str, msisdn: str
    ) -> EsimOrderResult:
        raise NotImplementedError(_V016_POINTER % "1GLOBAL Connect")

    async def release_profile(self, *, iccid: str) -> None:
        raise NotImplementedError(_V016_POINTER % "1GLOBAL Connect")

    async def get_activation_code(self, *, iccid: str) -> str:
        raise NotImplementedError(_V016_POINTER % "1GLOBAL Connect")


class EsimAccessEsimProvider:
    async def order_profile(
        self, *, iccid: str, imsi: str, msisdn: str
    ) -> EsimOrderResult:
        raise NotImplementedError(_V016_POINTER % "eSIM Access")

    async def release_profile(self, *, iccid: str) -> None:
        raise NotImplementedError(_V016_POINTER % "eSIM Access")

    async def get_activation_code(self, *, iccid: str) -> str:
        raise NotImplementedError(_V016_POINTER % "eSIM Access")


def select_esim_provider(name: str) -> EsimProviderAdapter:
    """Resolve ``BSS_ESIM_PROVIDER`` to a concrete adapter.

    ``sim`` is the only valid v0.15 value that fully works. ``onbglobal`` and
    ``esim_access`` are accepted at startup (operator can set the env in
    advance of the v0.16+ integration) but raise on first call. Unknown
    names fail fast.
    """
    if name == "sim":
        return SimEsimProvider()
    if name == "onbglobal":
        return OneGlobalEsimProvider()
    if name == "esim_access":
        return EsimAccessEsimProvider()
    raise RuntimeError(
        f"Unknown BSS_ESIM_PROVIDER={name!r}; expected one of "
        "'sim' | 'onbglobal' | 'esim_access'"
    )
