"""Terminal output conventions: copyable fixes, one dry-run footer, short sync summaries, plans that only list what exists."""

from __future__ import annotations

import json

import pytest

from rolehop import agents, doctor, shellrc


@pytest.fixture
def quiet_machine(monkeypatch):
    """No AI agents or direnv on PATH, so only what a test creates shows up."""
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")


def test_confirmation_without_terminal_prints_the_same_command_with_yes(sample, monkeypatch, capsys):
    cli, p = sample
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["remove", "acme-prod"]) == 1
    out, err = capsys.readouterr()
    assert "→ run: rolehop remove acme-prod --yes" in err
    assert "comments are not kept" in out  # the orgs.toml note comes before the question, not after writing


@pytest.mark.parametrize(("argv", "fix"), [
    (["sync", "--dry-run"], "rolehop sync"),
    (["sync", "--prune", "--dry-run"], "rolehop sync --prune"),
    (["remove", "acme-prod", "--dry-run"], "rolehop remove acme-prod"),
])
def test_dry_run_footer_is_shared_and_rebuilds_the_command(sample, capsys, argv, fix):
    cli, _p = sample
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.endswith(f"(dry run: nothing was written)\n  → run: {fix}\n")


def test_setup_dry_run_ends_with_the_footer_not_done(sample, quiet_machine, capsys):
    cli, p = sample
    assert cli.main(["setup", "--dry-run", "--no-agents"]) == 0
    out = capsys.readouterr().out
    assert "==> Done" not in out and "sign in" not in out
    assert "[dry-run] would update" in out and out.endswith("  → run: rolehop setup --no-agents\n")
    assert not p.aws.exists() and not (p.home / ".zshrc").exists()


def test_setup_dry_run_without_config(env, quiet_machine, capsys):
    cli, _p = env
    assert cli.main(["setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "setup will start the wizard" in out and out.endswith("  → run: rolehop setup\n")


def test_sync_prints_a_summary_and_the_diff_only_on_request(sample, capsys):
    cli, p = sample
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "+ acme: added · profiles: 3" in out and "@@" not in out
    assert "Updated" in out and "Profiles: 3, organizations: 1." in out
    p.orgs.write_text(p.orgs.read_text().replace('roles = ["admin"]', 'roles = ["admin", "ro"]'))
    assert cli.main(["sync", "--diff"]) == 0
    out = capsys.readouterr().out
    assert "~ acme: updated · profiles: 4" in out and "@@" in out and "+[profile acme-prod-ro]" in out
    p.orgs.write_text(p.orgs.read_text().replace('roles = ["admin", "ro"]', 'roles = ["admin"]'))
    assert cli.main(["sync", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "~ acme" not in out and "@@" not in out and "Updated" in out


def test_uninstall_plan_lists_only_what_exists(sample, quiet_machine, capsys):
    cli, p = sample
    assert cli.main(["sync", "--quiet"]) == 0
    capsys.readouterr()
    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "profile blocks in" in out and "acme" in out
    for absent in ("AI agents", "shell integration", "cached SSO tokens", "local state"):
        assert absent not in out
    agents.install(agents.AGENTS["kiro"], p.home, agents.Changes(False))
    shellrc.ensure_shell_integration(home=p.home, shell="zsh")
    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "rules and hooks of these AI agents: Kiro" in out and "shell integration block (PATH and direnv) in: ~/.zshrc" in out
    assert cli.main(["uninstall", "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["uninstall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Nothing to uninstall" in out and "uninstall rolehop" in out


def test_uninstall_lists_claude_when_only_its_hooks_are_left(sample, quiet_machine, capsys):
    cli, p = sample
    agents.install(agents.AGENTS["claude"], p.home, agents.Changes(False))
    (p.home / ".claude/rules/aws.md").unlink()  # rules removed by hand, hooks still there
    assert agents.has_leftovers(agents.AGENTS["claude"], p.home)
    assert cli.main(["uninstall", "--dry-run"]) == 0
    assert "AI agents: Claude Code" in capsys.readouterr().out


def test_kiro_note_is_a_dim_hint(sample, quiet_machine, capsys):
    cli, _p = sample
    assert cli.main(["agent", "install", "kiro"]) == 0
    out = capsys.readouterr().out
    assert "  Kiro: rules only" in out and "! Kiro" not in out


def test_doctor_reports_missing_direnv_as_info(sample, quiet_machine, monkeypatch, capsys):
    cli, _p = sample
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "aws_cli_version", lambda: (2, 30, 1))
    cli.main(["doctor"])
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("· direnv") for line in lines) and not any(line.startswith("✓ direnv") for line in lines)
    cli.main(["doctor", "--json"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["direnv"]["status"] == "info" and checks["direnv"]["fix"] == ""


def test_init_without_terminal_fails_before_the_menu(env, monkeypatch, capsys):
    cli, _p = env
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["init"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "interactive terminal" in err and "→ open a terminal and run: rolehop init" in err


def test_translated_dry_run_prefix(sample, quiet_machine, capsys):
    cli, _p = sample
    assert cli.main(["--lang", "es", "agent", "install", "kiro", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[simulación]" in out and "[dry-run]" not in out and "→ ejecuta: rolehop agent install kiro" in out
