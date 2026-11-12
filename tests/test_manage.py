"""`rolehop refresh`, `remove` y `uninstall`."""

from __future__ import annotations

import configparser
import hashlib
import json
import textwrap

import pytest
from test_wizard_discover import Portal, portal  # noqa: F401  (fixture reutilizada)

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
    """Portal simulado + sesión activa (sin llamar al AWS CLI real)."""
    cli, p = portal
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": True, "org": org["name"]})
    p.orgs.write_text(SAMPLE)
    assert cli.main(["sync", "--quiet"]) == 0
    return cli, p


# --------------------------------------------------------------------------- refresh
def test_plan_refresh_agrega_y_marca_revocados_sin_prune(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    inv = [{"id": "111111111111", "name": "devsecops", "roles": ["Administrator", "PowerUser"]},
           {"id": "999999999999", "name": "empresa-nueva", "roles": ["ReadOnly"]}]
    new, changes, removed = manage.plan_refresh(org, inv, prune=False)
    assert removed == set()
    accs = {a["name"]: a for a in new["accounts"]}
    assert accs["devsecops"]["roles"] == ["admin", "ro", "power"]  # ro revocado pero se conserva
    assert accs["prod"]["roles"] == ["admin"]  # cuenta desaparecida: se conserva sin --prune
    assert accs["nueva"]["roles"] == ["ro"] and new["roles"]["power"] == "PowerUser"
    text = "\n".join(changes)
    assert "+ rol empresa-devsecops-power" in text and "+ cuenta nueva" in text
    assert "! rol empresa-devsecops-ro" in text and "--prune" in text and "! cuenta prod" in text
    assert org["accounts"][0]["roles"] == ["admin", "ro"]  # no muta la org original


def test_plan_refresh_prune_respeta_assume_roles(sample):
    cli, p = sample
    p.orgs.write_text(TWO_ORGS)
    org = cli.load_orgs()[0]
    inv = [{"id": "222222222222", "name": "prod", "roles": ["Administrator"]}]  # devsecops desapareció
    new, changes, removed = manage.plan_refresh(org, inv, prune=True)
    assert removed == {"empresa-devsecops-ro"}
    assert [a["name"] for a in new["accounts"]] == ["devsecops", "prod"]
    assert new["accounts"][0]["roles"] == ["admin"]  # admin protegido (origen de deploy); ro se quita
    assert any("assume_roles" in c for c in changes)


def test_refresh_dry_run_aplica_y_prune(live, capsys, monkeypatch):
    cli, p = live
    before = p.orgs.read_text()
    assert cli.main(["refresh", "--dry-run"]) == 0
    assert p.orgs.read_text() == before and "dry-run" in capsys.readouterr().out

    assert cli.main(["refresh"]) == 0
    got = profiles(p)
    assert {"empresa-devsecops-power", "empresa-prod-ro", "empresa-sandbox-dataanalyst"} <= got
    assert "empresa-devsecops-ro" in got  # revocado en el portal, pero sin --prune se conserva
    assert list(p.orgs.parent.glob("orgs.toml.bak-*"))

    capsys.readouterr()
    snapshot = p.orgs.read_text()
    assert cli.main(["refresh"]) == 0  # idempotente: solo repite el aviso del rol revocado
    out = capsys.readouterr().out
    assert p.orgs.read_text() == snapshot and "+ " not in out and "! rol empresa-devsecops-ro" in out

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["refresh", "--prune"]) == 1  # quitar perfiles pide confirmación
    assert "empresa-devsecops-ro" in profiles(p)
    assert cli.main(["refresh", "--prune", "--yes"]) == 0
    assert "empresa-devsecops-ro" not in profiles(p)


def test_refresh_sin_sesion_no_escribe(live, capsys):
    cli, p = live
    for f in p.cache.glob("*.json"):  # sin token SSO en caché
        f.unlink()
    before = p.orgs.read_text()
    assert cli.main(["refresh"]) == 1
    assert p.orgs.read_text() == before
    assert "awslogin empresa" in capsys.readouterr().err


