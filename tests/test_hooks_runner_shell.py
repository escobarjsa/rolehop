from __future__ import annotations

import json

from rolehop import hooks, runner, shellrc


def run_hook(capsys, payload, fmt="claude"):
    capsys.readouterr()
    assert hooks.post_tool(fmt, json.dumps(payload)) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out.startswith("{") else out


EXPIRED = "Error when retrieving token from sso: Token has expired and refresh failed"


def test_hook_detecta_expiracion_y_marca(sample, capsys):
    cli, p = sample
    out = run_hook(capsys, {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                            "tool_input": {"command": "AWS_PROFILE=empresa-prod-admin aws s3 ls"},
                            "error": f"Exit code 255\n{EXPIRED}"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "awslogin empresa" in ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert cli.read_state("empresa")["blocked"] is True


def test_hook_limpia_marca_tras_exito(sample, capsys):
    cli, _ = sample
    cli.write_state("empresa", blocked=True, blocked_at="2026-01-01T00:00:00+00:00")
    out = run_hook(capsys, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                            "tool_input": {"command": "aws s3 ls --profile empresa-prod-admin"},
                            "tool_response": {"stdout": "ok", "stderr": ""}})
    assert out == ""
    st = cli.read_state("empresa")
    assert st["blocked"] is False and st["blocked_at"] is None


def test_hook_sin_falsos_positivos(sample, capsys):
    # Datos legítimos en stdout (CloudTrail) y comandos que no son de AWS.
    assert run_hook(capsys, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "aws cloudtrail lookup-events"},
                             "tool_response": {"stdout": '{"EventName":"ExpiredTokenException"}', "stderr": ""}}) == ""
    assert run_hook(capsys, {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                             "tool_input": {"command": 'grep -n "Token has expired" notas.txt'},
                             "error": "Exit code 1\nToken has expired and refresh failed"}) == ""
    assert run_hook(capsys, {"hook_event_name": "PostToolUse", "tool_name": "Read",
                             "tool_input": {"file_path": "x"}}) == ""


def test_hook_json_roto_no_falla(sample):
    assert hooks.post_tool("claude", "esto no es json") == 0


def test_hook_formato_generico(sample, capsys):
    out = run_hook(capsys, {"tool_name": "shell", "tool_input": {"command": "terraform plan"},
                            "error": EXPIRED}, fmt="text")
    assert "awslogin" in out


def test_parse_minutes():
    assert runner.parse_minutes("90m") == 90
    assert runner.parse_minutes("3h") == 180
    assert runner.parse_minutes("1h30m") == 90
    assert runner.parse_minutes("120") == 120
    assert runner.parse_minutes("ocho") is None
    assert runner.parse_minutes("") is None


def test_run_no_arranca_sin_sesion(sample, capsys, monkeypatch):
    cli, _ = sample
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": False, "estimated_left_min": None})
    assert runner.run([], "3h", False, False, []) == 75
    assert "sin sesión" in capsys.readouterr().out


def test_run_tiempo_insuficiente_y_suficiente(sample, capsys, monkeypatch):
    cli, _ = sample
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": True, "estimated_left_min": 120})
    assert runner.run([], "3h", False, False, []) == 75
    assert "--max 1h45m" in capsys.readouterr().out
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": True, "estimated_left_min": 470})
    assert runner.run([], "6h", False, False, ["true"]) == 0
    assert runner.run([], "1h", False, False, ["false"]) == 1


def test_shellrc_idempotente(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / ".zshrc").write_text("# mío\n")
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: "/usr/bin/direnv" if name == "direnv" else None)
    msgs = shellrc.ensure_shell_integration(home=home, shell="zsh")
    text = (home / ".zshrc").read_text()
    assert text.startswith("# mío\n") and shellrc.MARK_BEGIN in text and ".local/bin" in text
    assert "direnv hook zsh" in text
    assert any("copia de seguridad" in m for m in msgs)
    again = shellrc.ensure_shell_integration(home=home, shell="zsh")
    assert (home / ".zshrc").read_text() == text and "ya tiene" in again[0]


def test_shellrc_fish_y_dry_run(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    msgs = shellrc.ensure_shell_integration(home=home, shell="fish", dry_run=True)
    assert "[dry-run]" in msgs[0] and not (home / ".config/fish/config.fish").exists()
    shellrc.ensure_shell_integration(home=home, shell="fish")
    assert "fish_add_path" in (home / ".config/fish/config.fish").read_text()
