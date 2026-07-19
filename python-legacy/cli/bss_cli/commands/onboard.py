"""``bss onboard`` — first-run provider configuration wizard (v0.14+).

The SaaS-onboarding feel without being a SaaS. Reads ``.env`` if
present, prompts only for missing/changed values, validates each
provider with a probe call before saving, writes back atomically
(``.env.tmp`` → rename) preserving comments + ordering.

v0.14 ships ``--domain email`` only. v0.15 adds ``--domain kyc``;
v0.16 adds ``--domain payment``. The wizard is re-runnable per-domain
so an operator can swap providers post-first-run without re-prompting
everything.

Why ``.env`` not ``settings.toml``: secrets, per the v0.13 doctrine
("Don't store secrets in ``settings.toml``"). The provider-name
selector (``BSS_<DOMAIN>_PROVIDER``) is technically non-secret, but
keeping it adjacent to its API key in ``.env`` is the pragmatic call.

Doctrine reminders:

* Fail-fast on probe failure — never write `.env` if the API key
  doesn't actually work. Ops would only discover the misconfig
  later when an OTP failed to send.
* Refuse to proceed if ``BSS_ENV=production`` is set and a known-
  test API key is provided (e.g. ``re_test_*``, ``sk_test_*``).
  v0.14 only checks Resend; v0.15/v0.16 extend the same pattern.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich import print as rprint
from rich.prompt import Confirm, Prompt

app = typer.Typer(
    name="onboard",
    help="Configure real-provider integrations (email, KYC, payment).",
    no_args_is_help=False,
    invoke_without_command=True,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_ENV_PATH: Final[Path] = _REPO_ROOT / ".env"

DOMAIN_EMAIL: Final[str] = "email"
DOMAIN_KYC: Final[str] = "kyc"          # v0.15+
DOMAIN_PAYMENT: Final[str] = "payment"  # v0.16+

ALL_DOMAINS_V014: Final[tuple[str, ...]] = (DOMAIN_EMAIL,)
ALL_DOMAINS_V015: Final[tuple[str, ...]] = (DOMAIN_EMAIL, DOMAIN_KYC)
ALL_DOMAINS_V016: Final[tuple[str, ...]] = (DOMAIN_EMAIL, DOMAIN_KYC, DOMAIN_PAYMENT)

# How many wizard-written .env.backup-* files to keep. Hand-named
# backups (no -HHMMSS suffix) are NOT matched and never touched.
BACKUP_RETENTION_COUNT: Final[int] = 5
BACKUP_FILENAME_GLOB: Final[str] = ".env.backup-????-??-??-??????"

DIDIT_DEFAULT_JWKS_URL: Final[str] = (
    "https://verification.didit.me/.well-known/jwks.json"
)


@app.callback()
def onboard_callback(
    ctx: typer.Context,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            "-d",
            help=(
                "Configure a single domain (email | kyc | payment). "
                "Default: walk all v0.14-supported domains."
            ),
        ),
    ] = None,
    env_path: Annotated[
        Path | None,
        typer.Option(
            "--env-path",
            help="Path to the .env file. Defaults to <repo-root>/.env.",
            envvar="BSS_ONBOARD_ENV_PATH",
        ),
    ] = None,
) -> None:
    """Walk the operator through provider configuration."""
    # Subcommand explicitly requested? Let it run.
    if ctx.invoked_subcommand is not None:
        return

    target = env_path or _ENV_PATH
    rprint(f"[green]Welcome to BSS-CLI.[/] Configuring [bold]{target}[/].\n")

    domains = _domains_to_configure(domain)
    env = read_env_file(target)

    for d in domains:
        rprint(f"\n[bold]── {d.upper()} ──[/]")
        if d == DOMAIN_EMAIL:
            _configure_email(env)
        elif d == DOMAIN_KYC:
            _configure_kyc(env)
        elif d == DOMAIN_PAYMENT:
            _configure_payment(env)
        else:
            rprint(f"[red]Unknown domain: {d!r}[/]")
            raise typer.Exit(code=2)

    backup_path = write_env_file(target, env)
    rprint(
        f"\n[green]Wrote {target}.[/] "
        "Restart services with: docker compose down && docker compose up -d"
    )
    if backup_path is not None:
        rprint(
            f"[dim]Previous .env backed up to {backup_path}. "
            f"Keeps the last {BACKUP_RETENTION_COUNT} wizard backups; "
            "older are pruned. Hand-named backups are never touched.[/]"
        )


# ── per-domain prompts ──────────────────────────────────────────────


def _configure_email(env: dict[str, str]) -> None:
    """Resend (v0.14) prompts. Mode 1 = test (logging dev mailbox);
    mode 2 = production (Resend)."""
    current = env.get("BSS_PORTAL_EMAIL_PROVIDER", "") or env.get(
        "BSS_PORTAL_EMAIL_ADAPTER", ""
    )
    rprint(f"  Current provider: [green]{current or '(unset; defaults to logging)'}[/]")
    mode = Prompt.ask(
        "  Mode",
        choices=["test", "production"],
        default="production" if current == "resend" else "test",
    )

    if mode == "test":
        env["BSS_PORTAL_EMAIL_PROVIDER"] = "logging"
        # Drop any stale Resend creds — leaving them around is a
        # foot-gun ("why did email send to a real user from staging?").
        env.pop("BSS_PORTAL_EMAIL_RESEND_API_KEY", None)
        rprint("  [green]✓[/] Mode set to [green]logging[/] — emails write to dev mailbox.")
        return

    # production = Resend
    env["BSS_PORTAL_EMAIL_PROVIDER"] = "resend"
    api_key = Prompt.ask(
        "  Resend API key (re_...)",
        default=env.get("BSS_PORTAL_EMAIL_RESEND_API_KEY", ""),
        password=True,
    )
    if not api_key.startswith("re_"):
        rprint("  [red]API key must start with 're_'. Aborting.[/]")
        raise typer.Exit(code=2)
    env["BSS_PORTAL_EMAIL_RESEND_API_KEY"] = api_key

    from_addr = Prompt.ask(
        "  Sender address (e.g. 'BSS-CLI <noreply@mail.example.com>')",
        default=env.get("BSS_PORTAL_EMAIL_FROM", ""),
    )
    if not from_addr or "@" not in from_addr:
        rprint("  [red]Sender address must contain an email; aborting.[/]")
        raise typer.Exit(code=2)
    env["BSS_PORTAL_EMAIL_FROM"] = from_addr

    webhook_secret = Prompt.ask(
        "  Resend webhook secret (whsec_...)",
        default=env.get("BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET", ""),
        password=True,
    )
    if webhook_secret and not webhook_secret.startswith("whsec_"):
        rprint(
            "  [yellow]webhook secret usually starts with 'whsec_' — "
            "double-check the dashboard. Saved anyway.[/]"
        )
    env["BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET"] = webhook_secret

    if Confirm.ask("  Probe Resend with a real test send?", default=True):
        recipient = Prompt.ask("    Recipient email (your account email)")
        ok = _probe_resend(api_key=api_key, from_addr=from_addr, recipient=recipient)
        if not ok:
            rprint(
                "  [red]Probe failed. Review the error above and re-run "
                "`bss onboard --domain email`.[/]"
            )
            raise typer.Exit(code=2)
        rprint("  [green]✓[/] Probe send accepted by Resend.")


def _configure_kyc(env: dict[str, str]) -> None:
    """Didit (v0.15) prompts. Mode 1 = test (prebaked); mode 2 = production
    (Didit). The Didit path optionally probes by creating a real sandbox
    session and printing the redirect URL — the operator opens it manually
    to verify end-to-end. The webhook + JWS verification round runs at
    `make scenarios-hero --tag didit-sandbox` post-onboard, not here."""
    current = env.get("BSS_PORTAL_KYC_PROVIDER", "")
    rprint(f"  Current provider: [green]{current or '(unset; defaults to prebaked)'}[/]")
    mode = Prompt.ask(
        "  Mode",
        choices=["test", "production"],
        default="production" if current == "didit" else "test",
    )

    if mode == "test":
        env["BSS_PORTAL_KYC_PROVIDER"] = "prebaked"
        # Drop stale Didit creds. Same foot-gun reasoning as email mode.
        env.pop("BSS_PORTAL_KYC_DIDIT_API_KEY", None)
        env.pop("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID", None)
        env.pop("BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET", None)
        rprint(
            "  [green]✓[/] Mode set to [green]prebaked[/] — "
            "deterministic per-customer attestation, no external calls."
        )
        return

    # production = Didit
    env["BSS_PORTAL_KYC_PROVIDER"] = "didit"

    api_key = Prompt.ask(
        "  Didit API key",
        default=env.get("BSS_PORTAL_KYC_DIDIT_API_KEY", ""),
        password=True,
    )
    if not api_key:
        rprint("  [red]API key is required. Aborting.[/]")
        raise typer.Exit(code=2)
    env["BSS_PORTAL_KYC_DIDIT_API_KEY"] = api_key

    workflow_id = Prompt.ask(
        "  Didit workflow ID (raw UUID, e.g. 7411e1f2-119d-4eee-9b8c-6e759933c2b8)",
        default=env.get("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID", ""),
    )
    # Light validation: must look like a UUID (8-4-4-4-12 hex with dashes).
    import re

    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", workflow_id):
        rprint(
            "  [red]Workflow ID must be a raw UUID (8-4-4-4-12 hex). "
            "Didit's dashboard returns it without a 'wf_' prefix.[/]"
        )
        raise typer.Exit(code=2)
    env["BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID"] = workflow_id

    webhook_secret = Prompt.ask(
        "  Didit webhook secret",
        default=env.get("BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET", ""),
        password=True,
    )
    if not webhook_secret:
        rprint(
            "  [red]Webhook secret is required — it's the trust anchor "
            "for v0.15 KYC (HMAC verifies inbound webhooks, which write "
            "the corroboration row the BSS policy reads).[/]"
        )
        raise typer.Exit(code=2)
    env["BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET"] = webhook_secret

    if Confirm.ask(
        "  Probe Didit by creating a real sandbox session?", default=True
    ):
        ok = _probe_didit_session(
            api_key=api_key, workflow_id=workflow_id
        )
        if not ok:
            rprint(
                "  [red]Probe failed. Review the error above and re-run "
                "`bss onboard --domain kyc`.[/]"
            )
            raise typer.Exit(code=2)
        rprint(
            "  [green]✓[/] Didit accepted the sandbox session. The redirect "
            "URL was printed above — open it to validate end-to-end. "
            "BSS-side webhook + corroboration round runs separately."
        )


def _probe_didit_session(*, api_key: str, workflow_id: str) -> bool:
    """Try a single Didit POST /v2/session/. Return ``True`` on success."""
    try:
        import httpx
    except ImportError:
        rprint("  [red]The `httpx` package isn't installed. Run `uv sync`.[/]")
        return False

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                "https://verification.didit.me/v2/session/",
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "workflow_id": workflow_id,
                    "vendor_data": "bss-cli-onboard-probe",
                },
            )
            if resp.status_code != 201:
                rprint(
                    f"  [red]Didit returned {resp.status_code}: {resp.text}[/]"
                )
                return False
            body = resp.json()
    except Exception as exc:  # noqa: BLE001
        rprint(f"  [red]Didit probe failed: {exc}[/]")
        return False

    rprint(
        f"    Didit accepted: session_id=[green]{body.get('session_id')}[/]\n"
        f"    Open this URL to walk the hosted UI:\n"
        f"    [green]{body.get('url')}[/]"
    )
    return True


def _configure_payment(env: dict[str, str]) -> None:
    """Stripe (v0.16) prompts. Mode 1 = test (mock tokenizer);
    mode 2 = production (Stripe). The Stripe path probes by calling
    Account.retrieve — confirms the key works without spending money or
    creating customers."""
    current = env.get("BSS_PAYMENT_PROVIDER", "")
    rprint(f"  Current provider: [green]{current or '(unset; defaults to mock)'}[/]")
    mode = Prompt.ask(
        "  Mode",
        choices=["test", "production"],
        default="production" if current == "stripe" else "test",
    )

    if mode == "test":
        env["BSS_PAYMENT_PROVIDER"] = "mock"
        # Drop stale Stripe creds so the test deployment doesn't
        # silently carry production keys around. Same foot-gun
        # reasoning as email + KYC.
        env.pop("BSS_PAYMENT_STRIPE_API_KEY", None)
        env.pop("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY", None)
        env.pop("BSS_PAYMENT_STRIPE_WEBHOOK_SECRET", None)
        env.pop("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE", None)
        rprint(
            "  [green]✓[/] Mode set to [green]mock[/] — "
            "in-process tokenizer, no external calls. "
            "Hero scenarios use this mode."
        )
        return

    # production = Stripe
    env["BSS_PAYMENT_PROVIDER"] = "stripe"

    api_key = Prompt.ask(
        "  Stripe secret key (sk_test_... for sandbox, sk_live_*** for production)",
        default=env.get("BSS_PAYMENT_STRIPE_API_KEY", ""),
        password=True,
    )
    if not (api_key.startswith("sk_test_") or api_key.startswith("sk_live_")):
        rprint(
            "  [red]Secret key must start with 'sk_test_' or 'sk_live_'. Aborting.[/]"
        )
        raise typer.Exit(code=2)
    env["BSS_PAYMENT_STRIPE_API_KEY"] = api_key
    is_test_secret = api_key.startswith("sk_test_")

    publishable_key = Prompt.ask(
        "  Stripe publishable key (pk_test_... or pk_live_...)",
        default=env.get("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY", ""),
    )
    if not (
        publishable_key.startswith("pk_test_")
        or publishable_key.startswith("pk_live_")
    ):
        rprint(
            "  [red]Publishable key must start with 'pk_test_' or 'pk_live_'. Aborting.[/]"
        )
        raise typer.Exit(code=2)
    is_test_pub = publishable_key.startswith("pk_test_")
    if is_test_secret != is_test_pub:
        rprint(
            "  [red]Stripe key mode mismatch — secret and publishable keys "
            "must both be test (sk_test_/pk_test_) or both live "
            "(sk_live_/pk_live_).[/]"
        )
        raise typer.Exit(code=2)
    env["BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY"] = publishable_key

    bss_env = env.get("BSS_ENV", "development")
    if bss_env == "production" and is_test_secret:
        rprint(
            "  [red]Refusing to write sk_test_* with BSS_ENV=production. "
            "Production must use sk_live_*; sandbox testing must use "
            "BSS_ENV=staging or development.[/]"
        )
        raise typer.Exit(code=2)
    if bss_env != "production" and not is_test_secret:
        rprint(
            "  [yellow]Warning:[/] live Stripe keys (sk_live_*) configured "
            f"with BSS_ENV={bss_env!r}. Real card charges will hit your "
            "Stripe account — make sure this is intentional."
        )

    webhook_secret = Prompt.ask(
        "  Stripe webhook signing secret (whsec_...)",
        default=env.get("BSS_PAYMENT_STRIPE_WEBHOOK_SECRET", ""),
        password=True,
    )
    if not webhook_secret.startswith("whsec_"):
        rprint(
            "  [red]Webhook secret must start with 'whsec_'. "
            "Get it from Stripe Dashboard → Developers → Webhooks → "
            "your endpoint → Signing secret. Aborting.[/]"
        )
        raise typer.Exit(code=2)
    env["BSS_PAYMENT_STRIPE_WEBHOOK_SECRET"] = webhook_secret

    if is_test_secret:
        # Sandbox affordance — preserved across re-runs to stay consistent
        # with Track 1 select_tokenizer guards (refuses with sk_live_*).
        if Confirm.ask(
            "  Enable BSS_PAYMENT_ALLOW_TEST_CARD_REUSE? "
            "(sandbox-only; lets the same Stripe test pm_* re-attach to "
            "different BSS customers — required if you'll signup multiple "
            "test customers using `pm_card_visa`)",
            default=env.get("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE", "").lower()
            == "true",
        ):
            env["BSS_PAYMENT_ALLOW_TEST_CARD_REUSE"] = "true"
        else:
            env.pop("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE", None)
    else:
        # Live keys — strip the flag if it's there. select_tokenizer
        # would refuse to start otherwise.
        env.pop("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE", None)

    if Confirm.ask(
        "  Probe Stripe by calling Account.retrieve?", default=True
    ):
        ok = _probe_stripe(api_key=api_key)
        if not ok:
            rprint(
                "  [red]Probe failed. Review the error above and re-run "
                "`bss onboard --domain payment`.[/]"
            )
            raise typer.Exit(code=2)
        rprint("  [green]✓[/] Stripe accepted the key.")

    rprint(
        "  [dim]Reminder: cut over saved cards before flipping the env "
        "var in production — see [green]docs/runbooks/stripe-cutover.md[/].[/]"
    )


def _probe_stripe(*, api_key: str) -> bool:
    """Try a single stripe.Account.retrieve. Return True on success.

    Account.retrieve costs nothing and creates nothing — it just
    confirms the key is valid + tells us the account id.
    """
    try:
        import stripe  # type: ignore[import-not-found]
    except ImportError:
        rprint("  [red]The `stripe` SDK isn't installed. Run `uv sync`.[/]")
        return False

    try:
        account = stripe.Account.retrieve(api_key=api_key)
    except Exception as exc:  # noqa: BLE001 — surface SDK error verbatim
        rprint(f"  [red]Stripe rejected the probe: {exc}[/]")
        return False
    acct_id = (
        account.get("id")
        if isinstance(account, dict)
        else getattr(account, "id", "?")
    )
    rprint(f"    Stripe account: id=[green]{acct_id}[/]")
    return True


def _probe_resend(*, api_key: str, from_addr: str, recipient: str) -> bool:
    """Try a single Resend send. Return ``True`` on success."""
    try:
        import resend  # type: ignore[import-not-found]
    except ImportError:
        rprint("  [red]The `resend` SDK isn't installed. Run `uv sync`.[/]")
        return False

    resend.api_key = api_key
    try:
        result = resend.Emails.send(
            {
                "from": from_addr,
                "to": [recipient],
                "subject": "BSS-CLI onboard probe",
                "html": "<p>If you see this, Resend is configured correctly.</p>",
                "text": "If you see this, Resend is configured correctly.",
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface any SDK error verbatim
        rprint(f"  [red]Resend rejected the probe: {exc}[/]")
        return False
    msg_id = (
        result.get("id") if isinstance(result, dict) else getattr(result, "id", "?")
    )
    rprint(f"    Resend accepted: id=[green]{msg_id}[/]")
    return True


# ── .env round-trip helpers (preserves comments + ordering) ─────────


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` into a dict. Missing file → empty dict.

    Lines that aren't ``key=value`` (comments, blanks) are dropped from
    the dict but preserved on disk by ``write_env_file``. Quoted values
    are stripped of surrounding ``"`` or ``'``.
    """
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        env[key] = value
    return env


