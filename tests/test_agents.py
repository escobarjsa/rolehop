from __future__ import annotations

import json

import pytest

from rolehop import agents


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(agents, "rolehop_command", lambda: "/opt/bin/rolehop")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    return h


def install(home, *names, dry=False):
    ch = agents.Changes(dry)
    for n in names:
        agents.install(agents.AGENTS[n], home, ch)
    return ch.lines


def test_claude_rules_hooks_and_deny_preserve_existing_settings(home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PostToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo mine"}]},
            {"matcher": "Bash", "hooks": [{"type": "command",
                                           "command": "/home/x/.local/share/awsorg/hooks/aws-expired.sh"}]},
        ]},
        "permissions": {"deny": ["Bash(rm -rf *)"]},
    }))
    install(home, "claude")
    assert (home / ".claude/rules/aws.md").read_text().startswith("# AWS with rolehop")
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert "echo mine" in cmds
    assert "/opt/bin/rolehop hook post-tool --format claude" in cmds
    assert not any("aws-expired.sh" in c for c in cmds)            # the old kit hook is replaced
    assert data["hooks"]["CwdChanged"][0]["hooks"][0]["command"] == "/opt/bin/rolehop hook cwd"
    assert "Bash(rm -rf *)" in data["permissions"]["deny"] and "Bash(awslogin *)" in data["permissions"]["deny"]
    assert data["model"] == "opus"
    before = settings.read_text()
    lines = install(home, "claude")                                  # idempotent
    assert settings.read_text() == before and all("already up to date" in line for line in lines if line.startswith("✓"))


def test_block_in_shared_file_and_uninstall(home):
    agents_md = home / ".codex/AGENTS.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text("# My rules\n\nAnswer in Spanish.\n")
    install(home, "codex")
    text = agents_md.read_text()
    assert text.startswith("# My rules") and agents.MD_BEGIN in text and "rolehop list" in text
    hooks = json.loads((home / ".codex/hooks.json").read_text())
    assert hooks["hooks"]["PostToolUse"][0]["matcher"] == "Bash"
    install(home, "codex")
    assert agents_md.read_text().count(agents.MD_BEGIN) == 1
    ch = agents.Changes(False)
    agents.uninstall(agents.AGENTS["codex"], home, ch)
    assert agents_md.read_text() == "# My rules\n\nAnswer in Spanish.\n"
    assert "hooks" not in json.loads((home / ".codex/hooks.json").read_text())


SPANISH_MD_BEGIN = "<!-- >>> rolehop (gestionado por rolehop, no editar) >>> -->"


def test_spanish_marker_block_is_replaced_counted_and_removed(home):
    assert (SPANISH_MD_BEGIN, agents.MD_END) in agents.LEGACY_MD
    agents_md = home / ".codex/AGENTS.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(f"# My rules\n\n{SPANISH_MD_BEGIN}\nOLD-SPANISH-RULES\n{agents.MD_END}\n\nKeep this.\n")
    assert agents.status(agents.AGENTS["codex"], home)["installed"]  # older install still counts
    install(home, "codex")
    text = agents_md.read_text()
    assert SPANISH_MD_BEGIN not in text and "OLD-SPANISH-RULES" not in text
    assert text.count(agents.MD_BEGIN) == 1 and text.count(agents.MD_END) == 1
    assert text.startswith("# My rules") and "Keep this." in text and "rolehop list" in text
    install(home, "codex")
    assert agents_md.read_text() == text  # idempotent
    agents.uninstall(agents.AGENTS["codex"], home, agents.Changes(False))
    assert agents_md.read_text() == "# My rules\nKeep this.\n"


def test_spanish_and_english_blocks_together_leave_one(home):
    agents_md = home / ".gemini/GEMINI.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(f"{agents.MD_BEGIN}\nNEW-RULES\n{agents.MD_END}\n\n{SPANISH_MD_BEGIN}\nOLD-SPANISH-RULES\n{agents.MD_END}\n")
    install(home, "gemini")
    text = agents_md.read_text()
    assert SPANISH_MD_BEGIN not in text and "OLD-SPANISH-RULES" not in text and "NEW-RULES" not in text
    assert text.count(agents.MD_BEGIN) == 1 and text.count(agents.MD_END) == 1
    agents.uninstall(agents.AGENTS["gemini"], home, agents.Changes(False))
    assert not agents_md.exists()


def test_kiro_steering_with_frontmatter(home):
    install(home, "kiro")
    text = (home / ".kiro/steering/aws.md").read_text()
    assert text.startswith("---\ninclusion: always\n---")


def test_copilot_qwen_and_gemini(home):
    install(home, "copilot", "qwen", "gemini")
    cop = json.loads((home / ".copilot/hooks/rolehop.json").read_text())
    assert cop["version"] == 1 and "PostToolUse" in cop["hooks"]
    assert "--format copilot" in cop["hooks"]["PostToolUse"][0]["bash"]
    qwen = json.loads((home / ".qwen/settings.json").read_text())
    assert qwen["hooks"]["PostToolUse"][0]["matcher"] == "^run_shell_command$"
    gem = json.loads((home / ".gemini/settings.json").read_text())
    assert gem["hooks"]["AfterTool"][0]["matcher"] == "run_shell_command"
    assert agents.MD_BEGIN in (home / ".gemini/GEMINI.md").read_text()


def test_invalid_json_is_left_untouched(home):
    s = home / ".claude/settings.json"
    s.parent.mkdir(parents=True)
    s.write_text("{ // comment")
    lines = install(home, "claude")
    assert any(line.startswith("✗") for line in lines)
    assert s.read_text() == "{ // comment"


def test_dry_run_writes_nothing(home):
    lines = install(home, "claude", "codex", "opencode", dry=True)
    assert all(line.startswith(("[dry-run]", "!")) for line in lines)
    assert not any(home.rglob("*.md")) and not any(home.rglob("*.json"))


def test_cursor_note_only_and_resolve(home):
    lines = install(home, "cursor")
    assert any("Settings → Rules" in line for line in lines)
    with pytest.raises(KeyError):
        agents.resolve(["nonexistent"], False, home)
    (home / ".kiro").mkdir()
    assert [a.key for a in agents.resolve([], False, home)] == ["kiro"]


def test_hook_formats_per_agent(sample, capsys):
    from rolehop import hooks
    expired = "Error when retrieving token from sso: Token has expired and refresh failed"
    # Copilot (camelCase, toolArgs as JSON, combined output)
    capsys.readouterr()
    hooks.post_tool("copilot", json.dumps({"toolName": "bash", "toolArgs": '{"command": "aws s3 ls"}',
                                           "toolResult": {"resultType": "failure", "textResultForLlm": expired}}))
    out = json.loads(capsys.readouterr().out)
    assert "awslogin" in out["additionalContext"]
    # Gemini (combined output: must not fire on a generic ExpiredToken inside legitimate data)
    hooks.post_tool("gemini", json.dumps({"tool_name": "run_shell_command",
                                          "tool_input": {"command": "aws cloudtrail lookup-events"},
                                          "tool_response": {"llmContent": '{"EventName":"ExpiredTokenException"}'}}))
    assert capsys.readouterr().out == ""
    hooks.post_tool("gemini", json.dumps({"tool_name": "run_shell_command", "tool_input": {"command": "aws s3 ls"},
                                          "tool_response": {"llmContent": expired}}))
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["hookEventName"] == "AfterTool"
