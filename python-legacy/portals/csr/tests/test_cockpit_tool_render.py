"""Cockpit web — tool-row rendering doctrine (v0.19+ Option 1).

The browser veneer must render the conversation store's ``tool``-role
rows through the SAME deterministic ASCII pipeline as the REPL, then
wrap the result in ``<pre>``. There is exactly ONE rendering rule for
tool results across both surfaces; this file proves the cockpit route
honours it.
"""

from __future__ import annotations

import json

from bss_csr.routes.cockpit import _render_tool_row_as_pre


class TestToolRowRendersAsPre:
    def test_port_request_list_real_json_becomes_pre(self) -> None:
        rows = [
            {
                "id": "PORT-93A0E81F",
                "direction": "port_out",
                "donorMsisdn": "90000000",
                "donorCarrier": "BSS-CLI",
                "state": "completed",
                "requestedPortDate": "2026-05-04",
            },
            {
                "id": "PORT-50627949",
                "direction": "port_in",
                "donorMsisdn": "98007777",
                "donorCarrier": "ACME Mobile",
                "state": "requested",
                "requestedPortDate": "2026-05-05",
            },
        ]
        out = _render_tool_row_as_pre("port_request.list", json.dumps(rows))
        # The renderer fired — IDs and the deterministic header land
        # in the output verbatim. (Note: line breaks are encoded as
        # &#10; inside <pre>; the browser renders them as newlines.)
        assert "PORT-93A0E81F" in out
        assert "ACME Mobile" in out
        assert "Port Requests" in out
        # Wrapped in <pre>, NOT <table>. This is the non-negotiable.
        assert "<pre" in out
        assert "<table" not in out
        # Pill announces the tool name (no shouting markdown grammar).
        assert "port_request.list" in out
        # Single-line safety: no raw \n that would split the SSE frame.
        assert "\n" not in out

    def test_inventory_msisdn_count_renders_through_shared_dispatch(self) -> None:
        payload = {
            "available": 47,
            "reserved": 0,
            "assigned": 3,
            "ported_out": 0,
            "total": 50,
            "prefix": "8888",
        }
        out = _render_tool_row_as_pre(
            "inventory.msisdn.count", json.dumps(payload)
        )
        assert "<pre" in out
        assert "<table" not in out
        # Real numbers from the payload, not paraphrased.
        assert "47" in out and "50" in out
        assert "prefix=8888" in out

    def test_unknown_tool_falls_back_to_raw_json_inside_pre(self) -> None:
        """Per doctrine, when a tool has no registered renderer the
        body surfaces verbatim — never as a fabricated table."""
        body = '{"foo": "bar"}'
        out = _render_tool_row_as_pre("never.registered", body)
        assert "<pre" in out
        assert "<table" not in out
        assert "foo" in out and "bar" in out

    def test_pre_rendered_ascii_passes_through_untouched(self) -> None:
        """Stored ASCII (e.g. from the REPL's /360 slash command) should
        not be re-rendered — it's already in its final form."""
        ascii_card = "── Customers " + "─" * 50 + "\n  CUST-001  …"
        out = _render_tool_row_as_pre("customer.list", ascii_card)
        assert "<pre" in out
        assert "<table" not in out
        # The ASCII is HTML-escaped (──, ─ pass through; any &lt; / &gt;
        # in real cards would too). Verify the content survives.
        assert "Customers" in out
        assert "CUST-001" in out

    def test_html_escape_protects_against_injection(self) -> None:
        """The body is HTML-escaped before being placed in <pre>; a
        crafted tool result containing tags must not break out."""
        body = "<script>alert(1)</script>"
        out = _render_tool_row_as_pre("never.registered", body)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_output_is_single_physical_line_safe_for_sse(self) -> None:
        """SSE's ``data:`` field must be a single line. The renderer
        encodes ``\\n`` as ``&#10;`` so multi-line ASCII cards survive
        the wire intact — without this, only the first line of a port-
        request table or MSISDN list would reach the browser."""
        rows = [{"id": f"PORT-{i:08X}", "direction": "port_in",
                 "donorMsisdn": "98007777", "donorCarrier": "ACME",
                 "state": "completed",
                 "requestedPortDate": "2026-05-04"} for i in range(5)]
        out = _render_tool_row_as_pre("port_request.list", json.dumps(rows))
        # No raw \n anywhere — would split the SSE frame.
        assert "\n" not in out
        # Newlines are ENCODED, not removed — the browser will render
        # them as line breaks inside <pre>.
        assert "&#10;" in out

    def test_unified_block_wraps_in_details_open(self) -> None:
        """Doctrine: page-load + SSE both produce the same
        ``<details open>`` block — same look-and-feel either way."""
        out = _render_tool_row_as_pre("port_request.list", "[]")
        assert '<details class="tool-row" open>' in out
        assert "<summary" in out
        assert "tool-row-name" in out
        assert "port_request.list" in out
        assert "<pre" in out

    def test_include_pill_legacy_arg_does_not_change_output(self) -> None:
        """``include_pill`` is preserved for source compatibility but
        no longer affects HTML — both code paths must emit the same
        block so look-and-feel is identical."""
        a = _render_tool_row_as_pre(
            "port_request.list", "[]", include_pill=False
        )
        b = _render_tool_row_as_pre("port_request.list", "[]")
        assert a == b


