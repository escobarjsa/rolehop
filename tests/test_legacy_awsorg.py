"""Compatibility with older installs: the previous name (awsorg <= 1.2.0) and Spanish markers (rolehop <= 1.4.0).

Their config, state, managed blocks and hooks are adopted.
"""

from __future__ import annotations

import configparser
import importlib
import json

import pytest
from conftest import SAMPLE
from rolehop import agents, shellrc

LEGACY_BLOCK = """\
[default]
region = eu-west-1

# >>> awsorg:acme (gestionado por awsorg, no editar) >>>
[sso-session acme]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile acme-devsecops-admin]
sso_session = acme
sso_account_id = 111111111111
sso_role_name = Administrator
# <<< awsorg:acme <<<
"""


def test_sync_adopts_awsorg_block_without_duplicates(sample):
    cli, p = sample
    p.aws.write_text(LEGACY_BLOCK)
    assert cli.main(["sync", "--quiet"]) == 0
    text = p.aws.read_text()
    assert "awsorg:" not in text and "# >>> rolehop:acme" in text
    assert text.count("[sso-session acme]") == 1
    assert "[default]" in text
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)  # valid INI, no duplicate sections
    assert cfg.get("profile acme-prod-admin", "sso_account_id") == "222222222222"


def test_migrates_awsorg_config_and_state(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".config/awsorg").mkdir(parents=True)
    (home / ".config/awsorg/orgs.toml").write_text(SAMPLE)
    (tmp_path / "state/awsorg").mkdir(parents=True)
    (tmp_path / "state/awsorg/acme.json").write_text('{"login_at": 1}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    for var in ("ROLEHOP_CONFIG", "AWSORG_CONFIG", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr()
    assert "migrated" in out.err and "acme-devsecops-admin" in out.out
    assert (home / ".config/rolehop/orgs.toml").read_text() == SAMPLE
    assert (home / ".config/awsorg/orgs.toml").exists()  # the original is kept
    assert (tmp_path / "state/rolehop/acme.json").exists()
    capsys.readouterr()
    cli.main(["list"])
    assert "migrated" not in capsys.readouterr().err  # only the first time


def test_awsorg_config_env_var_still_works(tmp_path, monkeypatch):
    cfg = tmp_path / "other.toml"
    cfg.write_text(SAMPLE)
    monkeypatch.delenv("ROLEHOP_CONFIG", raising=False)
    monkeypatch.setenv("AWSORG_CONFIG", str(cfg))
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    assert cli.CONFIG_PATH == cfg


AWSORG_MARKS = ("# >>> awsorg (gestionado por awsorg setup) >>>", "# <<< awsorg <<<")
SPANISH_MARKS = ("# >>> rolehop (gestionado por rolehop setup) >>>", "# <<< rolehop <<<")
SPANISH_MD = ("<!-- >>> rolehop (gestionado por rolehop, no editar) >>> -->", "<!-- <<< rolehop <<< -->")
AWSORG_MD = ("<!-- >>> awsorg (gestionado por awsorg, no editar) >>> -->", "<!-- <<< awsorg <<< -->")


def test_legacy_markers_are_registered():
    assert AWSORG_MARKS in shellrc.LEGACY_MARKS and SPANISH_MARKS in shellrc.LEGACY_MARKS
    assert AWSORG_MD in agents.LEGACY_MD and SPANISH_MD in agents.LEGACY_MD
    assert shellrc.MARK_BEGIN not in (b for b, _ in shellrc.LEGACY_MARKS)
    assert agents.MD_BEGIN not in (b for b, _ in agents.LEGACY_MD)


@pytest.mark.parametrize("marks", [AWSORG_MARKS, SPANISH_MARKS], ids=["awsorg", "spanish"])
def test_shellrc_replaces_legacy_block(tmp_path, monkeypatch, marks):
    home = tmp_path / "h"
    home.mkdir()
    begin, end = marks
    (home / ".zshrc").write_text(f"# mine\n\n{begin}\nexport PATH=old\n{end}\n\nalias x=y\n")
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    shellrc.ensure_shell_integration(home=home, shell="zsh")
    text = (home / ".zshrc").read_text()
    assert begin not in text and "PATH=old" not in text
    assert text.count(shellrc.MARK_BEGIN) == 1 and text.count(shellrc.MARK_END) == 1
    assert "alias x=y" in text and text.startswith("# mine\n")
    again = shellrc.ensure_shell_integration(home=home, shell="zsh")
    assert (home / ".zshrc").read_text() == text and len(again) == 1  # idempotent


def test_shellrc_drops_spanish_block_next_to_english_one(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    spanish = f"{SPANISH_MARKS[0]}\nexport PATH=old\n{SPANISH_MARKS[1]}\n"
    (home / ".zshrc").write_text(f"# mine\n\n{spanish}\n{shellrc.block_for('zsh', False)}\nalias x=y\n")
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    shellrc.ensure_shell_integration(home=home, shell="zsh")
    text = (home / ".zshrc").read_text()
    assert SPANISH_MARKS[0] not in text and "PATH=old" not in text
    assert text.count(shellrc.MARK_BEGIN) == 1 and text.count(shellrc.MARK_END) == 1 and "alias x=y" in text


@pytest.mark.parametrize("marks", [AWSORG_MARKS, SPANISH_MARKS], ids=["awsorg", "spanish"])
def test_remove_shell_integration_removes_legacy_block(tmp_path, marks):
    home = tmp_path / "h"
    home.mkdir()
    begin, end = marks
    (home / ".zshrc").write_text(f"# mine\n\n{begin}\nexport PATH=old\n{end}\n\nalias x=y\n")
    msgs = shellrc.remove_shell_integration(home=home)
    text = (home / ".zshrc").read_text()
    assert len(msgs) == 1 and begin not in text and end not in text and "PATH=old" not in text
    assert text == "# mine\nalias x=y\n"


def test_agents_replace_awsorg_block_and_hooks(tmp_path, monkeypatch):
    home = tmp_path / "h"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agents, "rolehop_command", lambda: "/opt/bin/rolehop")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    md = home / ".codex/AGENTS.md"
    md.parent.mkdir(parents=True)
    md.write_text(f"# My rules\n\n{AWSORG_MD[0]}\nOLD-RULES\n{AWSORG_MD[1]}\n")
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/home/x/.local/bin/awsorg hook post-tool --format claude"}]}]},
        "permissions": {"deny": ["Bash(awsorg login *)"]}}))
    old_copilot = home / ".copilot/hooks/awsorg.json"
    old_copilot.parent.mkdir(parents=True)
    old_copilot.write_text("{}")

    ch = agents.Changes(False)
    for name in ("codex", "claude", "copilot"):
        agents.install(agents.AGENTS[name], home, ch)

    text = md.read_text()
    assert AWSORG_MD[0] not in text and "OLD-RULES" not in text and text.count(agents.MD_BEGIN) == 1
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert cmds == ["/opt/bin/rolehop hook post-tool --format claude"]
    assert not old_copilot.exists() and (home / ".copilot/hooks/rolehop.json").exists()
    assert agents.status(agents.AGENTS["codex"], home)["installed"]

    ch = agents.Changes(False)
    agents.uninstall(agents.AGENTS["claude"], home, ch)
    assert "Bash(awsorg login *)" not in json.loads(settings.read_text()).get("permissions", {}).get("deny", [])


def test_dry_run_does_not_migrate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config/awsorg").mkdir(parents=True)
    (home / ".config/awsorg/orgs.toml").write_text(SAMPLE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    for var in ("ROLEHOP_CONFIG", "AWSORG_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    cli.main(["sync", "--dry-run"])
    assert not (home / ".config/rolehop/orgs.toml").exists()