# --------------------------------------------------------------------------- remove
@pytest.fixture
def two(sample):
    cli, p = sample
    p.orgs.write_text(TWO_ORGS)
    assert cli.main(["sync", "--quiet"]) == 0
    return cli, p


def test_remove_perfil_cuenta_y_assume(two):
    cli, p = two
    assert cli.main(["remove", "empresa-devsecops-ro", "--yes"]) == 0
    assert "empresa-devsecops-ro" not in profiles(p) and "empresa-devsecops-admin" in profiles(p)
    assert cli.main(["remove", "empresa-deploy", "--yes"]) == 0  # rol encadenado
    assert "empresa-deploy" not in profiles(p)
    assert cli.main(["remove", "empresa-devsecops", "--yes"]) == 0  # cuenta entera
    assert not any(x.startswith("empresa-devsecops") for x in profiles(p))


def test_remove_protege_origen_de_assume_y_org_vacia(two, capsys):
    cli, p = two
    before = p.orgs.read_text()
    assert cli.main(["remove", "empresa-devsecops-admin", "--yes"]) == 2
    assert cli.main(["remove", "personal-main-admin", "--yes"]) == 2  # dejaría la org vacía
    assert cli.main(["remove", "no-existe", "--yes"]) == 2
    assert p.orgs.read_text() == before
    err = capsys.readouterr().err
    assert "assume_role" in err and "rolehop remove personal" in err and "rolehop list" in err


def test_remove_org_borra_bloque_estado_y_token(two):
    cli, p = two
    p.cache.mkdir(parents=True, exist_ok=True)
    token = p.cache / f"{hashlib.sha1(b'personal').hexdigest()}.json"
    token.write_text(json.dumps({"accessToken": "x"}))
    cli.write_state("personal", last_login="2026-01-01T00:00:00+00:00")
    assert cli.main(["remove", "personal", "--dry-run"]) == 0
    assert token.exists() and "personal-main-admin" in profiles(p)
    assert cli.main(["remove", "personal", "--yes"]) == 0
    text = p.aws.read_text()
    assert "rolehop:personal" not in text and "[sso-session personal]" not in text and "rolehop:empresa" in text
    assert not token.exists() and not cli.state_file("personal").exists()
    assert [o["name"] for o in cli.load_orgs()] == ["empresa"]


def test_remove_pide_confirmacion_sin_tty(two, monkeypatch):
    cli, p = two
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    before = p.orgs.read_text()
    assert cli.main(["remove", "empresa-devsecops-ro"]) == 1
    assert p.orgs.read_text() == before


def test_remove_no_quita_todas_las_orgs(sample):
    cli, p = sample
    assert cli.main(["sync", "--quiet"]) == 0
    assert cli.main(["remove", "empresa", "--yes"]) == 2


# --------------------------------------------------------------------------- uninstall
def test_uninstall_limpia_todo_y_conserva_config(sample, monkeypatch, capsys):
    cli, p = sample
    p.aws.write_text("[default]\nregion = eu-west-1\n")
    assert cli.main(["sync", "--quiet"]) == 0
    monkeypatch.setattr(agents, "rolehop_command", lambda: "/opt/bin/rolehop")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(shellrc.shutil, "which", lambda name: None)
    ch = agents.Changes(False)
    agents.install(agents.AGENTS["claude"], p.home, ch)
    (p.home / ".zshrc").write_text("# mío\n")
    shellrc.ensure_shell_integration(home=p.home, shell="zsh")
    cli.write_state("empresa", last_login="2026-01-01T00:00:00+00:00")

    assert cli.main(["uninstall", "--dry-run"]) == 0
    assert "rolehop:empresa" in p.aws.read_text()
    assert cli.main(["uninstall", "--yes"]) == 0

    assert p.aws.read_text() == "[default]\nregion = eu-west-1\n"
    assert (p.home / ".zshrc").read_text() == "# mío\n"
    assert not (p.home / ".claude/rules/aws.md").exists()
    settings = json.loads((p.home / ".claude/settings.json").read_text())
    assert "hooks" not in settings and not settings.get("permissions", {}).get("deny")
    assert not cli.state_file("empresa").exists()
    assert p.orgs.exists() and "uv tool uninstall rolehop" in capsys.readouterr().out

    assert cli.main(["uninstall", "--yes", "--purge"]) == 0
    assert not p.orgs.exists()


