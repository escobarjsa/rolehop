"""Isolate each test: HOME, rolehop config, ~/.aws/config and state live in a temporary directory."""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

SAMPLE = textwrap.dedent("""
    [[org]]
    name = "acme"
    start_url = "https://acme.awsapps.com/start"
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
    """Reload rolehop.cli with temporary paths and return (cli, paths)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ROLEHOP_CONFIG", str(tmp_path / "orgs.toml"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ROLEHOP_SSO_CACHE", str(tmp_path / "sso-cache"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    import rolehop.cli as cli
    cli = importlib.reload(cli)
    paths = type("P", (), {
        "home": home,
        "orgs": tmp_path / "orgs.toml",
        "aws": tmp_path / "aws-config",
        "state": tmp_path / "state" / "rolehop",
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
