"""PCI scope startup guard tests (v0.16 Track 2).

The guard refuses portal boot in BSS_ENV=production + BSS_PAYMENT_PROVIDER=stripe
mode if any Jinja template still has a card-number `<input>` element.
PAN must NEVER touch BSS in production (DECISIONS 2026-05-03).
"""

from __future__ import annotations

import pytest
from bss_self_serve.pci_scope import scan_templates_for_pan_inputs


class TestPciScopeGuard:
    def test_clean_directory_passes(self, tmp_path):
        clean = tmp_path / "templates"
        clean.mkdir()
        (clean / "ok.html").write_text(
            "<html><body><p>nothing to see here</p></body></html>"
        )
        # No exception expected.
        scan_templates_for_pan_inputs(templates_dir=clean)

    def test_card_number_input_trips_guard(self, tmp_path):
        bad = tmp_path / "templates"
        bad.mkdir()
        (bad / "signup.html").write_text(
            '<form><input type="text" name="card_number" required/></form>'
        )
        with pytest.raises(RuntimeError, match="card-number inputs"):
            scan_templates_for_pan_inputs(templates_dir=bad)

    def test_camel_case_cardNumber_input_trips_guard(self, tmp_path):
        bad = tmp_path / "templates"
        bad.mkdir()
        (bad / "signup.html").write_text(
            '<form><input name="cardNumber" /></form>'
        )
        with pytest.raises(RuntimeError, match="card-number inputs"):
            scan_templates_for_pan_inputs(templates_dir=bad)

    def test_pan_input_trips_guard(self, tmp_path):
        bad = tmp_path / "templates"
        bad.mkdir()
        (bad / "signup.html").write_text(
            '<form><input name="pan" type="text" /></form>'
        )
        with pytest.raises(RuntimeError, match="card-number inputs"):
            scan_templates_for_pan_inputs(templates_dir=bad)

    def test_card_pan_passes(self, tmp_path):
        # The mock-mode v0.1 form uses name="card_pan" (mock dev affordance).
        # The guard is for `card_number` / `cardNumber` / `pan`; `card_pan`
        # is the mock-mode hidden field that v0.16 keeps as the explicit
        # mock affordance.
        ok = tmp_path / "templates"
        ok.mkdir()
        (ok / "mock_signup.html").write_text(
            '<form><input name="card_pan" type="hidden" value="" /></form>'
        )
        scan_templates_for_pan_inputs(templates_dir=ok)

    def test_missing_directory_passes_silently(self, tmp_path):
        # Unit-test app construction may import without templates mounted.
        # The guard fails open in this case; the integration test catches
        # real regressions in production.
        scan_templates_for_pan_inputs(templates_dir=tmp_path / "missing")

    def test_mock_suffix_files_are_exempt(self, tmp_path):
        # *_mock.html files are explicitly mock-mode-only and never
        # selected when payment_provider='stripe'. A card_number input
        # IS allowed inside one — that's the affordance the convention
        # exists to support.
        d = tmp_path / "templates"
        d.mkdir()
        (d / "payment_methods_add_mock.html").write_text(
            '<form><input type="text" name="card_number" required/></form>'
        )
        (d / "payment_methods_add.html").write_text(
            "<p>elements iframe goes here</p>"  # no PAN input
        )
        scan_templates_for_pan_inputs(templates_dir=d)

    def test_mock_suffix_does_not_exempt_card_number_in_production_template(
        self, tmp_path
    ):
        # Belt-and-suspenders: a non-_mock template with a PAN input
        # still trips even when a sibling _mock template exists.
        d = tmp_path / "templates"
        d.mkdir()
        (d / "regular.html").write_text(
            '<form><input type="text" name="card_number"/></form>'
        )
        (d / "other_mock.html").write_text(
            '<form><input type="text" name="card_number"/></form>'
        )
        with pytest.raises(RuntimeError, match=r"regular\.html"):
            scan_templates_for_pan_inputs(templates_dir=d)

    def test_real_portal_templates_pass(self):
        # The actual portal templates as committed must pass — if a
        # future commit accidentally adds a card_number input to a
        # non-_mock template, this test will catch it before the
        # production lifespan does.
        scan_templates_for_pan_inputs()
