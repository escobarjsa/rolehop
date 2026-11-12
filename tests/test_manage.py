"""`rolehop refresh`, `remove` and `uninstall`."""

from __future__ import annotations

import configparser
import hashlib
import json
import textwrap

import pytest
from test_wizard_discover import Portal, portal  # noqa: F401  (reused fixture)

from conftest import SAMPLE
from rolehop import agents, manage, shellrc

TWO_ORGS = SAMPLE + textwrap.dedent("""
    [[org.assume_roles]]
    name = "deploy"
    role_arn = "arn:aws:iam::222222222222:role/Deploy"
    source = "devsecops-admin"

    [[org]]
    name = "personal"
    start_url = "https://personal.awsapps.com/start"
    sso_region = "us-east-1"

    [org.roles]
    admin = "AdministratorAccess"

    [[org.accounts]]
    name = "main"
    id = "333333333333"
    roles = ["admin"]
""")


def profiles(p) -> set[str]:
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)
    return {s.removeprefix("profile ") for s in cfg.sections() if s.startswith("profile ")}


@pytest.fixture
def live(portal, monkeypatch):  # noqa: F811
    """Mocked access portal + active session (without calling the real AWS CLI)."""
    cli, p = portal
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": True, "org": org["name"]})
    p.orgs.write_text(SAMPLE)
    assert cli.main(["sync", "--quiet"]) == 0
    return cli, p


# --------------------------------------------------------------------------- refresh
def test_plan_refresh_adds_and_flags_revoked_without_prune(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    inv = [{"id": "111111111111", "name": "devsecops", "roles": ["Administrator", "PowerUser"]},
           {"id": "999999999999", "name": "acme-new", "roles": ["ReadOnly"]}]
    new, changes, removed = manage.plan_refresh(org, inv, prune=False)
    assert removed == set()
    accs = {a["name"]: a for a in new["accounts"]}
    assert accs["devsecops"]["roles"] == ["admin", "ro", "power"]  # ro revoked but kept
    assert accs["prod"]["roles"] == ["admin"]  # missing account: kept without --prune
    assert accs["new"]["roles"] == ["ro"] and new["roles"]["power"] == "PowerUser"
    text = "\n".join(changes)
    assert "+ role acme-devsecops-power" in text and "+ account new" in text
    assert "! role acme-devsecops-ro" in text and "--prune" in text and "! account prod" in text
    assert org["accounts"][0]["roles"] == ["admin", "ro"]  # does not mutate the original org


def test_plan_refresh_prune_keeps_assume_role_sources(sample):
    cli, p = sample
    p.orgs.write_text(TWO_ORGS)
    org = cli.load_orgs()[0]
    inv = [{"id": "222222222222", "name": "prod", "roles": ["Administrator"]}]  # devsecops is gone
    new, changes, removed = manage.plan_refresh(org, inv, prune=True)
    assert removed == {"acme-devsecops-ro"}
    assert [a["name"] for a in new["accounts"]] == ["devsecops", "prod"]
    assert new["accounts"][0]["roles"] == ["admin"]  # admin protected (source of deploy); ro is removed
    assert any("assume_roles" in c for c in changes)


def test_refresh_dry_run_apply_and_prune(live, capsys, monkeypatch):
    cli, p = live
    before = p.orgs.read_text()
    assert cli.main(["refresh", "--dry-run"]) == 0
    assert p.orgs.read_text() == before and "dry run" in capsys.readouterr().out

    assert cli.main(["refresh"]) == 0
    got = profiles(p)
    assert {"acme-devsecops-power", "acme-prod-ro", "acme-sandbox-dataanalyst"} <= got
    assert "acme-devsecops-ro" in got  # revoked in the portal, but kept without --prune
    assert list(p.orgs.parent.glob("orgs.toml.bak-*"))

    capsys.readouterr()
    snapshot = p.orgs.read_text()
    assert cli.main(["refresh"]) == 0  # idempotent: only repeats the revoked-role warning
    out = capsys.readouterr().out
    assert p.orgs.read_text() == snapshot and "+ " not in out and "! role acme-devsecops-ro" in out

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["refresh", "--prune"]) == 1  # removing profiles requires confirmation
    assert "acme-devsecops-ro" in profiles(p)
    assert cli.main(["refresh", "--prune", "--yes"]) == 0
    assert "acme-devsecops-ro" not in profiles(p)


