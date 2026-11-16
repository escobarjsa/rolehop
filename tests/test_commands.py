"""CLI commands, session probing, the access portal client, runner, hooks, doctor, completion and debug output.

External programs (`aws`, `direnv`, `herdr`) are replaced with fakes; nothing touches the network or the real HOME.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import urllib.error

import pytest
from test_wizard_discover import Portal, portal  # noqa: F401  (reused fixture)

from conftest import SAMPLE
from rolehop import agents, cli, completion, config, doctor, errors, hooks, paths, runner, session, ui
from rolehop import portal as portal_api


def completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_agents(monkeypatch):
    """No AI agent binaries on PATH (the temporary HOME has no agent folders either)."""
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)


# --------------------------------------------------------------------------- session.check_org
def test_check_org_ok_with_estimate(sample, monkeypatch):
    cli_, p = sample
    calls = []
    monkeypatch.setattr(session.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or completed(0, json.dumps({"Account": "111", "Arn": "arn:x"})))
    session.write_state("acme", last_login=(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat())
    r = session.check_org(config.load_orgs()[0])
    assert r["ok"] and r["account"] == "111" and r["probe_profile"] == "acme-devsecops-admin"
    assert 418 <= r["estimated_left_min"] <= 420
    assert calls[0][:3] == ["aws", "sts", "get-caller-identity"] and "acme-devsecops-admin" in calls[0]


@pytest.mark.parametrize("behaviour, expected", [
    (completed(255, stderr="Error when retrieving token from sso: Token has expired and refresh failed"), "expired"),
    (completed(0, stdout="not json"), "not JSON"),
    (FileNotFoundError(), "aws command not found"),
    (subprocess.TimeoutExpired("aws", 30), "timed out"),
])
def test_check_org_failures(sample, monkeypatch, behaviour, expected):
    _, _ = sample

    def fake(cmd, **kw):
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    monkeypatch.setattr(session.subprocess, "run", fake)
    r = session.check_org(config.load_orgs()[0])
    assert not r["ok"]
    assert expected in (r["error"] or "") or (expected == "expired" and r["expired"])


def test_check_org_blocked_state_overrides_cached_credentials(sample, monkeypatch):
    monkeypatch.setattr(session.subprocess, "run", lambda cmd, **kw: completed(0, json.dumps({"Account": "1"})))
    session.write_state("acme", blocked=True)
    r = session.check_org(config.load_orgs()[0])
    assert not r["ok"] and r["expired"] and "expired" in r["error"]


def test_check_org_without_profiles():
    r = session.check_org({"name": "x", "accounts": [], "roles": {}})
    assert not r["ok"] and r["error"] == "no profiles defined"


def test_fmt_left_and_org_of_profile(sample):
    assert session.fmt_left(None) == "no login data" and session.fmt_left(125) == "~2h05m estimated"
    assert session.org_of_profile("acme-prod-admin") == "acme" and session.org_of_profile("nope") is None


# --------------------------------------------------------------------------- status / login / list / envrc
def test_status_text_and_json(sample, monkeypatch, capsys):
    cli_, _ = sample
    monkeypatch.setattr(session, "check_org", lambda org: {"org": org["name"], "ok": True, "account": "111",
                                                            "probe_profile": "acme-devsecops-admin", "estimated_left_min": 90})
    assert cli_.main(["status"]) == 0
    assert "active session · account 111 · ~1h30m estimated" in capsys.readouterr().out
    assert cli_.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["account"] == "111"
    monkeypatch.setattr(session, "check_org", lambda org: {"org": org["name"], "ok": False, "expired": True, "error": "x"})
    assert cli_.main(["status"]) == 1
    assert "no session · run: rolehop login acme" in capsys.readouterr().out


def test_login_only_where_needed(sample, monkeypatch, capsys):
    cli_, _ = sample
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/aws")
    runs = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd: runs.append(cmd) or completed(0))
    states = iter([{"ok": False, "error": "expired"}, {"ok": True, "account": "111"}])
    monkeypatch.setattr(session, "check_org", lambda org: next(states))
    assert cli_.main(["login"]) == 0
    assert runs == [["aws", "sso", "login", "--sso-session", "acme", "--use-device-code"]]
    assert session.read_state("acme")["last_login"] and "logged in · account 111" in capsys.readouterr().out

    monkeypatch.setattr(session, "check_org", lambda org: {"ok": True, "estimated_left_min": 60})
    assert cli_.main(["login"]) == 0
    assert len(runs) == 1 and "already has a session" in capsys.readouterr().out

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd: completed(1))
    assert cli_.main(["login", "--force"]) == 1
    assert "login failed (exit code 1)" in capsys.readouterr().err


def test_login_requires_aws_cli(sample, monkeypatch, capsys):
    cli_, _ = sample
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli_.main(["login"]) == 2
    assert "aws command not found" in capsys.readouterr().err


def test_list_text_json_and_filter(sample, capsys):
    cli_, _ = sample
    assert cli_.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PROFILE") and "acme-prod-admin" in out
    assert cli_.main(["list", "--json", "--org", "acme"]) == 0
    assert {r["profile"] for r in json.loads(capsys.readouterr().out)} == {
        "acme-devsecops-admin", "acme-devsecops-ro", "acme-prod-admin"}


def test_envrc(sample, capsys):
    cli_, _ = sample
    assert cli_.main(["envrc", "acme-prod-admin", "--state", "acme-devsecops-admin", "--state-var", "TF_STATE"]) == 0
    out = capsys.readouterr().out
    assert "export AWS_PROFILE=acme-prod-admin" in out and "export TF_STATE=acme-devsecops-admin" in out
    assert "unset AWS_ACCESS_KEY_ID" in out
    assert cli_.main(["envrc", "nope"]) == 2
    assert cli_.main(["envrc", "acme-prod-admin", "--state-var", "1bad"]) == 2


def test_discover_prints_toml(portal, capsys):  # noqa: F811
    cli_, p = portal
    p.orgs.write_text(SAMPLE)
    assert cli_.main(["discover", "acme"]) == 0
    out = capsys.readouterr().out
    assert '[[org.accounts]]\nname = "devsecops"' in out and '"Administrator", "PowerUser"' in out


def test_mark_and_which_org(sample, capsys):
    cli_, _ = sample
    assert cli_.main(["_mark", "acme", "blocked"]) == 0 and session.read_state("acme")["blocked"]
    assert cli_.main(["_mark", "acme", "clear"]) == 0 and not session.read_state("acme")["blocked"]
    assert cli_.main(["_mark", "unknown-org", "blocked"]) == 0 and not session.state_file("unknown-org").exists()
    assert cli_.main(["_which-org", "acme-prod-admin"]) == 0 and capsys.readouterr().out.strip() == "acme"
    assert cli_.main(["_which-org", "nope"]) == 1


# --------------------------------------------------------------------------- agent / setup
def test_agent_list_install_and_errors(sample, no_agents, capsys):
    cli_, p = sample
    assert cli_.main(["agent", "list", "--json"]) == 0
    assert not any(r["detected"] for r in json.loads(capsys.readouterr().out))
    assert cli_.main(["agent", "list"]) == 0 and capsys.readouterr().out.startswith("AGENT")
    assert cli_.main(["agent", "install"]) == 1  # nothing detected
    assert "No installed AI agents detected" in capsys.readouterr().out
    assert cli_.main(["agent", "install", "nope"]) == 2
    assert cli_.main(["agent", "install", "claude"]) == 0
    assert (p.home / ".claude/rules/aws.md").exists()
    capsys.readouterr()
    assert cli_.main(["agent", "rules"]) == 0 and capsys.readouterr().out.startswith("# AWS with rolehop")


def test_setup_without_config_is_not_interactive(env, no_agents, monkeypatch, capsys):
    cli_, p = env
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli_.main(["setup"]) == 1
    assert "rolehop init" in capsys.readouterr().out
    assert cli_.main(["setup", "--dry-run"]) == 0


def test_setup_with_config(sample, no_agents, monkeypatch, capsys):
    cli_, p = sample
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert cli_.main(["setup", "--no-agents"]) == 0
    out = capsys.readouterr().out
    assert "==> Done" in out and "skipped: --no-agents" in out
    assert "rolehop:acme" in p.aws.read_text() and (p.home / ".zshrc").exists()
    assert cli_.main(["setup"]) == 0
    assert "no agents detected" in capsys.readouterr().out


# --------------------------------------------------------------------------- portal client
def test_cached_token_errors(sample):
    with pytest.raises(errors.PortalError, match="no active session"):
        portal_api.cached_token("acme")
    paths.SSO_CACHE.mkdir(parents=True, exist_ok=True)
    import hashlib
    f = paths.SSO_CACHE / (hashlib.sha1(b"acme").hexdigest() + ".json")
    f.write_text(json.dumps({"expiresAt": "2099-01-01T00:00:00Z"}))
    with pytest.raises(errors.PortalError, match="no token"):
        portal_api.cached_token("acme")
    f.write_text(json.dumps({"accessToken": "t", "expiresAt": "2000-01-01T00:00:00Z"}))
    with pytest.raises(errors.PortalError, match="expired"):
        portal_api.cached_token("acme")
    f.write_text(json.dumps({"accessToken": "t", "expiresAt": "garbage"}))
    assert portal_api.cached_token("acme") == "t"


@pytest.mark.parametrize("exc, message", [
    (urllib.error.HTTPError("u", 401, "x", None, None), "rejected the token"),  # type: ignore[arg-type]
    (urllib.error.HTTPError("u", 500, "x", None, None), "HTTP error 500"),  # type: ignore[arg-type]
    (urllib.error.URLError("down"), "could not reach"),
])
def test_portal_get_errors(monkeypatch, exc, message):
    def boom(*a, **kw):
        raise exc
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(errors.PortalError, match=message):
        portal_api.portal_get("us-east-1", "/assignment/accounts", "token", {})


def test_login_for_discovery_uses_a_temporary_config(sample, monkeypatch):
    seen = {}

    def fake(cmd, env):
        seen["cmd"], seen["config"] = cmd, env["AWS_CONFIG_FILE"]
        seen["content"] = open(env["AWS_CONFIG_FILE"]).read()
        return completed(0)

    monkeypatch.setattr(portal_api.subprocess, "run", fake)
    org = config.load_orgs()[0]
    assert portal_api.login_for_discovery(org)
    assert seen["cmd"][-1] == "--use-device-code" and "[sso-session acme]" in seen["content"]
    import os
    assert not os.path.exists(seen["config"]) and session.read_state("acme")["last_login"]


# --------------------------------------------------------------------------- runner
def test_runner_paths(sample, monkeypatch, capsys):
    _, _ = sample
    assert runner.run([], "soon", False, False, []) == 2
    monkeypatch.setattr(cli, "cmd_login", lambda a: 1)
    assert runner.run([], "1h", True, False, []) == 1
    monkeypatch.setattr(cli, "cmd_login", lambda a: 0)
    monkeypatch.setattr(session, "check_org", lambda org: {"ok": True, "estimated_left_min": None})
    assert runner.run([], "1h", False, False, []) == 75
    assert runner.run([], "1h", False, True, []) == 0
    monkeypatch.setattr(session, "check_org", lambda org: {"ok": True, "estimated_left_min": 60})
    assert runner.run([], "2h", True, False, []) == 75
    assert "Use at most --max" in capsys.readouterr().out


def test_runner_runs_the_command_and_reports_blocked_orgs(sample, monkeypatch, capsys):
    monkeypatch.setattr(session, "check_org", lambda org: {"ok": True, "estimated_left_min": 480})

    def fake(cmd):
        session.write_state("acme", blocked=True)
        return completed(3)

    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.run([], "1h", False, False, ["claude", "-p", "x"]) == 3
    out = capsys.readouterr().out
    assert "The AWS session ended during the work in: acme" in out and "rolehop login acme" in out

    def missing(cmd):
        raise FileNotFoundError
    monkeypatch.setattr(runner.subprocess, "run", missing)
    assert runner.run([], "1h", False, False, ["nope"]) == 127


# --------------------------------------------------------------------------- hooks
def test_cwd_direnv_appends_export(tmp_path, monkeypatch):
    env_file = tmp_path / "claude.env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setattr(hooks.shutil, "which", lambda name: "/usr/bin/direnv")
    monkeypatch.setattr(hooks.subprocess, "run", lambda cmd, **kw: completed(0, "export AWS_PROFILE=x\n"))
    assert hooks.cwd_direnv(json.dumps({"new_cwd": str(tmp_path)})) == 0
    assert env_file.read_text() == "export AWS_PROFILE=x\n"
    assert hooks.cwd_direnv("not json") == 0
    assert hooks.cwd_direnv(json.dumps({"new_cwd": str(tmp_path / "missing")})) == 0


def test_herdr_title(monkeypatch):
    calls = []
    monkeypatch.setenv("HERDR_PANE_ID", "p1")
    monkeypatch.setenv("HERDR_BIN_PATH", "/opt/herdr")
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda args, **kw: calls.append(args))
    hooks._herdr_title("⚠ AWS: run rolehop login acme")
    hooks._herdr_title(None)
    assert calls[0][-3:] == ["--title", "⚠ AWS: run rolehop login acme", "p1"] and "--clear-title" in calls[1]


# --------------------------------------------------------------------------- doctor
def test_doctor_reports_and_fixes(sample, no_agents, monkeypatch, capsys):
    cli_, p = sample
    monkeypatch.setattr(doctor, "aws_cli_version", lambda: (2, 30, 1))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    assert cli_.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ aws-cli          2.30.1" in out and "out of date" in out and "run: rolehop sync" in out
    assert "AWS_ACCESS_KEY_ID" in out and "run: rolehop login acme" in out and "no AI coding agents detected" in out
    assert "AKIAEXAMPLE" not in out  # never echo secrets
    assert cli_.main(["sync", "--quiet"]) == 0
    capsys.readouterr()
    assert cli_.main(["doctor", "--json"]) == 0
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert checks["aws-config"]["status"] == "ok" and checks["orgs.toml"]["status"] == "ok"


def test_doctor_errors(env, no_agents, monkeypatch, capsys):
    cli_, p = env
    monkeypatch.setattr(doctor, "aws_cli_version", lambda: None)
    assert cli_.main(["doctor"]) == 1  # no aws, no orgs.toml
    out = capsys.readouterr().out
    assert "✗ aws-cli" in out and "✗ orgs.toml" in out and "rolehop init" in out
    monkeypatch.setattr(doctor, "aws_cli_version", lambda: (2, 10, 0))
    p.orgs.write_text(SAMPLE)
    p.aws.write_text("[profile acme-prod-admin]\nregion = eu-west-1\n")
    assert cli_.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "older than 2.22.0" in out and "conflict with generated profiles" in out


def test_doctor_online(sample, no_agents, monkeypatch, capsys):
    cli_, _ = sample
    from rolehop import upgrade
    monkeypatch.setattr(doctor, "aws_cli_version", lambda: (2, 30, 0))
    monkeypatch.setattr(session, "check_org", lambda org: {"ok": True, "estimated_left_min": 30})
    monkeypatch.setattr(upgrade, "latest_release", lambda: {"version": "99.0.0"})
    assert cli_.main(["doctor", "--online"]) == 0
    out = capsys.readouterr().out
    assert "active · ~0h30m estimated" in out and "99.0.0 is available" in out


def test_aws_cli_version_parsing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/aws")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda cmd, **kw: completed(0, "aws-cli/2.31.4 Python/3.13.7 Linux/6 exe/x86_64\n"))
    assert doctor.aws_cli_version() == (2, 31, 4)
    monkeypatch.setattr(doctor.subprocess, "run", lambda cmd, **kw: completed(0, "weird"))
    assert doctor.aws_cli_version() == ()
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.aws_cli_version() is None


# --------------------------------------------------------------------------- completion
def test_completion_scripts(sample, capsys):
    cli_, _ = sample
    parser = cli.build_parser()
    sp = completion.spec(parser)
    assert "status" in sp and "hook" not in sp and "_complete" not in sp and "--json" in sp["status"][1]
    for shell in ("bash", "zsh", "fish"):
        assert cli_.main(["completion", shell]) == 0
        script = capsys.readouterr().out
        assert "awslogin" in script and "refresh" in script
    assert "bashcompinit" in completion.zsh_script(parser)
    bash = completion.bash_script(parser)
    probe = bash + '\nCOMP_WORDS=(rolehop sta); COMP_CWORD=1; _rolehop; echo "${COMPREPLY[@]}"\n'
    assert subprocess.run(["bash", "-c", probe], capture_output=True, text=True).stdout.strip() == "status"


def test_dynamic_names(sample, capsys):
    cli_, _ = sample
    assert completion.names("orgs") == ["acme"]
    assert "acme-prod" in completion.names("targets") and "acme-prod-admin" in completion.names("profiles")
    assert completion.names("shells") == ["bash", "zsh", "fish"] and "claude" in completion.names("agents")
    assert cli_.main(["_complete", "orgs"]) == 0 and capsys.readouterr().out == "acme\n"


def test_dynamic_names_without_config(env):
    assert completion.names("orgs") == []


# --------------------------------------------------------------------------- debug output
def test_verbose_prints_debug_without_secrets(sample, monkeypatch, capsys):
    cli_, _ = sample
    monkeypatch.setattr(ui, "VERBOSE", False)
    assert cli_.main(["-v", "sync", "--quiet"]) == 0
    err = capsys.readouterr().err
    assert "[debug] rolehop" in err and "writing" in err
    monkeypatch.setattr(ui, "VERBOSE", False)
    ui.debug("hidden")
    assert capsys.readouterr().err == ""


def test_awslogin_is_a_shortcut_for_rolehop_login(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv=None: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["awslogin", "acme", "--force"])
    assert cli.awslogin_main() == 0
    assert seen == [["login", "acme", "--force"]]
