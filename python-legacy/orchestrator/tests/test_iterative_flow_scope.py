"""v1.5 doctrine guard — ``ITERATIVE FLOW`` must not leak into the
customer-chat system prompt.

Compound actions are an operator capability. The customer-chat surface
runs the ``customer_self_serve`` tool profile, which has no destructive
tools and a narrow read surface; an LLM with the operator-side
ITERATIVE FLOW block in scope would still be constrained by the
profile filter, but its responses would describe operator workflows
("propose-then-/confirm", "destructive", "case.investigate") to a
customer for whom none of that surface exists. Worse, "register
CUST-XYZ + create order" patterns invite the customer-side LLM to
chain reads-to-writes in a way the v0.12 chat caps + ownership trip-
wire were not stress-tested against.

The split is: ``_COCKPIT_INVARIANTS`` (operator) gets ITERATIVE FLOW;
``customer_chat_prompt`` does NOT. This test pins that boundary.
"""

from __future__ import annotations

from pathlib import Path

# We read the prompt module's TEXT — not its loaded constants — so the
# guard catches the case where someone copy-pastes the block under a
# differently-named constant. A grep-equivalent at test time.

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (_REPO_ROOT / rel_path).read_text(encoding="utf-8")


def test_customer_chat_prompt_has_no_iterative_flow_block() -> None:
    body = _read("orchestrator/bss_orchestrator/customer_chat_prompt.py")
    assert "ITERATIVE FLOW" not in body, (
        "customer_chat_prompt.py must NOT contain the operator-side "
        "ITERATIVE FLOW block — compound actions are an operator "
        "capability. If you want to enable multi-step chains for "
        "customer chat, raise it as a doctrine amendment first."
    )


def test_operator_cockpit_prompt_has_iterative_flow_block() -> None:
    # The flip side — guard against accidental removal of the block
    # from the operator prompt, which would silently regress v1.5's
    # main unlock.
    body = _read("packages/bss-cockpit/bss_cockpit/prompts.py")
    assert "ITERATIVE FLOW" in body, (
        "bss_cockpit/prompts.py MUST contain the ITERATIVE FLOW block "
        "(v1.5). If you removed it intentionally you need a doctrine "
        "amendment in CLAUDE.md plus an update to this test."
    )


def test_iterative_flow_block_is_under_v15_marker() -> None:
    # Soft check that the block is tagged with the v1.5 marker so a
    # future code-archaeology sweep can find it. Tightens the bond
    # between phase doc and prompt content.
    body = _read("packages/bss-cockpit/bss_cockpit/prompts.py")
    assert "(v1.5+) ITERATIVE FLOW" in body, (
        "ITERATIVE FLOW block should be tagged with the v1.5 marker "
        '("(v1.5+) ITERATIVE FLOW") so the version history stays '
        "discoverable from the source."
    )