def test_refresh_without_session_writes_nothing(live, capsys):
    cli, p = live
    for f in p.cache.glob("*.json"):  # no cached SSO token
        f.unlink()
    before = p.orgs.read_text()
    assert cli.main(["refresh"]) == 1
    assert p.orgs.read_text() == before
    assert "awslogin acme" in capsys.readouterr().err


# --------------------------------------------------------------------------- remove
@pytest.fixture
def two(sample):
    cli, p = sample
    p.orgs.write_text(TWO_ORGS)
    assert cli.main(["sync", "--quiet"]) == 0
    return cli, p


def test_remove_profile_account_and_assume_role(two):
    cli, p = two
    assert cli.main(["remove", "acme-devsecops-ro", "--yes"]) == 0
    assert "acme-devsecops-ro" not in profiles(p) and "acme-devsecops-admin" in profiles(p)
    assert cli.main(["remove", "acme-deploy", "--yes"]) == 0  # chained role
    assert "acme-deploy" not in profiles(p)
    assert cli.main(["remove", "acme-devsecops", "--yes"]) == 0  # whole account
    assert not any(x.startswith("acme-devsecops") for x in profiles(p))


def test_remove_protects_assume_source_and_empty_org(two, capsys):
    cli, p = two
    before = p.orgs.read_text()
    assert cli.main(["remove", "acme-devsecops-admin", "--yes"]) == 2
    assert cli.main(["remove", "personal-main-admin", "--yes"]) == 2  # would leave the org empty
    assert cli.main(["remove", "does-not-exist", "--yes"]) == 2
    assert p.orgs.read_text() == before
    err = capsys.readouterr().err
    assert "assume_role" in err and "rolehop remove personal" in err and "rolehop list" in err


def test_remove_org_deletes_block_state_and_token(two):
    cli, p = two
    p.cache.mkdir(parents=True, exist_ok=True)
    token = p.cache / f"{hashlib.sha1(b'personal').hexdigest()}.json"
    token.write_text(json.dumps({"accessToken": "x"}))
    cli.write_state("personal", last_login="2026-01-01T00:00:00+00:00")
    assert cli.main(["remove", "personal", "--dry-run"]) == 0
    assert token.exists() and "personal-main-admin" in profiles(p)
    assert cli.main(["remove", "personal", "--yes"]) == 0
    text = p.aws.read_text()
    assert "rolehop:personal" not in text and "[sso-session personal]" not in text and "rolehop:acme" in text
    assert not token.exists() and not cli.state_file("personal").exists()
    assert [o["name"] for o in cli.load_orgs()] == ["acme"]


def test_remove_requires_confirmation_without_tty(two, monkeypatch):
    cli, p = two
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    before = p.orgs.read_text()
    assert cli.main(["remove", "acme-devsecops-ro"]) == 1
    assert p.orgs.read_text() == before


def test_remove_refuses_to_remove_every_org(sample):
    cli, p = sample
    assert cli.main(["sync", "--quiet"]) == 0
    assert cli.main(["remove", "acme", "--yes"]) == 2


# --------------------------------------------------------------------------- uninstall
def test_uninstall_cleans_everything_and_keeps_config(sample, monkeypatch, capsys):
    cli, p = sample
    p.aws.write_text("[default]\nregion = eu-west-1\n")
    assert cli.main(["sync", "--quiet"]) == 0
    monkeypatch.setattr(agents, "rolehop_command", lambda: "/opt/bin/rolehop")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    ch = agents.Changes(False)
    agents.install(agents.AGENTS["claude"], p.home, ch)
    (p.home / ".zshrc").write_text("# mine\n")
    shellrc.ensure_shell_integration(home=p.home, shell="zsh")
    cli.write_state("acme", last_login="2026-01-01T00:00:00+00:00")

    assert cli.main(["uninstall", "--dry-run"]) == 0
    assert "rolehop:acme" in p.aws.read_text()
    assert cli.main(["uninstall", "--yes"]) == 0

    assert p.aws.read_text() == "[default]\nregion = eu-west-1\n"
    assert (p.home / ".zshrc").read_text() == "# mine\n"
    assert not (p.home / ".claude/rules/aws.md").exists()
    settings = json.loads((p.home / ".claude/settings.json").read_text())
    assert "hooks" not in settings and not settings.get("permissions", {}).get("deny")
    assert not cli.state_file("acme").exists()
    assert p.orgs.exists() and "uv tool uninstall rolehop" in capsys.readouterr().out

    assert cli.main(["uninstall", "--yes", "--purge"]) == 0
    assert not p.orgs.exists()


