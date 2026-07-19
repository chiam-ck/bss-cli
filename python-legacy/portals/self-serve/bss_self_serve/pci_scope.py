"""PCI scope startup guard (v0.16 Track 2).

Refuses to boot the portal in production-stripe mode if any Jinja
template under ``templates/`` still has a card-number `<input>`. The
doctrine line is "PAN never touches BSS in production"
(DECISIONS 2026-05-03 — PCI scope: Stripe.js + Elements only in
production). The scan is a regex; false positives are acceptable
(a comment that mentions ``card_number`` would trip it; fix the
comment). The scan is cheap (~few ms at boot).

Mock mode skips the scan entirely — the v0.1 server-rendered form is
the dev affordance and stays.
"""

from __future__ import annotations

import pathlib
import re

# Match any input named card_number (single or double quotes, optional
# `type="..."` attribute, any element). Conservative: misses obfuscated
# JS-rendered inputs but the goal is to catch the obvious accidents.
_PAN_INPUT_PATTERNS = (
    re.compile(r'''<input[^>]*\bname\s*=\s*["']card_number["']''', re.IGNORECASE),
    re.compile(r'''<input[^>]*\bname\s*=\s*["']pan["']''', re.IGNORECASE),
    re.compile(r'''<input[^>]*\bname\s*=\s*["']cardNumber["']''', re.IGNORECASE),
)

_TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"


def scan_templates_for_pan_inputs(
    templates_dir: pathlib.Path | None = None,
) -> None:
    """Walk every .html under templates/; raise on any PAN-input match.

    Used at portal lifespan startup ONLY in production-stripe mode.
    Raises ``RuntimeError`` with the offending file path so the failure
    message points the operator straight at the template to fix.
    """
    root = templates_dir or _TEMPLATES_DIR
    if not root.exists():
        # Unit-test app construction may import without the templates dir
        # mounted; fail open here, the integration test will catch real
        # regressions.
        return

    offenders: list[tuple[pathlib.Path, str]] = []
    for path in sorted(root.rglob("*.html")):
        # `*_mock.html` templates are explicitly mock-mode-only and
        # are never selected by route handlers when payment_provider=
        # 'stripe'. The naming convention is the doctrine: a template
        # whose filename ends in `_mock.html` may carry mock affordances
        # (card_number input, etc.) without tripping the PCI guard;
        # any other template file in production-stripe mode that
        # contains a PAN input is a doctrine bug.
        if path.name.endswith("_mock.html"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pat in _PAN_INPUT_PATTERNS:
            m = pat.search(text)
            if m:
                offenders.append((path, m.group(0)))
                break  # one hit per file is enough

    if offenders:
        msg_lines = [
            "PCI scope guard: refuses to boot in BSS_ENV=production + "
            "BSS_PAYMENT_PROVIDER=stripe with card-number inputs in "
            "templates. PAN must NEVER touch BSS in production; the "
            "portal must use Stripe.js + Elements client-side instead.",
            "",
            "Offending templates:",
        ]
        for path, snippet in offenders:
            msg_lines.append(f"  {path.relative_to(root)} → {snippet[:80]}")
        msg_lines.extend(
            [
                "",
                "Fix: remove the card_number/cardNumber/pan input from "
                "these templates and let Stripe.js mount the Elements "
                "iframe instead. See DECISIONS 2026-05-03 (PCI scope).",
            ]
        )
        raise RuntimeError("\n".join(msg_lines))
