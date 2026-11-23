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
# Never signed in on this machine (no cached token), as opposed to a session that ran out.
NOT_SIGNED_IN_RE = re.compile(r"Token for \S+ does not exist|Error loading SSO Token: Token .* does not exist")
FORBIDDEN_RE = re.compile(r"ForbiddenException|AccessDenied|UnauthorizedException|No access")
NETWORK_RE = re.compile(r"Could not connect to the endpoint URL|EndpointConnectionError|Name or service not known"
                        r"|Temporary failure in name resolution|Connection (?:was )?(?:closed|reset|refused)")


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
    """Check an org's session with `aws sts get-caller-identity` on its probe profile.

    The result always has the same keys. "error" is the raw English detail (for --json and bug reports);
    "code" is stable and drives the message and the suggested fix:
    no_profiles, needs_sync, aws_missing, timeout, bad_response, not_signed_in, expired, forbidden, network,
    aws_error (anything else), or None when the session is active.
    """
    prof = config.probe_profile(org)
    res: dict = {"org": org["name"], "probe_profile": prof, "ok": False, "code": None, "expired": False,
                 "needs_sync": False, "blocked": False, "account": None, "arn": None, "error": None}
    st = read_state(org["name"])
    res["blocked"] = bool(st.get("blocked"))
    if st.get("last_login"):
        try:
            elapsed = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(st["last_login"])
            left = dt.timedelta(hours=float(org["portal_session_hours"])) - elapsed
            res["last_login"] = st["last_login"]
            res["estimated_left_min"] = max(int(left.total_seconds() // 60), 0)
        except (TypeError, ValueError):
            pass
    if not prof:
        res.update(code="no_profiles", error="no profiles defined")
        return res
    if not awsconfig.profile_exists(prof):  # orgs.toml has the org but `rolehop sync` has not written it yet
        res.update(code="needs_sync", needs_sync=True, error=f"profile {prof} is not in ~/.aws/config")
        return res
    try:
        ui.debug(f"$ aws sts get-caller-identity --profile {prof}")
        p = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", prof, "--output", "json"],
                           capture_output=True, text=True, timeout=30)
        ui.debug(f"  exit {p.returncode}")
    except FileNotFoundError:
        res.update(code="aws_missing", error="aws command not found")
        return res
    except subprocess.TimeoutExpired:
        res.update(code="timeout", error="timed out calling STS")
        return res
    if p.returncode == 0:
        try:
            info = json.loads(p.stdout)
            res.update(ok=True, account=info.get("Account"), arn=info.get("Arn"))
        except ValueError:
            res.update(code="bad_response", error="unexpected response from aws sts (not JSON)")
    else:
        stderr = p.stderr or p.stdout or ""
        lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        res["error"] = (lines[0] if lines else "unknown error")[:200]
        res["code"] = classify(stderr)
        res["needs_sync"] = res["code"] == "needs_sync"
        res["expired"] = res["code"] in ("expired", "not_signed_in")
    if res["blocked"]:  # a hook saw the session expire: don't trust cached role credentials
        res.update(ok=False, expired=True, code="expired")
        res["error"] = res["error"] or "session expired (detected by a recent command)"
    return res


def classify(stderr: str) -> str:
    """Stable code for an `aws sts` failure, from its stderr."""
    if NOT_FOUND_RE.search(stderr):
        return "needs_sync"
    if NOT_SIGNED_IN_RE.search(stderr):
        return "not_signed_in"
    if EXPIRED_RE.search(stderr):
        return "expired"
    if FORBIDDEN_RE.search(stderr):
        return "forbidden"
    if NETWORK_RE.search(stderr):
        return "network"
    return "aws_error"


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