def test_refresh_works_even_if_probe_profile_fails(live, monkeypatch):
    """The probe_profile role may be the revoked one: refresh does not depend on it if the token is valid."""
    cli, p = live
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": False, "error": "ForbiddenException"})
    assert cli.main(["refresh"]) == 0
    assert "acme-devsecops-power" in profiles(p)


def test_refresh_does_not_rename_roles_written_with_exact_name(sample):
    cli, p = sample
    p.orgs.write_text(textwrap.dedent("""
        [[org]]
        name = "e"
        start_url = "https://e.awsapps.com/start"
        sso_region = "us-east-1"

        [[org.accounts]]
        name = "dev"
        id = "111111111111"
        roles = ["AdministratorAccess"]
    """))
    org = cli.load_orgs()[0]
    inv = [{"id": "111111111111", "name": "dev", "roles": ["AdministratorAccess"]},
           {"id": "222222222222", "name": "prod", "roles": ["AdministratorAccess", "ReadOnly"]}]
    new, changes, _ = manage.plan_refresh(org, inv, prune=False)
    got = {r["profile"] for r in cli.catalog([new])}
    assert "e-dev-administratoraccess" in got and "e-prod-administratoraccess" in got and "e-prod-ro" in got


def test_refresh_prune_with_empty_portal_or_emptied_org(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    with pytest.raises(cli.ConfigError, match="returned no accounts"):
        manage.plan_refresh(org, [], prune=True)
    inv = [{"id": "111111111111", "name": "devsecops", "roles": ["Billing"]}]  # everything known is revoked
    new, _, _ = manage.plan_refresh(org, inv, prune=True)  # the account keeps the new role: valid
    assert [a["roles"] for a in new["accounts"]] == [["billing"]]


def test_refresh_does_not_save_if_sync_would_conflict(live):
    cli, p = live
    p.aws.write_text(p.aws.read_text() + "\n[profile acme-sandbox-dataanalyst]\nregion = us-east-1\n")
    before = p.orgs.read_text()
    assert cli.main(["refresh"]) == 2
    assert p.orgs.read_text() == before  # orgs.toml and ~/.aws/config stay in sync


def test_refresh_multiple_orgs_one_fails(live, capsys):
    cli, p = live
    p.orgs.write_text(SAMPLE + textwrap.dedent("""
        [[org]]
        name = "other"
        start_url = "https://other.awsapps.com/start"
        sso_region = "us-east-1"
        [org.roles]
        admin = "Administrator"
        [[org.accounts]]
        name = "main"
        id = "999999999999"
        roles = ["admin"]
    """))
    assert cli.main(["refresh"]) == 1  # 'other' has no token: it fails, but 'acme' is applied
    assert "acme-devsecops-power" in profiles(p) and "other-main-admin" in profiles(p)
    assert "awslogin other" in capsys.readouterr().err


def test_remove_ambiguous_name_and_prefixes(sample):
    cli, p = sample
    p.orgs.write_text(SAMPLE + textwrap.dedent("""
        [[org.accounts]]
        name = "prod-eu"
        id = "333333333333"
        roles = ["admin"]

        [[org.assume_roles]]
        name = "deploy"
        role_arn = "arn:aws:iam::333333333333:role/Deploy"
        source = "prod-eu-admin"

        [[org]]
        name = "acme-prod"
        start_url = "https://other.awsapps.com/start"
        sso_region = "us-east-1"
        [org.roles]
        admin = "Administrator"
        [[org.accounts]]
        name = "x"
        id = "444444444444"
        roles = ["admin"]
    """))
    assert cli.main(["sync", "--quiet"]) == 0
    with pytest.raises(cli.ConfigError, match="ambiguous"):
        manage._resolve(cli.load_orgs(), "acme-prod")
    # 'prod' is not a source of anything even though 'prod-eu-admin' shares its prefix
    kind, org, acc = manage._resolve(cli.load_orgs(), "acme-prod-admin")
    assert kind == "profile"
    assert cli.main(["remove", "acme-prod-eu-admin", "--yes"]) == 2  # it is the source of 'deploy'


def test_alias_for_does_not_loop(sample):
    cli, _ = sample
    roles = {"data-analyst": "Data-Analyst", "data-analyst-2": "Data.Analyst"}
    assert cli._alias_for("Data_Analyst", roles) == "data-analyst-3"


def test_shellrc_end_marker_before_begin_marker(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    rc = home / ".zshrc"
    rc.write_text(f"# note: {shellrc.MARK_END}\nexport A=1\n\n{shellrc.MARK_BEGIN}\nexport PATH=x\n{shellrc.MARK_END}\n")
    shellrc.remove_shell_integration(home=home)
    text = rc.read_text()
    assert text.count("export A=1") == 1 and shellrc.MARK_BEGIN not in text and "PATH=x" not in text


def test_uninstall_without_aws_config_and_with_broken_config(sample, capsys):
    cli, p = sample
    assert cli.main(["uninstall", "--yes"]) == 0  # ~/.aws/config does not exist
    p.aws.write_text("# >>> rolehop:acme (managed by rolehop, do not edit) >>>\n[sso-session acme]\n")
    capsys.readouterr()
    assert cli.main(["uninstall", "--yes"]) == 1  # unclosed marker: left untouched, with a warning
    assert "rolehop:acme" in p.aws.read_text() and "incomplete" in capsys.readouterr().out


def test_uninstall_preserves_user_blank_lines(sample):
    cli, p = sample
    mine = "[default]\nregion = eu-west-1\n\n\n\n[profile mine]\nregion = us-east-2\n"
    p.aws.write_text(mine)
    assert cli.main(["sync", "--quiet"]) == 0
    assert p.aws.read_text().startswith(mine)  # sync does not touch the user's text either
    assert cli.main(["uninstall", "--yes"]) == 0
    assert p.aws.read_text() == mine


TIGHT = """[default]
output = json
# >>> rolehop:{org} (managed by rolehop, do not edit) >>>
[sso-session {org}]
sso_start_url = https://{org}.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
# <<< rolehop:{org} <<<
[profile mine]
region = eu-west-1
"""
TIGHT_AFTER = "[default]\noutput = json\n[profile mine]\nregion = eu-west-1\n"


def test_removing_adjacent_block_does_not_merge_user_sections(two):
    """Block with no blank line above and user text right below it (uninstall, sync --prune, remove)."""
    cli, p = two
    # remove <org>: the 'personal' block wedged between user sections
    text = p.aws.read_text()
    personal = text[text.index("# >>> rolehop:personal"):text.index("# <<< rolehop:personal <<<\n") + 27]
    p.aws.write_text(text.replace(personal, "").rstrip("\n") + "\n" + TIGHT.replace(
        TIGHT[TIGHT.index("# >>>"):TIGHT.index("[profile mine]")], personal))
    assert cli.main(["remove", "personal", "--yes"]) == 0
    assert p.aws.read_text().endswith("[default]\noutput = json\n[profile mine]\nregion = eu-west-1\n")


def test_uninstall_and_prune_with_adjacent_block(sample):
    cli, p = sample
    p.aws.write_text(TIGHT.format(org="stale"))
    assert cli.main(["sync", "--quiet", "--prune"]) == 0  # 'stale' is not in orgs.toml: it is pruned
    assert p.aws.read_text().startswith(TIGHT_AFTER)
    p.aws.write_text(TIGHT.format(org="acme"))
    assert cli.main(["uninstall", "--yes"]) == 0
    assert p.aws.read_text() == TIGHT_AFTER


def test_drop_placeholder_cases(sample):
    cli, _ = sample
    t = "\0ROLEHOP:x\0\n"
    assert cli.drop_placeholder(f"A\n{t}B\n", t) == "A\nB\n"
    assert cli.drop_placeholder(f"A\n\n{t}\nB\n", t) == "A\n\nB\n"
    assert cli.drop_placeholder(f"{t}\n[default]\n", t) == "[default]\n"
    assert cli.drop_placeholder(f"A\n\n{t}", t) == "A\n"
