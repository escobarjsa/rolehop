"""`rolehop upgrade` against a mocked GitHub releases endpoint (no real network)."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rolehop import upgrade

WHEEL = b"PK\x03\x04 fake wheel bytes"
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()


class Releases(BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.routes.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        if body is not None:
            self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Releases)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("ROLEHOP_RELEASES_URL", f"{base}/latest")
    calls: list[list[str]] = []

    def fake_run(cmd):
        if cmd[-1].endswith(".whl"):  # the installer receives the verified local file
            with open(cmd[-1], "rb") as fh:
                calls.append([*cmd[:-1], hashlib.sha256(fh.read()).hexdigest()])
        else:
            calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    yield base, calls
    srv.shutdown()


def publish(base, tag, *, wheel=True, digest: str | None = WHEEL_SHA, sums: str | None = WHEEL_SHA, body=WHEEL):
    name = f"rolehop-{tag[1:]}-py3-none-any.whl"
    assets = []
    if wheel:
        asset = {"name": name, "browser_download_url": f"{base}/dl/{name}"}
        if digest:
            asset["digest"] = f"sha256:{digest}"
        assets.append(asset)
    routes = {f"/dl/{name}": body}
    if sums:
        assets.append({"name": "SHA256SUMS", "browser_download_url": f"{base}/dl/SHA256SUMS"})
        routes["/dl/SHA256SUMS"] = f"{sums}  {name}\n".encode()
    routes["/latest"] = json.dumps({"tag_name": tag, "html_url": f"{base}/{tag}", "assets": assets}).encode()
    Releases.routes = routes


def args(check=False, force=False):
    return type("A", (), {"check": check, "force": force})()


def test_parse_version():
    assert upgrade._parse("1.4.0") > upgrade._parse("1.3.10") > upgrade._parse("1.3.9")
    assert upgrade._parse("v2") == (2, 0, 0)


def test_already_latest(server, monkeypatch, capsys):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "9.9.9")
    publish(base, "v1.4.0")
    assert upgrade.cmd_upgrade(args()) == 0
    assert "already the latest" in capsys.readouterr().out and calls == []


def test_installs_the_verified_local_wheel(server, monkeypatch, capsys):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "uv")
    publish(base, "v1.4.0")
    assert upgrade.cmd_upgrade(args()) == 0
    assert calls == [["uv", "tool", "install", "--force", WHEEL_SHA]]
    assert "GitHub asset digest + SHA256SUMS" in capsys.readouterr().out


@pytest.mark.parametrize("change", [
    {"body": b"tampered"},                    # download does not match the published checksum
    {"digest": "0" * 64, "sums": None},       # wrong digest
    {"sums": "1" * 64},                       # digest and SHA256SUMS disagree
    {"digest": None, "sums": None},           # no checksum published at all
])
def test_refuses_unverifiable_wheels(server, monkeypatch, capsys, change):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "pipx")
    publish(base, "v1.4.0", **change)
    assert upgrade.cmd_upgrade(args()) == 1
    assert calls == [] and "Not upgrading" in capsys.readouterr().err


def test_sha256sums_alone_is_enough(server, monkeypatch):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "pipx")
    publish(base, "v1.4.0", digest=None)
    assert upgrade.cmd_upgrade(args()) == 0
    assert calls == [["pipx", "install", "--force", WHEEL_SHA]]


def test_check_does_not_download_or_install(server, monkeypatch, capsys):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "uv")
    publish(base, "v1.4.0")
    assert upgrade.cmd_upgrade(args(check=True)) == 0
    assert calls == [] and "rolehop upgrade" in capsys.readouterr().out


def test_without_wheel_falls_back_to_git_tag(server, monkeypatch):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: "uv")
    publish(base, "v1.4.0", wheel=False, sums=None)
    assert upgrade.cmd_upgrade(args()) == 0
    assert calls == [["uv", "tool", "install", "--force", "git+https://github.com/escobarjsa/rolehop@v1.4.0"]]


def test_unknown_installer_prints_command(server, monkeypatch, capsys):
    base, calls = server
    monkeypatch.setattr(upgrade, "__version__", "1.3.1")
    monkeypatch.setattr(upgrade, "installer", lambda: None)
    publish(base, "v1.4.0")
    assert upgrade.cmd_upgrade(args()) == 1
    assert "uv tool install --force http" in capsys.readouterr().out and calls == []


def test_no_releases_yet(server, capsys):
    base, _ = server
    Releases.routes = {}
    assert upgrade.cmd_upgrade(args()) == 1
    assert "no published releases" in capsys.readouterr().err


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
