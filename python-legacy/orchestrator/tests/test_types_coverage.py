"""Semantic-layer test — no raw ``str`` for an ID or enum where a typed alias exists.

A small LLM only generates correct IDs (``SUB-007``) and enums
(``"active"``) if the JSON schema it sees carries the hint. ``Annotated`` +
``Literal`` aliases in ``bss_orchestrator.types`` flow those hints into the
schema. A bare ``str`` on an ``*_id`` or ``state`` / ``type`` parameter is
a bug: the LLM will fabricate a value and the call will fail.

We walk every registered tool's signature and assert that no parameter
whose name matches one of the "should-be-typed" patterns is left as
plain ``str``.
"""

from __future__ import annotations

import inspect
import typing

import pytest
from bss_orchestrator.tools import TOOL_REGISTRY

# Parameter-name patterns that MUST use a typed alias (Annotated/Literal),
# never a bare ``str``.
_TYPED_PARAM_SUFFIXES = (
    "_id",
    "_state",
    "_type",
    "event_type",
    "task_type",
    "fault_type",
    "aggregate_type",
)
# Exact param names likewise required to be typed.
_TYPED_PARAM_EXACT = {
    "msisdn",
    "iccid",
    "state",
    "email",
    "phone",
    "unit",
}


def _is_bare_str(annotation: typing.Any) -> bool:
    """True only for the literal ``str`` type — an Annotated or Literal wrapper is fine."""
    if annotation is str:
        return True
    # Optional[str] / str | None style: unpack and check. If the non-None arm
    # is a bare ``str`` that's still a bug.
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return len(args) == 1 and args[0] is str
    return False


def _should_be_typed(param_name: str) -> bool:
    if param_name in _TYPED_PARAM_EXACT:
        return True
    return any(param_name.endswith(suf) for suf in _TYPED_PARAM_SUFFIXES)


@pytest.mark.parametrize("tool_name", sorted(TOOL_REGISTRY))
def test_tool_id_and_enum_params_use_typed_aliases(tool_name: str) -> None:
    fn = TOOL_REGISTRY[tool_name]
    sig = inspect.signature(fn)
    # get_type_hints resolves forward refs and strips Annotated by default,
    # but we want Annotated preserved so we can see it *is* annotated.
    hints = typing.get_type_hints(fn, include_extras=True)
    offenders: list[str] = []
    for pname, _param in sig.parameters.items():
        if pname in {"self", "cls"}:
            continue
        if not _should_be_typed(pname):
            continue
        ann = hints.get(pname)
        if ann is None:
            offenders.append(f"{pname}: <no annotation>")
            continue
        if _is_bare_str(ann):
            offenders.append(f"{pname}: {ann!r}")

    assert not offenders, (
        f"Tool {tool_name!r} has parameters that should use a typed alias "
        f"from bss_orchestrator.types but are bare str: {offenders}"
    )
