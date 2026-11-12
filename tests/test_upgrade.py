"""`rolehop upgrade` against a mocked releases endpoint (no real network)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rolehop import upgrade


class Releases(BaseHTTPRequestHandler):
    payload: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps(self.payload).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def releases(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Releases)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ROLEHOP_RELEASES_URL", f"http://127.0.0.1:{srv.server_port}/latest")
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    yield calls
    srv.shutdown()


def release(tag, wheel=True):
    assets = [{"name": f"rolehop-{tag[1:]}-py3-none-any.whl",
               "browser_download_url": f"https://example.test/{tag}/rolehop-{tag[1:]}-py3-none-any.whl"}] if wheel else []
    return {"tag_name": tag, "html_url": f"https://example.test/{tag}", "assets": assets}


def test_parse_version():
    assert upgrade._parse("1.4.0") > upgrade._parse("1.3.10") > upgrade._parse("1.3.9")
    assert upgrade._parse("v2") == (2, 0, 0)


def test_already_latest(releases, monkeypatch, capsys):
    monkeypatch.setattr(upgrade, "__version__", "9.9.9")
    Releases.payload = release("v1.4.0")
    assert upgrade.cmd_upgrade(type("A", (), {"check": False, "force": False})()) == 0
    assert "already the latest" in capsys.readouterr().out and releases == []


def test_upgrades_with_uv_using_wheel(releases, monkeypatch):
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "uv")
    Releases.payload = release("v1.4.0")
    assert upgrade.cmd_upgrade(type("A", (), {"check": False, "force": False})()) == 0
    assert releases == [["uv", "tool", "install", "--force",
                         "https://example.test/v1.4.0/rolehop-1.4.0-py3-none-any.whl"]]


def test_check_does_not_install_and_uses_git_without_wheel(releases, monkeypatch, capsys):
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "pipx")
    Releases.payload = release("v1.4.0", wheel=False)
    assert upgrade.cmd_upgrade(type("A", (), {"check": True, "force": False})()) == 0
    out = capsys.readouterr().out
    assert releases == [] and "pipx install --force git+https://github.com/escobarjsa/rolehop@v1.4.0" in out


def test_unknown_installer_prints_command(releases, monkeypatch, capsys):
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: None)
    Releases.payload = release("v1.4.0")
    assert upgrade.cmd_upgrade(type("A", (), {"check": False, "force": False})()) == 1
    assert "uv tool install --force https://example.test/v1.4.0/" in capsys.readouterr().out and releases == []


def test_installer_detects_uv_and_pipx(monkeypatch):
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setattr(upgrade.sys, "prefix", "/home/u/.local/share/uv/tools/rolehop")
    assert upgrade.installer() == "uv"
    monkeypatch.setattr(upgrade.sys, "prefix", "/home/u/.local/share/pipx/venvs/rolehop")
    assert upgrade.installer() == "pipx"
    monkeypatch.setattr(upgrade.sys, "prefix", "/repo/.venv")
    monkeypatch.setenv("UV_TOOL_DIR", "")
    assert upgrade.installer() is None
    monkeypatch.setenv("UV_TOOL_DIR", "/opt/tools")
    monkeypatch.setattr(upgrade.sys, "prefix", "/opt/tools/rolehop")
    assert upgrade.installer() == "uv"