class TestSuppressToolRecap:
    """Defense-in-depth: when the LLM ignores the prompt's "do not
    re-render tool results" rule and produces a structural recap of
    a card the operator already saw, the bubble is replaced with a
    short acknowledgement before it reaches the SSE wire.
    """

    def test_pre_tag_recap_after_rendered_tool_is_suppressed(self) -> None:
        """The May 2026 cockpit screenshot: gemma produced
        ``<pre>Customer: Ck Chiam ... </pre>`` after customer.get
        already drew the canonical 360 card. The wrapper text leaks
        as literal angle brackets; the inner content duplicates the
        tool result. Both go away."""
        from bss_csr.routes.cockpit import _suppress_tool_recap

        text = (
            "<pre>\n"
            "Customer: Ck Chiam (CUST-c2c2b089)\n"
            "Status: active | KYC: verified\n"
            "Subscriptions (1):\n"
            "- SUB-3513 | PLAN_M | 88000009 | active\n"
            "</pre>"
        )
        out = _suppress_tool_recap(
            text, [{"name": "customer.get", "args": {"customer_id": "CUST-001"}}]
        )
        assert out == "(see above)"

    def test_headered_recap_after_rendered_tool_is_suppressed(self) -> None:
        """Even without a `<pre>` wrapper, multiple "Header: value"
        lines that mirror a Customer 360 card are still a recap."""
        from bss_csr.routes.cockpit import _suppress_tool_recap

        text = (
            "Customer 360: Ck Chiam (CUST-c2c2b089)\n"
            "**Status:** active | **KYC:** verified | **Since:** 2026-05-04\n"
            "**Contact Details**\n"
            "- **Email:** chiamck+001@icloud.com\n"
            "**Active Subscription**\n"
            "- ID: SUB-3513"
        )
        out = _suppress_tool_recap(
            text, [{"name": "customer.get", "args": {}}]
        )
        assert out == "(see above)"

    def test_legitimate_commentary_is_not_suppressed(self) -> None:
        """A short prose acknowledgement ("I've topped up your line —
        balance now shows X") is NOT a recap. The heuristic must let
        single-sentence commentary through."""
        from bss_csr.routes.cockpit import _suppress_tool_recap

        text = "I've topped up your line — your balance now shows 35 GB."
        out = _suppress_tool_recap(
            text, [{"name": "vas.purchase_for_me", "args": {}}]
        )
        assert out == text  # unchanged

    def test_recap_without_rendered_tool_is_left_alone(self) -> None:
        """If the turn fired no rendered tool, the LLM had nothing to
        echo; suppressing the bubble would lose information.
        Suppression requires a rendered-tool prerequisite."""
        from bss_csr.routes.cockpit import _suppress_tool_recap

        text = (
            "Customer: Ck Chiam\nStatus: active\nKYC: verified"
        )
        # No tool calls at all
        out = _suppress_tool_recap(text, [])
        assert out == text

        # A tool that doesn't have a renderer (raw JSON shown) — the
        # LLM may legitimately summarise it, so don't suppress.
        out = _suppress_tool_recap(
            text, [{"name": "never.registered", "args": {}}]
        )
        assert out == text

    def test_one_header_match_is_not_a_recap(self) -> None:
        """A single 'Status: …' mention in commentary is below the
        threshold — only multi-line structured recaps trigger."""
        from bss_csr.routes.cockpit import _suppress_tool_recap

        text = "Their status: active. The case is open and assigned."
        out = _suppress_tool_recap(
            text, [{"name": "customer.get", "args": {}}]
        )
        assert out == text

    def test_empty_text_is_left_alone(self) -> None:
        from bss_csr.routes.cockpit import _suppress_tool_recap
        assert _suppress_tool_recap("", [{"name": "customer.get"}]) == ""
        assert _suppress_tool_recap(None, [{"name": "customer.get"}]) is None