def write_env_file(path: Path, env: dict[str, str]) -> Path | None:
    """Write ``env`` back atomically, preserving comments + ordering of
    keys that already existed; new keys are appended at the end.

    v0.15 — before the atomic rename, copy the existing ``.env`` to a
    timestamped ``.env.backup-YYYY-MM-DD-HHMMSS`` (using ``shutil.copy2``
    to preserve mtime + permissions). After the write, prune backups
    matching ``.env.backup-????-??-??-??????`` to the most recent
    ``BACKUP_RETENTION_COUNT``. Hand-named backups (no ``-HHMMSS``
    suffix) are NOT matched and never touched. Returns the backup path
    (or ``None`` if the original file did not exist).

    Contract: ``env`` is the COMPLETE post-state. Keys present in the
    existing file but absent from ``env`` are removed (the wizard
    explicitly pops stale credentials when switching modes; preserving
    them silently would leave a foot-gun).
    """
    backup_path: Path | None = None
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
        # v0.15 belt-and-suspenders: timestamped backup before rename.
        # ``os.replace`` is atomic against process crashes; the backup
        # is the recourse against logic bugs in this function.
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")  # noqa: bss-clock
        backup_path = path.parent / f"{path.name}.backup-{timestamp}"
        shutil.copy2(path, backup_path)
    else:
        existing_lines = []

    written_keys: set[str] = set()
    new_lines: list[str] = []
    for raw in existing_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            new_lines.append(raw)
            continue
        key, sep, _ = line.partition("=")
        key = key.strip()
        if not sep:
            new_lines.append(raw)
            continue
        if key in env:
            new_lines.append(f"{key}={_quote_if_needed(env[key])}")
            written_keys.add(key)
        # else: key was deleted from env (wizard popped it). Drop the line.

    # New keys (set in this run, didn't exist before) → append.
    new_keys = [k for k in env if k not in written_keys]
    if new_keys:
        new_lines.append("")
        new_lines.append("# Added by `bss onboard` (v0.14+)")
        for k in new_keys:
            new_lines.append(f"{k}={_quote_if_needed(env[k])}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    # Prune older wizard-written backups to the retention count.
    if backup_path is not None:
        _prune_old_backups(path.parent)

    return backup_path


def _prune_old_backups(directory: Path) -> None:
    """Keep only the ``BACKUP_RETENTION_COUNT`` most recent
    ``.env.backup-YYYY-MM-DD-HHMMSS`` files; older ones are deleted.

    The glob is timestamp-shaped so hand-named backups
    (e.g. ``.env.backup-2026-05-02`` without ``-HHMMSS``) are never
    matched and never deleted.
    """
    backups = sorted(
        directory.glob(BACKUP_FILENAME_GLOB),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in backups[BACKUP_RETENTION_COUNT:]:
        try:
            stale.unlink()
        except OSError:
            # Permissions, race, etc. — non-fatal; the next wizard run
            # will retry the prune.
            pass


def _quote_if_needed(value: str) -> str:
    """Wrap value in double quotes if it contains spaces or special chars.

    Keeps simple unquoted values readable; matches the existing ``.env``
    convention (most lines unquoted; sender envelope with display name
    is quoted).
    """
    needs_quote = any(c in value for c in (" ", "\t", "#", "$", "'", "`"))
    if needs_quote:
        # Escape any embedded double quotes.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


# ── helpers ─────────────────────────────────────────────────────────


def _domains_to_configure(domain: str | None) -> tuple[str, ...]:
    if domain is None:
        return ALL_DOMAINS_V016
    d = domain.lower()
    if d in (DOMAIN_EMAIL, DOMAIN_KYC, DOMAIN_PAYMENT):
        return (d,)
    rprint(
        f"[red]Unknown --domain {domain!r}. "
        "Expected: email (v0.14), kyc (v0.15+), payment (v0.16+).[/]"
    )
    sys.exit(2)


# Re-export the underlying functions so tests can drive the round-trip
# without simulating an interactive Typer session.
__all__ = [
    "app",
    "read_env_file",
    "write_env_file",
    "_prune_old_backups",
    "BACKUP_RETENTION_COUNT",
    "BACKUP_FILENAME_GLOB",
]
