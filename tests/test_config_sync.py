"""`rolehop sync`: generating ~/.aws/config blocks and validating orgs.toml."""

from __future__ import annotations

import configparser
import os

import pytest

from conftest import SAMPLE


def sync(cli, *extra):
    return cli.main(["sync", "--quiet", *extra])


def test_sync_creates_profiles_and_is_idempotent(sample, capsys):
    cli, p = sample
    assert sync(cli) == 0
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)
    assert cfg.get("sso-session acme", "sso_registration_scopes") == "sso:account:access"
    assert cfg.get("profile acme-devsecops-ro", "sso_role_name") == "ReadOnly"
    assert cfg.get("profile acme-prod-admin", "sso_account_id") == "222222222222"
    assert oct(os.stat(p.aws).st_mode & 0o777) == "0o600"
    capsys.readouterr()
    assert sync(cli) == 0
    assert "already up to date" in capsys.readouterr().out


def test_sync_keeps_foreign_content_and_makes_backup(sample, capsys):
    cli, p = sample
    p.aws.write_text("[default]\nregion = eu-west-1\n\n[profile mine]\nregion = us-east-2\n")
    assert sync(cli) == 0
    text = p.aws.read_text()
    assert "[profile mine]" in text and "[default]" in text
    assert list(p.aws.parent.glob("aws-config.bak-*"))
    assert "✓ Backup:" in capsys.readouterr().out


def test_conflict_outside_block_writes_nothing(sample, capsys):
    cli, p = sample
    p.aws.write_text("[profile acme-prod-admin]\nregion = eu-west-1\n")
    before = p.aws.read_text()
    assert sync(cli) == 2
    assert p.aws.read_text() == before
    assert "conflict with generated profiles" in capsys.readouterr().err


def test_symlink_is_preserved(sample):
    cli, p = sample
    real = p.tmp / "real-config"
    real.write_text("[default]\nregion = us-east-1\n")
    p.aws.symlink_to(real)
    assert sync(cli) == 0
    assert p.aws.is_symlink()
    assert "rolehop:acme" in real.read_text()


def test_duplicate_block_and_broken_marker(sample):
    cli, p = sample
    assert sync(cli) == 0
    block = p.aws.read_text()
    p.aws.write_text(block + "\n" + block)
    assert sync(cli) == 2
    p.aws.write_text(block.replace("# <<< rolehop:acme <<<", "# broken"))
    assert sync(cli) == 2
    assert "\0" not in p.aws.read_text()


@pytest.mark.parametrize("extra, msg", [
    ('[[org.accounts]]\nname = "devsecops"\nid = "333333333333"\nroles = ["admin"]\n', "Duplicate profiles"),
    ('[[org.accounts]]\nname = "x"\nid = "123"\nroles = ["admin"]\n', "12 digits"),
    ('[[org.accounts]]\nname = "x"\nid = "333333333333"\nroles = "admin"\n', "must be a list"),
    ('[[org.accounts]]\nname = "x"\nid = "333333333333"\nroles = ["ro\\n[profile evil]"]\n', "invalid role"),
    ('[[org.assume_roles]]\nname = "d"\nrole_arn = "arn:aws:iam::111111111111:role/R"\nsource = "nope-ro"\n',
     "unknown source"),
    ('[[org.assume_roles]]\nname = "d"\nrole_arn = "no-arn"\nsource = "devsecops-ro"\n', "invalid role_arn"),
])
def test_validation_errors(env, capsys, extra, msg):
    cli, p = env
    p.orgs.write_text(SAMPLE + extra)
    assert sync(cli) == 2
    assert msg in capsys.readouterr().err
    assert not p.aws.exists()


def test_url_with_newline_is_rejected(env, capsys):
    cli, p = env
    p.orgs.write_text(SAMPLE.replace('"https://acme.awsapps.com/start"',
                                     '"https://acme.awsapps.com/start\\n[profile evil]"'))
    assert sync(cli) == 2
    assert "invalid start_url" in capsys.readouterr().err


def test_assume_role_and_list(sample, capsys):
    cli, p = sample
    p.orgs.write_text(SAMPLE + '[[org.assume_roles]]\nname = "deploy"\n'
                      'role_arn = "arn:aws:iam::222222222222:role/Deploy"\nsource = "devsecops-admin"\n')
    assert sync(cli) == 0
    cfg = configparser.RawConfigParser()
    cfg.read(p.aws)
    assert cfg.get("profile acme-deploy", "source_profile") == "acme-devsecops-admin"
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    import json
    rows = json.loads(capsys.readouterr().out)
    assert {r["profile"] for r in rows} >= {"acme-devsecops-admin", "acme-deploy"}


def test_prune(sample):
    cli, p = sample
    assert sync(cli) == 0
    p.orgs.write_text(SAMPLE.replace('name = "acme"', 'name = "other"').replace("acme.awsapps", "other.awsapps"))
    assert sync(cli) == 0
    assert "rolehop:acme" in p.aws.read_text()
    assert sync(cli, "--prune") == 0
    assert "rolehop:acme" not in p.aws.read_text()


def test_probe_honors_default_role(sample):
    cli, _ = sample
    org = cli.load_orgs()[0]
    org["default_role"] = "ro"
    assert cli.probe_profile(org) == "acme-devsecops-ro"


def test_envrc(sample, capsys):
    cli, _ = sample
    assert cli.main(["envrc", "acme-prod-admin", "--state", "acme-devsecops-admin"]) == 0
    out = capsys.readouterr().out
    assert "export AWS_PROFILE=acme-prod-admin" in out
    assert cli.main(["envrc", "does-not-exist"]) == 2


def test_prune_leaves_no_extra_blank_lines(sample):
    cli, p = sample
    p.aws.write_text("[default]\nregion = eu-west-1\n")
    assert sync(cli) == 0
    p.orgs.write_text(SAMPLE.replace('name = "acme"', 'name = "other"'))
    assert sync(cli, "--prune") == 0
    text = p.aws.read_text()
    assert "rolehop:acme" not in text and "\n\n\n" not in text and text.startswith("[default]\nregion = eu-west-1\n\n#")


def test_dry_run_writes_nothing(sample, capsys):
    cli, p = sample
    assert cli.main(["sync", "--dry-run"]) == 0
    assert not p.aws.exists()
    assert "dry run: nothing was written" in capsys.readouterr().out


def test_legacy_spanish_marker_is_adopted_and_rewritten(sample):
    """Blocks written by older versions (Spanish begin marker) are recognized and rewritten in place."""
    cli, p = sample
    old = ("[default]\nregion = eu-west-1\n\n"
           "# >>> rolehop:acme (gestionado por rolehop, no editar) >>>\n"
           "[sso-session acme]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n"
           "# <<< rolehop:acme <<<\n\n[profile mine]\nregion = us-east-2\n")
    p.aws.write_text(old)
    assert sync(cli) == 0
    text = p.aws.read_text()
    assert "gestionado por rolehop" not in text
    assert text.count("# >>> rolehop:acme (managed by rolehop, do not edit) >>>") == 1
    assert text.count("[sso-session acme]") == 1
    assert text.startswith("[default]\nregion = eu-west-1\n\n# >>> rolehop:acme")
    assert text.endswith("[profile mine]\nregion = us-east-2\n")
    assert "[profile acme-prod-admin]" in text
