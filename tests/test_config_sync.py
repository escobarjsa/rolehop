from __future__ import annotations

import configparser
import os

import pytest

from conftest import SAMPLE


def sync(cli, *extra):
    return cli.main(["sync", "--quiet", *extra])


def test_sync_crea_perfiles_y_es_idempotente(sample, capsys):
    cli, p = sample
    assert sync(cli) == 0
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)
    assert cfg.get("sso-session empresa", "sso_registration_scopes") == "sso:account:access"
    assert cfg.get("profile empresa-devsecops-ro", "sso_role_name") == "ReadOnly"
    assert cfg.get("profile empresa-prod-admin", "sso_account_id") == "222222222222"
    assert oct(os.stat(p.aws).st_mode & 0o777) == "0o600"
    capsys.readouterr()
    assert sync(cli) == 0
    assert "ya está al día" in capsys.readouterr().out


def test_sync_conserva_contenido_ajeno_y_hace_backup(sample):
    cli, p = sample
    p.aws.write_text("[default]\nregion = eu-west-1\n\n[profile mio]\nregion = us-east-2\n")
    assert sync(cli) == 0
    text = p.aws.read_text()
    assert "[profile mio]" in text and "[default]" in text
    assert list(p.aws.parent.glob("aws-config.bak-*"))


def test_conflicto_fuera_de_bloque_no_escribe(sample):
    cli, p = sample
    p.aws.write_text("[profile empresa-prod-admin]\nregion = eu-west-1\n")
    before = p.aws.read_text()
    assert sync(cli) == 2
    assert p.aws.read_text() == before


def test_symlink_se_respeta(sample):
    cli, p = sample
    real = p.tmp / "real-config"
    real.write_text("[default]\nregion = us-east-1\n")
    p.aws.symlink_to(real)
    assert sync(cli) == 0
    assert p.aws.is_symlink()
    assert "awsorg:empresa" in real.read_text()


def test_bloque_duplicado_y_marcador_roto(sample):
    cli, p = sample
    assert sync(cli) == 0
    block = p.aws.read_text()
    p.aws.write_text(block + "\n" + block)
    assert sync(cli) == 2
    p.aws.write_text(block.replace("# <<< awsorg:empresa <<<", "# roto"))
    assert sync(cli) == 2
    assert "\0" not in p.aws.read_text()


@pytest.mark.parametrize("extra, msg", [
    ('[[org.accounts]]\nname = "devsecops"\nid = "333333333333"\nroles = ["admin"]\n', "duplicados"),
    ('[[org.accounts]]\nname = "x"\nid = "123"\nroles = ["admin"]\n', "12 dígitos"),
    ('[[org.accounts]]\nname = "x"\nid = "333333333333"\nroles = "admin"\n', "lista"),
    ('[[org.accounts]]\nname = "x"\nid = "333333333333"\nroles = ["ro\\n[profile evil]"]\n', "rol inválido"),
    ('[[org.assume_roles]]\nname = "d"\nrole_arn = "arn:aws:iam::111111111111:role/R"\nsource = "nope-ro"\n',
     "source inexistente"),
    ('[[org.assume_roles]]\nname = "d"\nrole_arn = "no-arn"\nsource = "devsecops-ro"\n', "role_arn inválido"),
])
def test_validaciones(env, capsys, extra, msg):
    cli, p = env
    p.orgs.write_text(SAMPLE + extra)
    assert sync(cli) == 2
    assert msg in capsys.readouterr().err
    assert not p.aws.exists()


def test_url_con_salto_de_linea_rechazada(env, capsys):
    cli, p = env
    p.orgs.write_text(SAMPLE.replace('"https://empresa.awsapps.com/start"',
                                     '"https://empresa.awsapps.com/start\\n[profile evil]"'))
    assert sync(cli) == 2
    assert "start_url inválida" in capsys.readouterr().err


def test_assume_role_y_list(sample, capsys):
    cli, p = sample
    p.orgs.write_text(SAMPLE + '[[org.assume_roles]]\nname = "deploy"\n'
                      'role_arn = "arn:aws:iam::222222222222:role/Deploy"\nsource = "devsecops-admin"\n')
    assert sync(cli) == 0
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)
    assert cfg.get("profile empresa-deploy", "source_profile") == "empresa-devsecops-admin"
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    import json
    rows = json.loads(capsys.readouterr().out)
    assert {r["profile"] for r in rows} >= {"empresa-devsecops-admin", "empresa-deploy"}


def test_prune(sample):
    cli, p = sample
    assert sync(cli) == 0
    p.orgs.write_text(SAMPLE.replace('name = "empresa"', 'name = "otra"').replace("empresa.awsapps", "otra.awsapps"))
    assert sync(cli) == 0
    assert "awsorg:empresa" in p.aws.read_text()
    assert sync(cli, "--prune") == 0
    assert "awsorg:empresa" not in p.aws.read_text()


def test_probe_respeta_default_role(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    org["default_role"] = "ro"
    assert cli.probe_profile(org) == "empresa-devsecops-ro"


def test_envrc(sample, capsys):
    cli, _ = sample
    assert cli.main(["envrc", "empresa-prod-admin", "--state", "empresa-devsecops-admin"]) == 0
    out = capsys.readouterr().out
    assert "export AWS_PROFILE=empresa-prod-admin" in out
    assert cli.main(["envrc", "no-existe"]) == 2
