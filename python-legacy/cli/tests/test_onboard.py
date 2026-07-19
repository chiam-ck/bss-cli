"""``bss onboard`` — round-trip + atomic write tests.

Doesn't drive the interactive Typer session (that would require pty
work that's out of scope for v0.14). Instead exercises the underlying
``read_env_file`` / ``write_env_file`` helpers directly. These are the
parts that have correctness risk — the prompts are thin Rich shims.
"""

from __future__ import annotations

from bss_cli.commands.onboard import read_env_file, write_env_file


def test_read_env_empty_file_returns_empty_dict(tmp_path):
    f = tmp_path / "empty.env"
    f.write_text("")
    assert read_env_file(f) == {}


def test_read_env_missing_file_returns_empty_dict(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_read_env_skips_comments_and_blanks(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# this is a comment\n\nFOO=bar\n# another\nBAZ=qux\n")
    env = read_env_file(f)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_read_env_strips_surrounding_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text('FOO="hello world"\nBAR=\'single quoted\'\nBAZ=plain\n')
    env = read_env_file(f)
    assert env == {
        "FOO": "hello world",
        "BAR": "single quoted",
        "BAZ": "plain",
    }


def test_write_env_creates_file_when_missing(tmp_path):
    f = tmp_path / ".env"
    write_env_file(f, {"FOO": "bar"})
    out = f.read_text()
    assert "FOO=bar" in out
    assert "# Added by `bss onboard`" in out


def test_write_env_preserves_comments_and_existing_order(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# Header comment\n"
        "FOO=old\n"
        "\n"
        "# Section comment\n"
        "BAR=baz\n"
    )
    write_env_file(f, {"FOO": "new", "BAR": "baz"})
    out = f.read_text()
    # Comments + ordering preserved; FOO updated in place.
    assert out.startswith("# Header comment\n")
    assert "FOO=new" in out
    assert "BAR=baz" in out
    assert "# Section comment" in out
    # No duplicate FOO line.
    assert out.count("FOO=") == 1


def test_write_env_appends_new_keys_at_end(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FOO=bar\n")
    write_env_file(f, {"FOO": "bar", "NEW_KEY": "added"})
    out = f.read_text()
    assert out.startswith("FOO=bar")
    assert "NEW_KEY=added" in out
    # Header for new keys is the marker line.
    assert "# Added by `bss onboard`" in out


def test_write_env_quotes_values_with_spaces(tmp_path):
    f = tmp_path / ".env"
    write_env_file(f, {"BSS_PORTAL_EMAIL_FROM": "BSS-CLI <noreply@x>"})
    out = f.read_text()
    assert 'BSS_PORTAL_EMAIL_FROM="BSS-CLI <noreply@x>"' in out


def test_write_env_atomic_replaces_existing_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("PRE_EXISTING=yes\n")
    write_env_file(f, {"PRE_EXISTING": "no", "ANOTHER": "value"})
    # Atomic replacement → no .env.tmp leftover.
    assert not (tmp_path / ".env.tmp").exists()
    env = read_env_file(f)
    assert env["PRE_EXISTING"] == "no"
    assert env["ANOTHER"] == "value"


def test_round_trip_resend_block(tmp_path):
    """Realistic case: an existing .env with non-email config, then
    onboard adds the four Resend env vars without touching the rest."""
    f = tmp_path / ".env"
    f.write_text(
        "# BSS-CLI dev .env\n"
        "BSS_DB_URL=postgresql+asyncpg://bss:bss@db:5432/bss\n"
        "BSS_API_TOKEN=0123456789abcdef0123456789abcdef\n"
    )
    env = read_env_file(f)
    env["BSS_PORTAL_EMAIL_PROVIDER"] = "resend"
    env["BSS_PORTAL_EMAIL_RESEND_API_KEY"] = "re_test_xxxxxxxxx"
    env["BSS_PORTAL_EMAIL_FROM"] = "BSS-CLI <noreply@mail.example.com>"
    env["BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET"] = "whsec_xxx"
    write_env_file(f, env)
    out = f.read_text()
    assert "BSS_DB_URL=" in out
    assert "BSS_API_TOKEN=" in out
    assert "BSS_PORTAL_EMAIL_PROVIDER=resend" in out
    assert "BSS_PORTAL_EMAIL_RESEND_API_KEY=re_test_xxxxxxxxx" in out
    assert 'BSS_PORTAL_EMAIL_FROM="BSS-CLI <noreply@mail.example.com>"' in out
    assert "# BSS-CLI dev .env" in out  # comment preserved


def test_unknown_domain_aborts():
    """Passing --domain=nonsense should exit cleanly."""
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["--domain", "nonsense"])
    assert result.exit_code == 2


# ── v0.15: backup safeguard + kyc domain ──────────────────────────────


def test_write_env_returns_none_when_no_existing_file(tmp_path):
    f = tmp_path / ".env"
    backup = write_env_file(f, {"FOO": "bar"})
    assert backup is None


def test_write_env_creates_timestamped_backup_before_rename(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OLD=value\n")
    backup = write_env_file(f, {"OLD": "newvalue", "NEW": "x"})
    assert backup is not None
    assert backup.exists()
    assert backup.name.startswith(".env.backup-")
    # Naming pattern: .env.backup-YYYY-MM-DD-HHMMSS
    import re
    suffix = backup.name.removeprefix(".env.backup-")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}", suffix), suffix
    # Backup carries the OLD value, the live .env carries the new.
    assert "OLD=value" in backup.read_text()
    assert "OLD=newvalue" in f.read_text()


def test_prune_keeps_last_5_wizard_backups(tmp_path):
    from bss_cli.commands.onboard import (
        BACKUP_RETENTION_COUNT,
        _prune_old_backups,
    )

    # Seed 7 timestamped backups + a hand-named one (no -HHMMSS suffix).
    timestamps = [
        "2026-04-01-100000",
        "2026-04-01-110000",
        "2026-04-02-100000",
        "2026-04-03-100000",
        "2026-04-04-100000",
        "2026-04-05-100000",
        "2026-04-06-100000",
    ]
    for ts in timestamps:
        (tmp_path / f".env.backup-{ts}").write_text(f"# {ts}\n")
    handnamed = tmp_path / ".env.backup-2026-05-02"
    handnamed.write_text("# hand-edited\n")

    _prune_old_backups(tmp_path)

    surviving = sorted(p.name for p in tmp_path.glob(".env.backup-*"))
    # Hand-named always survives.
    assert ".env.backup-2026-05-02" in surviving
    # Last 5 timestamped survive (the ones with greatest sort order).
    expected_kept_ts = sorted(timestamps, reverse=True)[:BACKUP_RETENTION_COUNT]
    for ts in expected_kept_ts:
        assert f".env.backup-{ts}" in surviving
    # Older timestamped pruned.
    expected_dropped_ts = sorted(timestamps, reverse=True)[BACKUP_RETENTION_COUNT:]
    for ts in expected_dropped_ts:
        assert f".env.backup-{ts}" not in surviving


def test_kyc_test_mode_drops_didit_creds(tmp_path):
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    f.write_text(
        "BSS_PORTAL_KYC_PROVIDER=didit\n"
        "BSS_PORTAL_KYC_DIDIT_API_KEY=k_test_old\n"
        "BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID=00000000-0000-0000-0000-000000000000\n"
        "BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET=secret_old\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--domain", "kyc", "--env-path", str(f)],
        input="test\n",
    )
    assert result.exit_code == 0, result.output
    env = read_env_file(f)
    assert env["BSS_PORTAL_KYC_PROVIDER"] == "prebaked"
    # Stale Didit creds removed.
    assert "BSS_PORTAL_KYC_DIDIT_API_KEY" not in env
    assert "BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID" not in env
    assert "BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET" not in env


def test_kyc_production_mode_rejects_non_uuid_workflow(tmp_path):
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    runner = CliRunner()
    # Mode=production, then api key, then bad workflow id.
    result = runner.invoke(
        app,
        ["--domain", "kyc", "--env-path", str(f)],
        input="production\nk_test_xxx\nwf_not_a_uuid\n",
    )
    # Should reject the workflow id format and exit 2.
    assert result.exit_code == 2, result.output
    assert "Workflow ID must be a raw UUID" in result.output


def test_email_test_mode_drops_resend_creds(tmp_path, monkeypatch):
    """If operator switches from production back to test mode, stale
    Resend creds must be removed (otherwise a mode-mix is possible)."""
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    f.write_text(
        "BSS_PORTAL_EMAIL_PROVIDER=resend\n"
        "BSS_PORTAL_EMAIL_RESEND_API_KEY=re_test_old\n"
        "BSS_PORTAL_EMAIL_FROM=Old <a@b>\n"
    )
    runner = CliRunner()
    # Choose 'test' mode at the prompt.
    result = runner.invoke(
        app,
        ["--domain", "email", "--env-path", str(f)],
        input="test\n",
    )
    assert result.exit_code == 0, result.output
    env = read_env_file(f)
    assert env["BSS_PORTAL_EMAIL_PROVIDER"] == "logging"
    # Stale API key removed.
    assert "BSS_PORTAL_EMAIL_RESEND_API_KEY" not in env


# ── v0.16 payment domain tests ─────────────────────────────────────


def test_payment_test_mode_drops_stripe_creds(tmp_path):
    """Switching production → test must strip stale Stripe creds."""
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    f.write_text(
        "BSS_PAYMENT_PROVIDER=stripe\n"
        "BSS_PAYMENT_STRIPE_API_KEY=sk_test_old\n"
        "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY=pk_test_old\n"
        "BSS_PAYMENT_STRIPE_WEBHOOK_SECRET=whsec_old\n"
        "BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--domain", "payment", "--env-path", str(f)],
        input="test\n",
    )
    assert result.exit_code == 0, result.output
    env = read_env_file(f)
    assert env["BSS_PAYMENT_PROVIDER"] == "mock"
    assert "BSS_PAYMENT_STRIPE_API_KEY" not in env
    assert "BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY" not in env
    assert "BSS_PAYMENT_STRIPE_WEBHOOK_SECRET" not in env
    assert "BSS_PAYMENT_ALLOW_TEST_CARD_REUSE" not in env


