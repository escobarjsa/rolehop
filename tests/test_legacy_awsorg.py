"""Compatibilidad con el nombre anterior (awsorg ≤ 1.2.0): se adoptan su config, estado, bloques y hooks."""

from __future__ import annotations

import configparser
import importlib
import json

from conftest import SAMPLE
from rolehop import agents, shellrc

LEGACY_BLOCK = """\
[default]
region = eu-west-1

# >>> awsorg:empresa (gestionado por awsorg, no editar) >>>
[sso-session empresa]
sso_start_url = https://empresa.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile empresa-devsecops-admin]
sso_session = empresa
sso_account_id = 111111111111
sso_role_name = Administrator
# <<< awsorg:empresa <<<
"""


def test_sync_adopta_bloque_awsorg_sin_duplicar(sample):
    cli, p = sample
    p.aws.write_text(LEGACY_BLOCK)
    assert cli.main(["sync", "--quiet"]) == 0
    text = p.aws.read_text()
    assert "awsorg:" not in text and "# >>> rolehop:empresa" in text
    assert text.count("[sso-session empresa]") == 1
    assert "[default]" in text
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)  # INI válido, sin secciones duplicadas
    assert cfg.get("profile empresa-prod-admin", "sso_account_id") == "222222222222"


def test_migra_config_y_estado_de_awsorg(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".config/awsorg").mkdir(parents=True)
    (home / ".config/awsorg/orgs.toml").write_text(SAMPLE)
    (tmp_path / "state/awsorg").mkdir(parents=True)
    (tmp_path / "state/awsorg/empresa.json").write_text('{"login_at": 1}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    for var in ("ROLEHOP_CONFIG", "AWSORG_CONFIG", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr()
    assert "migrado" in out.err and "empresa-devsecops-admin" in out.out
    assert (home / ".config/rolehop/orgs.toml").read_text() == SAMPLE
    assert (home / ".config/awsorg/orgs.toml").exists()  # el original se conserva
    assert (tmp_path / "state/rolehop/empresa.json").exists()
    capsys.readouterr()
    cli.main(["list"])
    assert "migrado" not in capsys.readouterr().err  # solo la primera vez


def test_variable_awsorg_config_sigue_valiendo(tmp_path, monkeypatch):
    cfg = tmp_path / "otra.toml"
    cfg.write_text(SAMPLE)
    monkeypatch.delenv("ROLEHOP_CONFIG", raising=False)
    monkeypatch.setenv("AWSORG_CONFIG", str(cfg))
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    assert cli.CONFIG_PATH == cfg


def test_shellrc_reemplaza_bloque_awsorg(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    begin, end = shellrc.LEGACY_MARKS
    (home / ".zshrc").write_text(f"# mío\n\n{begin}\nexport PATH=viejo\n{end}\n\nalias x=y\n")
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    shellrc.ensure_shell_integration(home=home, shell="zsh")
    text = (home / ".zshrc").read_text()
    assert begin not in text and "PATH=viejo" not in text
    assert text.count(shellrc.MARK_BEGIN) == 1 and "alias x=y" in text and text.startswith("# mío\n")


def test_agentes_reemplazan_bloque_y_hooks_de_awsorg(tmp_path, monkeypatch):
    home = tmp_path / "h"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agents, "rolehop_command", lambda: "/opt/bin/rolehop")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    md = home / ".codex/AGENTS.md"
    md.parent.mkdir(parents=True)
    md.write_text(f"# Mis reglas\n\n{agents.LEGACY_MD[0]}\nviejo\n{agents.LEGACY_MD[1]}\n")
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
    assert agents.LEGACY_MD[0] not in text and "viejo" not in text and text.count(agents.MD_BEGIN) == 1
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert cmds == ["/opt/bin/rolehop hook post-tool --format claude"]
    assert not old_copilot.exists() and (home / ".copilot/hooks/rolehop.json").exists()
    assert agents.status(agents.AGENTS["codex"], home)["installed"]

    ch = agents.Changes(False)
    agents.uninstall(agents.AGENTS["claude"], home, ch)
    assert "Bash(awsorg login *)" not in json.loads(settings.read_text()).get("permissions", {}).get("deny", [])
