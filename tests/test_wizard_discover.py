"""`rolehop init` wizard and `discover` against a fake SSO portal (no real network or credentials)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

ACCOUNTS = [("acme-devsecops", "111111111111", ["Administrator", "PowerUser"]),
            ("billing", "222222222222", ["Administrator", "ReadOnly"]),
            ("app-dev", "333333333333", ["Administrator"]),
            ("app-qa", "444444444444", ["Administrator"]),
            ("data", "555555555555", ["Administrator"]),
            ("sandbox", "666666666666", ["DataAnalyst"])]


class Portal(BaseHTTPRequestHandler):
    accounts = ACCOUNTS

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("x-amz-sso_bearer_token") != "FAKE-TOKEN":
            self.send_response(401)
            self.end_headers()
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/assignment/accounts":
            start = int(q.get("next_token", ["0"])[0])
            page = self.accounts[start:start + 4]
            body = {"accountList": [{"accountId": i, "accountName": n} for n, i, _ in page]}
            if start + 4 < len(self.accounts):
                body["nextToken"] = str(start + 4)
        else:
            roles = next(r for n, i, r in self.accounts if i == q["account_id"][0])
            body = {"roleList": [{"roleName": r} for r in roles]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def portal(env, monkeypatch):
    cli, p = env
    srv = HTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ROLEHOP_PORTAL_ENDPOINT", f"http://127.0.0.1:{srv.server_port}")
    p.cache.mkdir(parents=True)
    exp = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in ("acme", "personal"):
        (p.cache / f"{hashlib.sha1(name.encode()).hexdigest()}.json").write_text(
            json.dumps({"accessToken": "FAKE-TOKEN", "expiresAt": exp}))
    monkeypatch.setattr(cli, "SSO_CACHE", p.cache)
    monkeypatch.setattr(cli, "_login_for_discovery", lambda org: True)
    yield cli, p
    srv.shutdown()


def answer(monkeypatch, lines):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))


def test_init_discovers_and_creates_orgs(portal, monkeypatch, capsys):
    cli, p = portal
    answer(monkeypatch, ["acme", "https://acme.awsapps.com/start/#/", "", "", "8", "y", "yes", "n"])
    assert cli.main(["init"]) == 0
    orgs = cli.load_orgs()
    accounts = {a["name"]: a for a in orgs[0]["accounts"]}
    assert orgs[0]["start_url"] == "https://acme.awsapps.com/start"
    assert set(accounts) == {"devsecops", "billing", "app-dev", "app-qa", "data", "sandbox"}  # org prefix stripped
    assert accounts["devsecops"]["roles"] == ["admin", "power"]
    assert accounts["sandbox"]["roles"] == ["dataanalyst"]
    assert orgs[0]["default_role"] == "admin"
    assert cli.main(["sync", "--quiet"]) == 0


def test_init_refresh_picks_up_new_account(portal, monkeypatch):
    cli, p = portal
    # Spanish answers (s/sí) from older versions keep working.
    answer(monkeypatch, ["acme", "https://acme.awsapps.com/start", "", "", "8", "s", "sí", "n"])
    cli.main(["init"])
    monkeypatch.setattr(Portal, "accounts", ACCOUNTS + [("new", "777777777777", ["ReadOnly"])])
    answer(monkeypatch, ["2", "acme", "3"])
    assert cli.main(["init"]) == 0
    names = {a["name"] for a in cli.load_orgs()[0]["accounts"]}
    assert "new" in names and len(list(p.tmp.glob("orgs.toml.bak-*"))) == 1


def test_init_manual_entry_and_validation(env, monkeypatch, capsys):
    cli, p = env
    answer(monkeypatch, ["personal", "http://bad", "https://p.awsapps.com/start", "us-east-2", "", "12", "n",
                         "main", "123", "999999999999", "AdministratorAccess", "n", "n"])
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "must be https" in out and "12 digits" in out
    org = cli.load_orgs()[0]
    assert org["sso_region"] == "us-east-2" and org["accounts"][0]["roles"] == ["admin"]


def test_discover(portal, capsys):
    cli, p = portal
    p.orgs.write_text('[[org]]\nname = "acme"\nstart_url = "https://acme.awsapps.com/start"\n'
                      'sso_region = "us-east-1"\n[[org.accounts]]\nname = "x"\nid = "111111111111"\n'
                      'roles = ["Administrator"]\n')
    capsys.readouterr()
    assert cli.main(["discover", "acme"]) == 0
    out = capsys.readouterr().out
    assert out.count("[[org.accounts]]") == len(ACCOUNTS) and "TOKEN" not in out


def test_discover_without_token(env, capsys):
    cli, p = env
    p.orgs.write_text('[[org]]\nname = "acme"\nstart_url = "https://acme.awsapps.com/start"\n'
                      'sso_region = "us-east-1"\n[[org.accounts]]\nname = "x"\nid = "111111111111"\n'
                      'roles = ["Administrator"]\n')
    assert cli.main(["discover", "acme"]) == 1
    assert "awslogin acme" in capsys.readouterr().err


@pytest.mark.parametrize("reply, default, expected", [
    ("", True, True), ("", False, False),
    ("y", False, True), ("yes", False, True), ("Y", False, True),
    ("s", False, True), ("si", False, True), ("sí", False, True),
    ("n", True, False), ("no", True, False),
])
def test_yes_prompt_accepts_english_and_spanish(env, monkeypatch, capsys, reply, default, expected):
    cli, _ = env
    answer(monkeypatch, [reply])
    assert cli._yes("Continue?", default) is expected
    assert ("[Y/n]" if default else "[y/N]") in capsys.readouterr().out