def test_payment_production_mode_rejects_bad_secret_prefix(tmp_path):
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    runner = CliRunner()
    # production → bogus secret key (no sk_ prefix).
    result = runner.invoke(
        app,
        ["--domain", "payment", "--env-path", str(f)],
        input="production\nrandom_garbage_key\n",
    )
    assert result.exit_code == 2, result.output
    assert "must start with 'sk_test_' or 'sk_live_'" in result.output


def test_payment_production_mode_rejects_key_mode_mismatch(tmp_path):
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    runner = CliRunner()
    # production → sk_test secret + pk_live publishable → mismatch.
    result = runner.invoke(
        app,
        ["--domain", "payment", "--env-path", str(f)],
        input="production\nsk_test_xxx\npk_live_yyy\n",
    )
    assert result.exit_code == 2, result.output
    assert "key mode mismatch" in result.output


def test_payment_production_mode_refuses_test_secret_in_production_env(tmp_path):
    from bss_cli.commands.onboard import app
    from typer.testing import CliRunner

    f = tmp_path / ".env"
    f.write_text("BSS_ENV=production\n")
    runner = CliRunner()
    # production → sk_test_xxx + matching pk_test_yyy → refused because
    # BSS_ENV=production was already set in the .env.
    result = runner.invoke(
        app,
        ["--domain", "payment", "--env-path", str(f)],
        input="production\nsk_test_xxx\npk_test_yyy\n",
    )
    assert result.exit_code == 2, result.output
    assert "Refusing to write sk_test_*" in result.output
