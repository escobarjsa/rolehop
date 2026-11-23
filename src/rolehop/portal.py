"""AWS access portal API (standard library only) and login for discovery."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess

from rolehop import errors, paths, session, ui
from rolehop.i18n import _


class LoginNeeded(errors.PortalError):
    """The SSO session is missing or expired. ``str()`` adds the command that fixes it; ``reason`` omits it."""

    def __init__(self, reason: str, org: str | None = None) -> None:
        self.reason = reason
        super().__init__(f"{reason}; " + _("run: {cmd}", cmd=f"rolehop login {org}" if org else "rolehop login"))


def cached_token(session_name: str) -> str:
    """Return the SSO access token for an sso-session from the AWS CLI cache. Kept in memory only; never printed."""
    import hashlib
    path = paths.SSO_CACHE / (hashlib.sha1(session_name.encode(), usedforsecurity=False).hexdigest() + ".json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise LoginNeeded(_("no active session for '{org}'", org=session_name), session_name) from None
    token, expires = data.get("accessToken"), data.get("expiresAt", "")
    if not token:
        raise LoginNeeded(_("the SSO cache has no token for '{org}'", org=session_name), session_name)
    try:
        exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00").replace("UTC", "+00:00"))
        if exp <= dt.datetime.now(dt.UTC):
            raise LoginNeeded(_("the token for '{org}' has expired", org=session_name), session_name)
    except ValueError:
        pass
    return token


def portal_get(region: str, path: str, token: str, params: dict) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request
    base = paths.env("PORTAL_ENDPOINT") or f"https://portal.sso.{region}.amazonaws.com"
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-amz-sso_bearer_token": token, "Accept": "application/json"})
    ui.debug(f"GET {base}{path} (bearer token not shown)")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed AWS URL)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LoginNeeded(_("the access portal rejected the token (the session expired)")) from None
        raise errors.PortalError(_("HTTP error {code} from the access portal", code=exc.code)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise errors.PortalError(_("could not reach the access portal: {error}", error=exc)) from None


def portal_inventory(session_name: str, region: str) -> list[dict]:
    """Return [{id, name, roles: [...]}] for every account and role visible to the user."""
    token = cached_token(session_name)
    accounts, nxt = [], None
    while True:
        params = {"max_result": 100} | ({"next_token": nxt} if nxt else {})
        page = portal_get(region, "/assignment/accounts", token, params)
        accounts += page.get("accountList", [])
        nxt = page.get("nextToken")
        if not nxt:
            break
    out = []
    for acc in sorted(accounts, key=lambda a: a.get("accountName", "")):
        roles, nxt = [], None
        while True:
            params = {"account_id": acc["accountId"], "max_result": 100} | ({"next_token": nxt} if nxt else {})
            page = portal_get(region, "/assignment/roles", token, params)
            roles += [r["roleName"] for r in page.get("roleList", [])]
            nxt = page.get("nextToken")
            if not nxt:
                break
        out.append({"id": acc["accountId"], "name": acc.get("accountName", acc["accountId"]), "roles": sorted(roles)})
    return out


def login_for_discovery(org: dict) -> bool:
    """Run SSO login against a temporary config holding only the sso-session (leaves ~/.aws/config alone)."""
    import tempfile
    block = (f"[sso-session {org['name']}]\nsso_start_url = {org['start_url']}\n"
             f"sso_region = {org['sso_region']}\nsso_registration_scopes = sso:account:access\n")
    with tempfile.NamedTemporaryFile("w", suffix=".aws-config", delete=False) as fh:
        fh.write(block)
        tmp = fh.name
    try:
        cmd = ["aws", "sso", "login", "--sso-session", org["name"]]
        if org.get("login_flow", "device") == "device":
            cmd.append("--use-device-code")
        ui.echo("  " + _("Running: {cmd}", cmd=" ".join(cmd)))
        ui.hint("  " + _("Approve the request in your browser."))
        rc = subprocess.run(cmd, env=os.environ | {"AWS_CONFIG_FILE": tmp}).returncode
    finally:
        os.unlink(tmp)
    if rc == 0:
        session.write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
    return rc == 0