def test_refresh_funciona_aunque_falle_el_perfil_de_prueba(live, monkeypatch):
    """El rol del probe_profile puede ser justo el revocado: refresh no depende de él si el token es válido."""
    cli, p = live
    monkeypatch.setattr(cli, "check_org", lambda org: {"ok": False, "error": "ForbiddenException"})
    assert cli.main(["refresh"]) == 0
    assert "empresa-devsecops-power" in profiles(p)


def test_refresh_no_renombra_roles_escritos_con_nombre_exacto(sample):
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


def test_refresh_prune_con_portal_vacio_o_que_vaciaria_la_org(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    with pytest.raises(cli.ConfigError, match="no devolvió ninguna cuenta"):
        manage.plan_refresh(org, [], prune=True)
    inv = [{"id": "111111111111", "name": "devsecops", "roles": ["Billing"]}]  # todo lo conocido revocado
    new, _, _ = manage.plan_refresh(org, inv, prune=True)  # queda la cuenta con el rol nuevo: válido
    assert [a["roles"] for a in new["accounts"]] == [["billing"]]


def test_refresh_no_guarda_si_sync_va_a_chocar(live):
    cli, p = live
    p.aws.write_text(p.aws.read_text() + "\n[profile empresa-sandbox-dataanalyst]\nregion = us-east-1\n")
    before = p.orgs.read_text()
    assert cli.main(["refresh"]) == 2
    assert p.orgs.read_text() == before  # orgs.toml y ~/.aws/config siguen alineados


def test_refresh_varias_orgs_una_falla(live, capsys):
    cli, p = live
    p.orgs.write_text(SAMPLE + textwrap.dedent("""
        [[org]]
        name = "otra"
        start_url = "https://otra.awsapps.com/start"
        sso_region = "us-east-1"
        [org.roles]
        admin = "Administrator"
        [[org.accounts]]
        name = "main"
        id = "999999999999"
        roles = ["admin"]
    """))
    assert cli.main(["refresh"]) == 1  # 'otra' no tiene token: falla, pero 'empresa' se aplica
    assert "empresa-devsecops-power" in profiles(p) and "otra-main-admin" in profiles(p)
    assert "awslogin otra" in capsys.readouterr().err


def test_remove_nombre_ambiguo_y_prefijos(sample):
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
        name = "empresa-prod"
        start_url = "https://otra.awsapps.com/start"
        sso_region = "us-east-1"
        [org.roles]
        admin = "Administrator"
        [[org.accounts]]
        name = "x"
        id = "444444444444"
        roles = ["admin"]
    """))
    assert cli.main(["sync", "--quiet"]) == 0
    with pytest.raises(cli.ConfigError, match="ambiguo"):
        manage._resolve(cli.load_orgs(), "empresa-prod")
    # 'prod' no es origen de nada aunque 'prod-eu-admin' empiece igual
    kind, org, acc = manage._resolve(cli.load_orgs(), "empresa-prod-admin")
    assert kind == "profile"
    assert cli.main(["remove", "empresa-prod-eu-admin", "--yes"]) == 2  # sí es origen de 'deploy'


def test_alias_for_no_entra_en_bucle(sample):
    cli, _ = sample
    roles = {"data-analyst": "Data-Analyst", "data-analyst-2": "Data.Analyst"}
    assert cli._alias_for("Data_Analyst", roles) == "data-analyst-3"


def test_shellrc_marcador_final_antes_del_inicio(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    rc = home / ".zshrc"
    rc.write_text(f"# nota: {shellrc.MARK_END}\nexport A=1\n\n{shellrc.MARK_BEGIN}\nexport PATH=x\n{shellrc.MARK_END}\n")
    shellrc.remove_shell_integration(home=home)
    text = rc.read_text()
    assert text.count("export A=1") == 1 and shellrc.MARK_BEGIN not in text and "PATH=x" not in text


def test_uninstall_sin_aws_config_y_con_config_rota(sample, capsys):
    cli, p = sample
    assert cli.main(["uninstall", "--yes"]) == 0  # no existe ~/.aws/config
    p.aws.write_text("# >>> rolehop:empresa (gestionado por rolehop, no editar) >>>\n[sso-session empresa]\n")
    capsys.readouterr()
    assert cli.main(["uninstall", "--yes"]) == 1  # marcador sin cierre: no se toca y se avisa
    assert "rolehop:empresa" in p.aws.read_text() and "incompleta" in capsys.readouterr().out


def test_uninstall_conserva_lineas_en_blanco_del_usuario(sample):
    cli, p = sample
    mine = "[default]\nregion = eu-west-1\n\n\n\n[profile mio]\nregion = us-east-2\n"
    p.aws.write_text(mine)
    assert cli.main(["sync", "--quiet"]) == 0
    assert p.aws.read_text().startswith(mine)  # sync tampoco toca el texto del usuario
    assert cli.main(["uninstall", "--yes"]) == 0
    assert p.aws.read_text() == mine


TIGHT = """[default]
output = json
# >>> rolehop:{org} (gestionado por rolehop, no editar) >>>
[sso-session {org}]
sso_start_url = https://{org}.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
# <<< rolehop:{org} <<<
[profile mine]
region = eu-west-1
"""
TIGHT_AFTER = "[default]\noutput = json\n[profile mine]\nregion = eu-west-1\n"


def test_quitar_bloque_pegado_no_fusiona_secciones_del_usuario(two):
    """Bloque sin línea en blanco encima y con texto del usuario justo debajo (uninstall, sync --prune, remove)."""
    cli, p = two
    # remove <org>: el bloque de 'personal' pegado entre secciones del usuario
    text = p.aws.read_text()
    personal = text[text.index("# >>> rolehop:personal"):text.index("# <<< rolehop:personal <<<\n") + 27]
    p.aws.write_text(text.replace(personal, "").rstrip("\n") + "\n" + TIGHT.replace(
        TIGHT[TIGHT.index("# >>>"):TIGHT.index("[profile mine]")], personal))
    assert cli.main(["remove", "personal", "--yes"]) == 0
    assert p.aws.read_text().endswith("[default]\noutput = json\n[profile mine]\nregion = eu-west-1\n")


def test_uninstall_y_prune_con_bloque_pegado(sample):
    cli, p = sample
    p.aws.write_text(TIGHT.format(org="viejo"))
    assert cli.main(["sync", "--quiet", "--prune"]) == 0  # 'viejo' no está en orgs.toml: se poda
    assert p.aws.read_text().startswith(TIGHT_AFTER)
    p.aws.write_text(TIGHT.format(org="empresa"))
    assert cli.main(["uninstall", "--yes"]) == 0
    assert p.aws.read_text() == TIGHT_AFTER


def test_drop_placeholder_casos(sample):
    cli, _ = sample
    t = "\0ROLEHOP:x\0\n"
    assert cli.drop_placeholder(f"A\n{t}B\n", t) == "A\nB\n"
    assert cli.drop_placeholder(f"A\n\n{t}\nB\n", t) == "A\n\nB\n"
    assert cli.drop_placeholder(f"{t}\n[default]\n", t) == "[default]\n"
    assert cli.drop_placeholder(f"A\n\n{t}", t) == "A\n"
