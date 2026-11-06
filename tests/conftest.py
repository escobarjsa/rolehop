"""Aísla cada test: HOME, config de awsorg, ~/.aws/config y estado en un directorio temporal."""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

SAMPLE = textwrap.dedent("""
    [[org]]
    name = "empresa"
    start_url = "https://empresa.awsapps.com/start"
    sso_region = "us-east-1"
    default_role = "admin"

    [org.roles]
    admin = "Administrator"
    ro = "ReadOnly"

    [[org.accounts]]
    name = "devsecops"
    id = "111111111111"
    roles = ["admin", "ro"]

    [[org.accounts]]
    name = "prod"
    id = "222222222222"
    roles = ["admin"]
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Recarga awsorg.cli con rutas temporales y devuelve (cli, rutas)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AWSORG_CONFIG", str(tmp_path / "orgs.toml"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AWSORG_SSO_CACHE", str(tmp_path / "sso-cache"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    import awsorg.cli as cli
    cli = importlib.reload(cli)
    paths = type("P", (), {
        "home": home,
        "orgs": tmp_path / "orgs.toml",
        "aws": tmp_path / "aws-config",
        "state": tmp_path / "state" / "awsorg",
        "cache": tmp_path / "sso-cache",
        "tmp": tmp_path,
    })
    return cli, paths


@pytest.fixture
def sample(env):
    cli, p = env
    p.orgs.write_text(SAMPLE)
    return cli, p


def write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text))
