from __future__ import annotations

import json

import pytest

from awsorg import agents


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(agents, "awsorg_command", lambda: "/opt/bin/awsorg")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    return h


def install(home, *names, dry=False):
    ch = agents.Changes(dry)
    for n in names:
        agents.install(agents.AGENTS[n], home, ch)
    return ch.lines


def test_claude_regla_hooks_y_deny_conservando_lo_existente(home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PostToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo mio"}]},
            {"matcher": "Bash", "hooks": [{"type": "command",
                                           "command": "/home/x/.local/share/awsorg/hooks/aws-expired.sh"}]},
        ]},
        "permissions": {"deny": ["Bash(rm -rf *)"]},
    }))
    install(home, "claude")
    assert (home / ".claude/rules/aws.md").read_text().startswith("# AWS con awsorg")
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert "echo mio" in cmds
    assert "/opt/bin/awsorg hook post-tool --format claude" in cmds
    assert not any("aws-expired.sh" in c for c in cmds)            # el hook del kit se reemplaza
    assert data["hooks"]["CwdChanged"][0]["hooks"][0]["command"] == "/opt/bin/awsorg hook cwd"
    assert "Bash(rm -rf *)" in data["permissions"]["deny"] and "Bash(awslogin *)" in data["permissions"]["deny"]
    assert data["model"] == "opus"
    before = settings.read_text()
    lines = install(home, "claude")                                  # idempotente
    assert settings.read_text() == before and all("ya al día" in line for line in lines if line.startswith("✓"))


def test_bloque_en_archivo_unico_y_desinstalar(home):
    agents_md = home / ".codex/AGENTS.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text("# Mis reglas\n\nUsa español.\n")
    install(home, "codex")
    text = agents_md.read_text()
    assert text.startswith("# Mis reglas") and agents.MD_BEGIN in text and "awsorg list" in text
    hooks = json.loads((home / ".codex/hooks.json").read_text())
    assert hooks["hooks"]["PostToolUse"][0]["matcher"] == "Bash"
    install(home, "codex")
    assert agents_md.read_text().count(agents.MD_BEGIN) == 1
    ch = agents.Changes(False)
    agents.uninstall(agents.AGENTS["codex"], home, ch)
    assert agents_md.read_text() == "# Mis reglas\n\nUsa español.\n"
    assert "hooks" not in json.loads((home / ".codex/hooks.json").read_text())


def test_kiro_steering_con_frontmatter(home):
    install(home, "kiro")
    text = (home / ".kiro/steering/aws.md").read_text()
    assert text.startswith("---\ninclusion: always\n---")


def test_copilot_y_qwen_y_gemini(home):
    install(home, "copilot", "qwen", "gemini")
    cop = json.loads((home / ".copilot/hooks/awsorg.json").read_text())
    assert cop["version"] == 1 and "PostToolUse" in cop["hooks"]
    assert "--format copilot" in cop["hooks"]["PostToolUse"][0]["bash"]
    qwen = json.loads((home / ".qwen/settings.json").read_text())
    assert qwen["hooks"]["PostToolUse"][0]["matcher"] == "^run_shell_command$"
    gem = json.loads((home / ".gemini/settings.json").read_text())
    assert gem["hooks"]["AfterTool"][0]["matcher"] == "run_shell_command"
    assert agents.MD_BEGIN in (home / ".gemini/GEMINI.md").read_text()


def test_json_invalido_no_se_toca(home):
    s = home / ".claude/settings.json"
    s.parent.mkdir(parents=True)
    s.write_text("{ // comentario")
    lines = install(home, "claude")
    assert any(line.startswith("✗") for line in lines)
    assert s.read_text() == "{ // comentario"


def test_dry_run_no_escribe(home):
    lines = install(home, "claude", "codex", "opencode", dry=True)
    assert all(line.startswith(("[dry-run]", "!")) for line in lines)
    assert not any(home.rglob("*.md")) and not any(home.rglob("*.json"))


def test_cursor_solo_nota_y_resolve(home):
    lines = install(home, "cursor")
    assert any("Settings → Rules" in line for line in lines)
    with pytest.raises(KeyError):
        agents.resolve(["noexiste"], False, home)
    (home / ".kiro").mkdir()
    assert [a.key for a in agents.resolve([], False, home)] == ["kiro"]


def test_hook_formatos_por_agente(sample, capsys):
    from awsorg import hooks
    expired = "Error when retrieving token from sso: Token has expired and refresh failed"
    # Copilot (camelCase, toolArgs como JSON y salida combinada)
    capsys.readouterr()
    hooks.post_tool("copilot", json.dumps({"toolName": "bash", "toolArgs": '{"command": "aws s3 ls"}',
                                           "toolResult": {"resultType": "failure", "textResultForLlm": expired}}))
    out = json.loads(capsys.readouterr().out)
    assert "awslogin" in out["additionalContext"]
    # Gemini (salida combinada: no salta con el genérico ExpiredToken de datos legítimos)
    hooks.post_tool("gemini", json.dumps({"tool_name": "run_shell_command",
                                          "tool_input": {"command": "aws cloudtrail lookup-events"},
                                          "tool_response": {"llmContent": '{"EventName":"ExpiredTokenException"}'}}))
    assert capsys.readouterr().out == ""
    hooks.post_tool("gemini", json.dumps({"tool_name": "run_shell_command", "tool_input": {"command": "aws s3 ls"},
                                          "tool_response": {"llmContent": expired}}))
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["hookEventName"] == "AfterTool"
