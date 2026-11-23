"""Per-organization session state and probing with the AWS CLI."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from rolehop import awsconfig, config, errors, paths, ui
from rolehop.i18n import _

# Actual AWS CLI / botocore / aws-sdk-go-v2 messages when the SSO session has ended.
EXPIRED_RE = re.compile(
    r"Error when retrieving token from sso|The SSO session associated with this profile has expired"
    r"|UnauthorizedSSOToken|Token has expired and refresh failed|refresh cached SSO token failed"
    r"|InvalidGrantException|SSO session.{0,40}(has expired|is invalid)|Error loading SSO Token"
    r"|ExpiredToken")
NOT_FOUND_RE = re.compile(r"The config profile \(.+?\) could not be found")


def state_file(org: str) -> Path:
    return paths.STATE_DIR / f"{org}.json"


def read_state(org: str) -> dict:
    try:
        return json.loads(state_file(org).read_text())
    except (OSError, ValueError):
        return {}


def write_state(org: str, **values) -> None:
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = read_state(org) | values
    state_file(org).write_text(json.dumps(data))


def check_org(org: dict) -> dict:
    """Check an org's session with `aws sts get-caller-identity` on its probe profile."""
    prof = config.probe_profile(org)
    res = {"org": org["name"], "probe_profile": prof, "ok": False, "expired": False,
           "account": None, "arn": None, "error": None}
    if not prof:
        res["error"] = "no profiles defined"
        return res
    if not awsconfig.profile_exists(prof):  # orgs.toml has the org but `rolehop sync` has not written it yet
        res["error"] = f"profile {prof} is not in {paths.AWS_CONFIG}"
        res["needs_sync"] = True
        return res
    try:
        ui.debug(f"$ aws sts get-caller-identity --profile {prof}")
        p = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", prof, "--output", "json"],
                           capture_output=True, text=True, timeout=30)
        ui.debug(f"  exit {p.returncode}")
    except FileNotFoundError:
        res["error"] = "aws command not found"
        return res
    except subprocess.TimeoutExpired:
        res["error"] = "timed out calling STS"
        return res
    if p.returncode == 0:
        try:
            info = json.loads(p.stdout)
            res.update(ok=True, account=info.get("Account"), arn=info.get("Arn"))
        except ValueError:
            res["error"] = "unexpected response from aws sts (not JSON)"
    else:
        err = (p.stderr or p.stdout).strip().splitlines()
        msg = next((line for line in err if line.strip()), "unknown error")
        res["error"] = msg[:200]
        res["expired"] = bool(EXPIRED_RE.search(p.stderr or ""))
        res["needs_sync"] = bool(NOT_FOUND_RE.search(p.stderr or ""))
    st = read_state(org["name"])
    if st.get("last_login"):
        try:
            elapsed = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(st["last_login"])
            left = dt.timedelta(hours=float(org["portal_session_hours"])) - elapsed
            res["last_login"] = st["last_login"]
            res["estimated_left_min"] = max(int(left.total_seconds() // 60), 0)
        except (TypeError, ValueError):
            pass
    res["blocked"] = bool(st.get("blocked"))
    if res["blocked"]:  # a hook saw the session expire: don't trust cached role credentials
        res["ok"] = False
        res["expired"] = True
        res["error"] = res["error"] or "session expired (detected by a recent command)"
    return res


def fmt_left(minutes: int | None) -> str:
    if minutes is None:
        return _("time left unknown")
    return _("~{time} left (estimated)", time=f"{minutes // 60}h{minutes % 60:02d}m")


def org_of_profile(profile: str) -> str | None:
    """Return the org a profile name belongs to, according to orgs.toml."""
    try:
        for row in config.catalog(config.load_orgs()):
            if row["profile"] == profile:
                return row["org"]
    except errors.ConfigError:
        return None
    return None
