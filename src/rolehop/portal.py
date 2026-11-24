"""AWS access portal API (standard library only) and login for discovery."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from rolehop import errors, paths, session, ui
from rolehop.i18n import _


class LoginNeeded(errors.PortalError):
    """The SSO session is missing or expired. ``str()`` adds the command that fixes it; ``reason`` omits it.

    ``expired`` is True when a token exists but has expired (or the portal rejected it): the AWS CLI may still be able
    to renew it with its refresh token. It is False when there is no token at all (never signed in).
    """

    def __init__(self, reason: str, org: str | None = None, *, expired: bool = False) -> None:
        self.reason = reason
        self.expired = expired
        super().__init__(f"{reason}; " + _("run: {cmd}", cmd=f"rolehop login {org}" if org else "rolehop login"))


def cached_token(session_name: str, margin: int = 0) -> str:
    """Return the SSO access token for an sso-session from the AWS CLI cache. Kept in memory only; never printed.

    ``margin`` treats a token that expires within that many seconds as already expired, so a long run can renew it
    first instead of failing halfway.
    """
    import hashlib
    path = paths.SSO_CACHE / (hashlib.sha1(session_name.encode(), usedforsecurity=False).hexdigest() + ".json")
    data = None
    for _attempt in range(2):  # the AWS CLI rewrites this file in place: a concurrent read can see it half-written
        try:
            data = json.loads(path.read_text())
            break
        except FileNotFoundError:
            break
        except (OSError, ValueError):
            import time
            time.sleep(0.2)
    if not isinstance(data, dict):
        raise LoginNeeded(_("no active session for '{org}'", org=session_name), session_name)
    token, expires = data.get("accessToken"), str(data.get("expiresAt", ""))
    if not token:
        raise LoginNeeded(_("the SSO cache has no token for '{org}'", org=session_name), session_name)
    try:
        exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00").replace("UTC", "+00:00"))
    except ValueError:
        return token
    if exp <= dt.datetime.now(dt.UTC) + dt.timedelta(seconds=margin):
        raise LoginNeeded(_("the access-portal token for '{org}' has expired", org=session_name), session_name,
                          expired=True)
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
            raise LoginNeeded(_("the access portal rejected the token (the session expired)"), expired=True) from None
        raise errors.PortalError(_("HTTP error {code} from the access portal", code=exc.code)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise errors.PortalError(_("could not reach the access portal: {error}", error=exc)) from None


def portal_inventory(session_name: str, region: str, margin: int = 0) -> list[dict]:
    """Return [{id, name, roles: [...]}] for every account and role visible to the user."""
    token = cached_token(session_name, margin)
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


def role_cache_file(org: dict, account_id: str, permission_set: str) -> Path:
    """The AWS CLI's cached-credentials file for one SSO role.

    Same key as botocore's SSOCredentialFetcher for sso-session profiles: sha1 of {"accountId", "roleName",
    "sessionName"} as compact, sorted JSON, in ~/.aws/cli/cache (rolehop names the sso-session after the org).
    """
    import hashlib
    key = json.dumps({"accountId": account_id, "roleName": permission_set, "sessionName": org["name"]},
                     sort_keys=True, separators=(",", ":"))
    return paths.CLI_CACHE / (hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest() + ".json")


def token_valid(org_name: str) -> bool:
    try:
        cached_token(org_name)
        return True
    except LoginNeeded:
        return False


def renew_access_token(org: dict) -> dict:
    """Make the AWS CLI renew an expired access-portal token with its own refresh token.

    The CLI renews the token only when it needs it, and it does not need it while a role's credentials are cached,
    so the token can be expired while STS still answers. Moving one SSO role's cached credentials aside forces the
    next STS call through the CLI's SSO token provider, which renews and saves the token itself; rolehop never reads
    or writes the token for this. If the token cannot be renewed (the portal session ended), the credentials are put
    back so they keep working until they expire.

    Returns {"renewed": bool, "code": <check code or None>, "error": <detail or None>}.
    """
    from rolehop import awsconfig, config
    sso = [r for r in config.catalog([org]) if r["type"] == "sso"]
    probe = config.probe_profile(org)
    row = next((r for r in sso if r["profile"] == probe), sso[0] if sso else None)  # a chained probe can't renew it
    if row is None or not awsconfig.profile_exists(row["profile"]):
        return {"renewed": False, "code": "needs_sync" if row else "no_profiles", "error": None}
    cached = role_cache_file(org, row["account_id"], row["permission_set"])
    backup: Path | None = cached.with_name(f"{cached.name}.rolehop-{os.getpid()}")
    ui.debug(f"renewing the access-portal token of {org['name']} through profile {row['profile']}")
    try:
        os.replace(cached, backup)  # type: ignore[arg-type]
    except FileNotFoundError:
        backup = None
    except OSError as exc:
        ui.debug(f"could not move {ui.short(cached)} aside: {exc}")
        return {"renewed": False, "code": None, "error": str(exc)}
    try:
        result = session.check_org({**org, "probe_profile": row["profile"]})
    finally:
        renewed = token_valid(org["name"])
        if backup is not None:
            if not renewed:
                try:  # put the still-valid credentials back, unless the CLI already wrote new ones
                    os.link(backup, cached)
                except FileExistsError:
                    pass
                except OSError:
                    os.replace(backup, cached)
            backup.unlink(missing_ok=True)
    return {"renewed": renewed, "code": result.get("code"), "error": result.get("error")}


RENEW_MARGIN = 300  # seconds: renew a token that expires sooner than this before a long portal read


def inventory(org: dict) -> list[dict]:
    """portal_inventory(), letting the AWS CLI renew an expired (or nearly expired) access-portal token once."""
    try:
        return portal_inventory(org["name"], org["sso_region"], margin=RENEW_MARGIN)
    except LoginNeeded as exc:
        if not exc.expired:  # never signed in: renewing can't help
            raise
        result = renew_access_token(org)
        if not result["renewed"] and result["code"] in ("network", "timeout"):
            raise errors.PortalError(_("could not reach AWS ({reason})", reason=result["error"] or "?")) from None
        return portal_inventory(org["name"], org["sso_region"])
