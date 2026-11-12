"""rolehop: declarative AWS IAM Identity Center profiles for any number of organizations.

Generates native `sso-session` profiles in ~/.aws/config from orgs.toml. Login and token refresh are handled
by the AWS CLI and SDKs: rolehop never writes tokens or keys.

Subcommands:
  setup                        all-in-one: PATH, wizard if no config, profiles and AI agents
  init                         wizard: organizations, account and role discovery, orgs.toml
  sync [--dry-run] [--prune]   write the managed blocks to ~/.aws/config
  login [org ...] [--force]    run aws sso login only where needed (also: awslogin)
  status [org ...] [--json]    session status per org
  list [--json] [--org ORG]    catalog of available profiles
  run [--login] [--max 6h] [-- CMD]   check the session before leaving an agent to work
  agent list|install|uninstall|rules  instructions and hooks for AI agents
  envrc PROFILE [--state P]    print an .envrc for direnv
  discover ORG                 list an org's actual accounts and roles (requires a session)

Config: $ROLEHOP_CONFIG or ~/.config/rolehop/orgs.toml
Target: $AWS_CONFIG_FILE or ~/.aws/config
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - clear message when run on an old Python
    sys.exit("rolehop requires Python 3.11 or later (for tomllib).")
import tomllib  # noqa: E402

from rolehop import __version__ as VERSION  # noqa: E402
HOME = Path.home()
LEGACY_NAME = "awsorg"  # the tool's former name (up to 1.2.0): its files and markers are still recognized


def _env(name: str) -> str | None:
    """Return ROLEHOP_<name>, falling back to the legacy AWSORG_<name>."""
    return os.environ.get(f"ROLEHOP_{name}") or os.environ.get(f"{LEGACY_NAME.upper()}_{name}") or None


CONFIG_PATH = Path(_env("CONFIG") or HOME / ".config/rolehop/orgs.toml")
LEGACY_CONFIG_PATH = HOME / f".config/{LEGACY_NAME}/orgs.toml"
AWS_CONFIG = Path(os.environ.get("AWS_CONFIG_FILE") or HOME / ".aws/config")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "rolehop"
LEGACY_STATE_DIR = STATE_DIR.with_name(LEGACY_NAME)
MARK_NAMES = f"(?:rolehop|{LEGACY_NAME})"  # block marker names accepted when reading ~/.aws/config


def migrate_legacy() -> list[str]:
    """Copy awsorg's orgs.toml and state to the rolehop paths if they don't exist yet. Never deletes anything."""
    msgs = []
    if not _env("CONFIG") and not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.is_file():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
        msgs.append(f"✓ migrated {LEGACY_CONFIG_PATH} → {CONFIG_PATH} (original kept)")
    if not STATE_DIR.exists() and LEGACY_STATE_DIR.is_dir():
        shutil.copytree(LEGACY_STATE_DIR, STATE_DIR)
        msgs.append(f"✓ migrated state from {LEGACY_STATE_DIR} → {STATE_DIR}")
    return msgs
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Actual AWS CLI / botocore / aws-sdk-go-v2 messages when the SSO session has ended.
EXPIRED_RE = re.compile(
    r"Error when retrieving token from sso|The SSO session associated with this profile has expired"
    r"|UnauthorizedSSOToken|Token has expired and refresh failed|refresh cached SSO token failed"
    r"|InvalidGrantException|SSO session.{0,40}(has expired|is invalid)|Error loading SSO Token"
    r"|ExpiredToken")
# Values written to ~/.aws/config: no whitespace, newlines, brackets or comment characters.
VAL_RE = re.compile(r"^[^\s\[\]#;]+$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s\[\]#;]*)?$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ARN_RE = re.compile(r"^arn:aws[\w-]*:iam::\d{12}:role/[\w+=,.@/-]+$")


def begin_marker(org: str) -> str:
    # split_blocks accepts any text between "rolehop:<org> " and ">>>", so older (Spanish) markers still match.
    return f"# >>> rolehop:{org} (managed by rolehop, do not edit) >>>"


def end_marker(org: str) -> str:
    return f"# <<< rolehop:{org} <<<"


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def load_orgs(path: Path = CONFIG_PATH) -> list[dict]:
    """Load and validate orgs.toml, filling in defaults. Raise ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy orgs.example.toml there and edit it.")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    orgs = data.get("org", [])
    if not orgs:
        raise ConfigError(f"{path} does not define any [[org]].")
    seen = set()
    for org in orgs:
        if not isinstance(org, dict):
            raise ConfigError("Each [[org]] must be a table.")
        for key in ("name", "start_url", "sso_region"):
            if not isinstance(org.get(key), str) or not org.get(key):
                raise ConfigError(f"An org is missing '{key}' (a quoted string).")
        name = org["name"]
        if not NAME_RE.match(name):
            raise ConfigError(f"Invalid org name: {name!r} (use lowercase letters, digits, - or _).")
        if name in seen:
            raise ConfigError(f"Duplicate org: {name}")
        seen.add(name)
        org.setdefault("region", org["sso_region"])
        org.setdefault("login_flow", "device")
        org.setdefault("portal_session_hours", 8)
        org.setdefault("roles", {})
        org.setdefault("accounts", [])
        org.setdefault("assume_roles", [])
        if not URL_RE.match(org["start_url"]):
            raise ConfigError(f"[{name}] invalid start_url: {org['start_url']!r} (expected https://<name>.awsapps.com/start)")
        for key in ("sso_region", "region"):
            if not isinstance(org[key], str) or not REGION_RE.match(org[key]):
                raise ConfigError(f"[{name}] invalid {key}: {org[key]!r} (e.g. us-east-1)")
        if org["login_flow"] not in ("device", "pkce"):
            raise ConfigError(f"[{name}] login_flow must be \"device\" or \"pkce\".")
        if not isinstance(org["portal_session_hours"], (int, float)) or org["portal_session_hours"] <= 0:
            raise ConfigError(f"[{name}] portal_session_hours must be a number of hours.")
        if not isinstance(org["roles"], dict):
            raise ConfigError(f"[{name}] [org.roles] must be a table (alias = \"PermissionSet\").")
        for alias, ps in org["roles"].items():
            if not NAME_RE.match(alias) or not isinstance(ps, str) or not VAL_RE.match(ps):
                raise ConfigError(f"[{name}] invalid role in [org.roles]: {alias} = {ps!r}")
        if not isinstance(org["accounts"], list) or not isinstance(org["assume_roles"], list):
            raise ConfigError(f"[{name}] accounts and assume_roles must be arrays of tables.")
        for acc in org["accounts"]:
            if not isinstance(acc, dict) or not acc.get("name") or not acc.get("id"):
                raise ConfigError(f"[{name}] each account needs 'name' and 'id'.")
            if not isinstance(acc["name"], str) or not NAME_RE.match(acc["name"]):
                raise ConfigError(f"[{name}] invalid account name: {acc['name']!r} "
                                  "(a string of lowercase letters, digits, - or _)")
            if not re.fullmatch(r"\d{12}", str(acc["id"])):
                raise ConfigError(f"[{name}] invalid account id for {acc['name']}: {acc['id']!r} (must be 12 digits)")
            acc["id"] = str(acc["id"])
            if not isinstance(acc.get("roles"), list) or not acc["roles"]:
                raise ConfigError(f"[{name}] roles for account {acc['name']} must be a list, e.g. [\"admin\"].")
            for role in acc["roles"]:
                if not isinstance(role, str) or not VAL_RE.match(role):
                    raise ConfigError(f"[{name}] invalid role in account {acc['name']}: {role!r}")
            if "region" in acc and (not isinstance(acc["region"], str) or not REGION_RE.match(acc["region"])):
                raise ConfigError(f"[{name}] invalid region in account {acc['name']}: {acc['region']!r}")
        sources = {f"{a['name']}-{k}" for a in org["accounts"] for k, _ in role_pairs(org, a)}
        for ar in org["assume_roles"]:
            for k in ("name", "role_arn", "source"):
                if not isinstance(ar, dict) or not isinstance(ar.get(k), str) or not ar.get(k):
                    raise ConfigError(f"[{name}] each [[org.assume_roles]] needs '{k}'.")
            if not NAME_RE.match(ar["name"]):
                raise ConfigError(f"[{name}] invalid name in assume_roles: {ar['name']!r}")
            if not ARN_RE.match(ar["role_arn"]):
                raise ConfigError(f"[{name}] invalid role_arn: {ar['role_arn']!r}")
            if ar["source"] not in sources:
                raise ConfigError(f"[{name}] unknown source in assume_roles: {ar['source']!r} "
                                  f"(valid: {', '.join(sorted(sources))})")
            if "external_id" in ar and (not isinstance(ar["external_id"], str) or not VAL_RE.match(ar["external_id"])):
                raise ConfigError(f"[{name}] invalid external_id in assume_roles {ar['name']}")
            if "duration_seconds" in ar and not isinstance(ar["duration_seconds"], int):
                raise ConfigError(f"[{name}] duration_seconds must be an integer")
    for org in orgs:
        pp = org.get("probe_profile")
        if pp and pp not in {r["profile"] for r in catalog([org])}:
            raise ConfigError(f"[{org['name']}] probe_profile {pp!r} is not one of this org's profiles.")
    from collections import Counter
    dups = sorted(p for p, c in Counter(r["profile"] for r in catalog(orgs)).items() if c > 1)
    if dups:
        raise ConfigError(f"Duplicate profiles: {', '.join(dups)}. Check orgs.toml for repeated accounts or roles.")
    return orgs


def role_pairs(org: dict, acc: dict) -> list[tuple[str, str]]:
    """Return [(short_key, permission_set_name)] for an account."""
    pairs = []
    reverse = {ps: alias for alias, ps in org["roles"].items()}
    for role in acc["roles"]:
        if role in org["roles"]:
            pairs.append((role, org["roles"][role]))
        elif role in reverse:  # a permission set name that matches an alias → use the alias
            pairs.append((reverse[role], role))
        else:  # a raw permission set name
            key = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
            pairs.append((key, role))
    return pairs


def catalog(orgs: list[dict]) -> list[dict]:
    """Return one row per generated profile across all orgs."""
    rows = []
    for org in orgs:
        for acc in org["accounts"]:
            for key, ps in role_pairs(org, acc):
                rows.append({
                    "profile": f"{org['name']}-{acc['name']}-{key}",
                    "org": org["name"], "account": acc["name"], "account_id": acc["id"],
                    "role": key, "permission_set": ps, "type": "sso",
                    "region": acc.get("region", org["region"]),
                })
        for ar in org["assume_roles"]:
            rows.append({
                "profile": f"{org['name']}-{ar['name']}", "org": org["name"],
                "account": ar.get("account", ""), "account_id": ar["role_arn"].split(":")[4],
                "role": ar["role_arn"].split("/")[-1], "permission_set": "",
                "type": f"assume-role (from {org['name']}-{ar['source']})",
                "region": ar.get("region", org["region"]),
            })
    return rows


def probe_profile(org: dict) -> str | None:
    """Pick the profile used to check an org's session: probe_profile, then default_role, then the first one."""
    if org.get("probe_profile"):
        return org["probe_profile"]
    default = org.get("default_role")
    if default:
        for acc in org["accounts"]:
            if default in [k for k, _ in role_pairs(org, acc)]:
                return f"{org['name']}-{acc['name']}-{default}"
    for acc in org["accounts"]:
        pairs = role_pairs(org, acc)
        if pairs:
            return f"{org['name']}-{acc['name']}-{pairs[0][0]}"
    return None


# --------------------------------------------------------------------------- render
def render_block(org: dict) -> str:
    """Render an org's managed block (sso-session plus profiles) for ~/.aws/config."""
    n = org["name"]
    out = [begin_marker(n), f"[sso-session {n}]",
           f"sso_start_url = {org['start_url']}",
           f"sso_region = {org['sso_region']}",
           "sso_registration_scopes = sso:account:access", ""]
    for acc in org["accounts"]:
        for key, ps in role_pairs(org, acc):
            out += [f"[profile {n}-{acc['name']}-{key}]",
                    f"sso_session = {n}",
                    f"sso_account_id = {acc['id']}",
                    f"sso_role_name = {ps}",
                    f"region = {acc.get('region', org['region'])}", ""]
    for ar in org["assume_roles"]:
        src = f"{n}-{ar['source']}"
        out += [f"[profile {n}-{ar['name']}]",
                f"role_arn = {ar['role_arn']}",
                f"source_profile = {src}",
                f"role_session_name = rolehop-{n}-{ar['name']}"]
        if ar.get("external_id"):
            out.append(f"external_id = {ar['external_id']}")
        if ar.get("duration_seconds"):
            out.append(f"duration_seconds = {int(ar['duration_seconds'])}")
        out += [f"region = {ar.get('region', org['region'])}", ""]
    if out[-1] == "":
        out.pop()
    out.append(end_marker(n))
    return "\n".join(out)


def split_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Split the file into free text (with placeholders) and managed blocks."""
    blocks: dict[str, str] = {}
    pattern = re.compile(
        rf"^# >>> {MARK_NAMES}:(?P<org>[a-z0-9_-]+) .*?>>>\n(?P<body>.*?)^# <<< {MARK_NAMES}:(?P=org) <<<\n?",
        re.S | re.M)

    def repl(m: re.Match) -> str:
        if m["org"] in blocks:
            raise ConfigError(f"Block rolehop:{m['org']} appears twice in {AWS_CONFIG}. "
                              f"Keep only one (back up the file first) and try again.")
        blocks[m["org"]] = m.group(0).rstrip("\n")
        return f"\0ROLEHOP:{m['org']}\0\n"

    if "\0" in text:
        raise ConfigError(f"{AWS_CONFIG} contains null bytes: check it or restore a .bak backup.")
    free = pattern.sub(repl, text)
    leftover = re.findall(rf"^# (?:>>>|<<<) {MARK_NAMES}:([a-z0-9_-]+)", free, re.M)
    if leftover:
        raise ConfigError(f"Incomplete or tampered rolehop markers for: {', '.join(sorted(set(leftover)))} "
                          f"in {AWS_CONFIG}. Fix or remove that block by hand (back up the file first).")
    return free, blocks


SECTION_RE = re.compile(r"^\[(?:profile\s+)?(?P<name>[^\]]+?)\]\s*$", re.M)
SSO_SESSION_RE = re.compile(r"^\[sso-session\s+(?P<name>[^\]]+?)\]\s*$", re.M)


def conflicts(free_text: str, org: dict) -> list[str]:
    """Return sections outside managed blocks that would clash with the generated ones."""
    clean = re.sub(r"\0ROLEHOP:[^\0]+\0", "", free_text)
    existing_profiles = {m["name"].strip() for m in SECTION_RE.finditer(clean)
                         if not m.group(0).startswith("[sso-session")}
    existing_sessions = {m["name"].strip() for m in SSO_SESSION_RE.finditer(clean)}
    wanted = {r["profile"] for r in catalog([org])}
    found = sorted(wanted & existing_profiles)
    if org["name"] in existing_sessions:
        found.insert(0, f"sso-session {org['name']}")
    return found


# --------------------------------------------------------------------------- sync
def drop_placeholder(text: str, token: str) -> str:
    """Remove a block's placeholder.

    Only the BLANK line that separated it is absorbed, never the newline ending the user's previous line,
    so the following section is not glued onto it.
    """
    i = text.find(token)
    if i < 0:
        return text
    head, tail = text[:i], text[i + len(token):]
    if head.endswith("\n\n"):
        head = head[:-1]
    elif not head:
        tail = tail.lstrip("\n")
    return head + tail


def plan_sync(orgs: list[dict], current: str, prune: bool = False,
              prune_only: set[str] | frozenset[str] = frozenset()) -> tuple[str | None, list[str], dict]:
    """Compute the new ~/.aws/config without writing anything. Return (text, kept orphan blocks, conflicts).

    If there are conflicts, text is None. Raise ConfigError if the markers are broken or the result is not valid INI.
    """
    free, blocks = split_blocks(current)
    problems = {o["name"]: c for o in orgs if (c := conflicts(free, o))}
    if problems:
        return None, [], problems
    wanted = {o["name"]: render_block(o) for o in orgs}
    stale = [n for n in blocks if n not in wanted]
    new_free = free
    for name, body in wanted.items():
        token = f"\0ROLEHOP:{name}\0\n"
        if token in new_free:
            new_free = new_free.replace(token, body + "\n", 1)
        else:
            if new_free and not new_free.endswith("\n"):
                new_free += "\n"
            if new_free and not new_free.endswith("\n\n"):
                new_free += "\n"
            new_free += body + "\n"
    kept = []
    for name in stale:
        token = f"\0ROLEHOP:{name}\0\n"
        if prune or name in prune_only:
            new_free = drop_placeholder(new_free, token)
        else:
            new_free = new_free.replace(token, blocks[name] + "\n", 1)
            kept.append(name)
    if "\0" in new_free:
        raise ConfigError("internal error: the result contains unresolved placeholders. Nothing was written.")
    import configparser
    try:
        configparser.RawConfigParser(strict=True, interpolation=None).read_string(new_free)
    except configparser.Error as exc:
        raise ConfigError(f"the result would not be a valid ~/.aws/config; nothing was written: {exc}") from None
    return new_free, kept, {}


def cmd_sync(args) -> int:
    orgs = load_orgs()
    current = AWS_CONFIG.read_text() if AWS_CONFIG.exists() else ""
    new_free, kept, problems = plan_sync(orgs, current, args.prune, set(getattr(args, "prune_only", None) or []))
    if problems:
        print("✗ Some sections in your config, outside the rolehop blocks, conflict with generated profiles:",
              file=sys.stderr)
        for org, items in problems.items():
            for it in items:
                print(f"    [{org}] {it}", file=sys.stderr)
        print("  Rename or remove them by hand (back up the file first), then run `rolehop sync` again.",
              file=sys.stderr)
        return 2
    assert new_free is not None
    if kept:
        print(f"! Blocks for orgs no longer in orgs.toml (kept; use --prune to remove them): "
              f"{', '.join(kept)}")
    total = sum(len(catalog([o])) for o in orgs)
    if new_free == current:
        print(f"✓ {AWS_CONFIG} already up to date ({total} profile(s)).")
        return 0
    if not args.quiet:
        diff = difflib.unified_diff(current.splitlines(True), new_free.splitlines(True),
                                    fromfile=str(AWS_CONFIG), tofile=f"{AWS_CONFIG} (new)")
        sys.stdout.writelines(diff)
    if args.dry_run:
        print("\n(dry run: nothing was written)")
        return 0
    write_aws_config(new_free)
    print(f"✓ {AWS_CONFIG} updated: {total} profile(s) in {len(orgs)} org(s).")
    return 0


def write_aws_config(text: str) -> None:
    """Write ~/.aws/config atomically (0600, fsync), with a backup, following symlinks."""
    target = AWS_CONFIG.resolve() if AWS_CONFIG.is_symlink() else AWS_CONFIG
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
        print(f"\n✓ Backup: {backup}")
    tmp = target.with_name(target.name + ".rolehop-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


# --------------------------------------------------------------------------- status / login
def state_file(org: str) -> Path:
    return STATE_DIR / f"{org}.json"


def read_state(org: str) -> dict:
    try:
        return json.loads(state_file(org).read_text())
    except (OSError, ValueError):
        return {}


def write_state(org: str, **values) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = read_state(org) | values
    state_file(org).write_text(json.dumps(data))


def check_org(org: dict) -> dict:
    """Check an org's session with `aws sts get-caller-identity` on its probe profile."""
    prof = probe_profile(org)
    res = {"org": org["name"], "probe_profile": prof, "ok": False, "expired": False,
           "account": None, "arn": None, "error": None}
    if not prof:
        res["error"] = "no profiles defined"
        return res
    try:
        p = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", prof, "--output", "json"],
                           capture_output=True, text=True, timeout=30)
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


def select(orgs: list[dict], names: list[str]) -> list[dict]:
    """Return the named orgs (all of them if names is empty). Raise ConfigError on unknown names."""
    if not names:
        return orgs
    by = {o["name"]: o for o in orgs}
    missing = [n for n in names if n not in by]
    if missing:
        raise ConfigError(f"Unknown org: {', '.join(missing)}. Available: {', '.join(by)}")
    return [by[n] for n in names]


def fmt_left(minutes: int | None) -> str:
    if minutes is None:
        return "no login data"
    return f"~{minutes // 60}h{minutes % 60:02d}m estimated"


def cmd_status(args) -> int:
    orgs = select(load_orgs(), args.orgs)
    results = [check_org(o) for o in orgs]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["ok"]:
                print(f"✓ {r['org']:<12} active session · account {r['account']} · "
                      f"{fmt_left(r.get('estimated_left_min'))} · checked with {r['probe_profile']}")
            else:
                hint = f"run: awslogin {r['org']}" if r["expired"] else r["error"]
                print(f"✗ {r['org']:<12} no session · {hint}")
        if any(r["ok"] for r in results):
            print("  (Cached role credentials can show ✓ even after the portal session has ended.)")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_login(args) -> int:
    orgs = select(load_orgs(), args.orgs)
    rc = 0
    if not shutil.which("aws"):
        raise ConfigError("aws command not found (install AWS CLI v2).")
    for org in orgs:
        if not args.force and not read_state(org["name"]).get("blocked"):
            r = check_org(org)
            if r["ok"]:
                print(f"✓ {org['name']}: already has a session ({fmt_left(r.get('estimated_left_min'))}). "
                      f"Use --force to renew it.")
                continue
        device = args.device or org["login_flow"] == "device"
        cmd = ["aws", "sso", "login", "--sso-session", org["name"]]
        if device:
            cmd.append("--use-device-code")
        print(f"→ {org['name']}: {' '.join(cmd)}")
        p = subprocess.run(cmd)
        if p.returncode == 0:
            write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
            r = check_org(org)
            print(f"✓ {org['name']}: logged in" + (f" · account {r['account']}" if r["ok"] else ""))
        else:
            print(f"✗ {org['name']}: login failed (exit code {p.returncode}).", file=sys.stderr)
            rc = 1
    return rc


# --------------------------------------------------------------------------- list / envrc / discover
def cmd_list(args) -> int:
    orgs = load_orgs()
    if args.org:
        orgs = select(orgs, [args.org])
    rows = catalog(orgs)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No profiles defined.")
        return 0
    w = max([7] + [len(r["profile"]) for r in rows])
    wo = max([3] + [len(r["org"]) for r in rows])
    wa = max([7] + [len(r["account"]) for r in rows])
    print(f"{'PROFILE':<{w}}  {'ORG':<{wo}}  {'ACCOUNT':<{wa}}  {'ID':<12}  ROLE")
    for r in rows:
        role = r["permission_set"] or r["role"]
        extra = "" if r["type"] == "sso" else f"  [{r['type']}]"
        print(f"{r['profile']:<{w}}  {r['org']:<{wo}}  {r['account']:<{wa}}  {r['account_id']:<12}  {role}{extra}")
    return 0


def cmd_envrc(args) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.state_var):
        print(f"✗ Invalid variable name: {args.state_var}", file=sys.stderr)
        return 2
    names = {r["profile"] for r in catalog(load_orgs())}
    for p in [args.profile] + ([args.state] if args.state else []):
        if p not in names:
            print(f"✗ Unknown profile: {p}. See `rolehop list`.", file=sys.stderr)
            return 2
    print("# .envrc generated by rolehop: default AWS profile for this repo.")
    print("# AI agents can use another profile by prefixing a command with AWS_PROFILE=<profile>.")
    print(f"export AWS_PROFILE={args.profile}")
    if args.state:
        print(f"export {args.state_var}={args.state}")
    print("unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN")
    return 0


# --------------------------------------------------------------------------- portal SSO (stdlib)
SSO_CACHE = Path(_env("SSO_CACHE") or HOME / ".aws/sso/cache")


class PortalError(Exception):
    pass


def _cached_token(session_name: str) -> str:
    """Return the SSO access token for an sso-session from the AWS CLI cache. Kept in memory only; never printed."""
    import hashlib
    path = SSO_CACHE / (hashlib.sha1(session_name.encode(), usedforsecurity=False).hexdigest() + ".json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise PortalError(f"no active session for '{session_name}' (run: awslogin {session_name})") from None
    token, expires = data.get("accessToken"), data.get("expiresAt", "")
    if not token:
        raise PortalError(f"SSO cache has no token for '{session_name}'")
    try:
        exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00").replace("UTC", "+00:00"))
        if exp <= dt.datetime.now(dt.UTC):
            raise PortalError(f"the token for '{session_name}' has expired (run: awslogin {session_name})")
    except ValueError:
        pass
    return token


def _portal_get(region: str, path: str, token: str, params: dict) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request
    base = _env("PORTAL_ENDPOINT") or f"https://portal.sso.{region}.amazonaws.com"
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-amz-sso_bearer_token": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed AWS URL)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PortalError("the portal rejected the token (session expired); run: awslogin") from None
        raise PortalError(f"HTTP error {exc.code} from the SSO portal") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PortalError(f"could not reach the SSO portal: {exc}") from None


def portal_inventory(session_name: str, region: str) -> list[dict]:
    """Return [{id, name, roles: [...]}] for every account and role visible to the user."""
    token = _cached_token(session_name)
    accounts, nxt = [], None
    while True:
        params = {"max_result": 100} | ({"next_token": nxt} if nxt else {})
        page = _portal_get(region, "/assignment/accounts", token, params)
        accounts += page.get("accountList", [])
        nxt = page.get("nextToken")
        if not nxt:
            break
    out = []
    for acc in sorted(accounts, key=lambda a: a.get("accountName", "")):
        roles, nxt = [], None
        while True:
            params = {"account_id": acc["accountId"], "max_result": 100} | ({"next_token": nxt} if nxt else {})
            page = _portal_get(region, "/assignment/roles", token, params)
            roles += [r["roleName"] for r in page.get("roleList", [])]
            nxt = page.get("nextToken")
            if not nxt:
                break
        out.append({"id": acc["accountId"], "name": acc.get("accountName", acc["accountId"]), "roles": sorted(roles)})
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "account"


def role_alias(ps_name: str) -> str:
    low = ps_name.lower()
    if low in ("administrator", "administratoraccess", "admin", "adminaccess"):
        return "admin"
    if low.startswith("poweruser"):
        return "power"
    if low in ("readonly", "readonlyaccess", "viewonly", "viewonlyaccess"):
        return "ro"
    return slug(ps_name)


def short_account_name(account_name: str, org: str) -> str:
    s = slug(account_name)
    for pre in (f"{org}-", f"{org}_"):
        if s.startswith(pre) and len(s) > len(pre):
            return s[len(pre):]
    return s


def cmd_discover(args) -> int:
    org = select(load_orgs(), [args.org])[0]
    try:
        inv = portal_inventory(org["name"], org["sso_region"])
    except PortalError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"# Accounts and roles visible in {org['name']} ({len(inv)}). Paste into orgs.toml and adjust the names.")
    for acc in inv:
        print(f'[[org.accounts]]\nname = "{short_account_name(acc["name"], org["name"])}"   # {acc["name"]}\n'
              f'id = "{acc["id"]}"\nroles = {json.dumps(acc["roles"])}\n')
    return 0


# --------------------------------------------------------------------------- wizard (init)
def _ask(prompt: str, default: str | None = None, validate=None, error: str = "invalid value") -> str:
    """Prompt until the answer passes validate; an empty answer takes the default."""
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise ConfigError("input closed: run the wizard in an interactive terminal") from None
        if not val and default is not None:
            val = default
        if val and (validate is None or validate(val)):
            return val
        print(f"    ✗ {error}")


def _yes(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. Accepts y/yes and, for compatibility, s/si/sí."""
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not val else val.startswith(("y", "s"))


def toml_dump(orgs: list[dict]) -> str:
    """Serialize orgs to orgs.toml text."""
    q = json.dumps
    lines = ["# orgs.toml - AWS IAM Identity Center organizations for rolehop.",
             "# Generated/updated by `rolehop init`. You can edit it by hand; then run `rolehop sync`.",
             "# Documentation: https://github.com/escobarjsa/rolehop", ""]
    for org in orgs:
        lines += ["[[org]]", f"name = {q(org['name'])}", f"start_url = {q(org['start_url'])}",
                  f"sso_region = {q(org['sso_region'])}", f"region = {q(org.get('region', org['sso_region']))}",
                  f"login_flow = {q(org.get('login_flow', 'device'))}",
                  f"portal_session_hours = {org.get('portal_session_hours', 8)}"]
        if org.get("default_role"):
            lines.append(f"default_role = {q(org['default_role'])}")
        if org.get("probe_profile"):
            lines.append(f"probe_profile = {q(org['probe_profile'])}")
        lines += ["", "[org.roles]"] + [f"{k} = {q(v)}" for k, v in org.get("roles", {}).items()] + [""]
        for acc in org.get("accounts", []):
            lines += ["[[org.accounts]]", f"name = {q(acc['name'])}", f"id = {q(str(acc['id']))}",
                      f"roles = {q(list(acc['roles']))}"]
            if acc.get("region"):
                lines.append(f"region = {q(acc['region'])}")
            lines.append("")
        for ar in org.get("assume_roles", []):
            lines.append("[[org.assume_roles]]")
            for k, v in ar.items():
                lines.append(f"{k} = {v if isinstance(v, int) else q(str(v))}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_orgs(orgs: list[dict]) -> None:
    text = toml_dump(orgs)
    tomllib.loads(text)  # sanity check: never write broken TOML
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"  ✓ Backup: {backup}")
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    print(f"  ✓ Saved {CONFIG_PATH}")


def _login_for_discovery(org: dict) -> bool:
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
        print(f"  → {' '.join(cmd)}  (approve in your browser)")
        rc = subprocess.run(cmd, env=os.environ | {"AWS_CONFIG_FILE": tmp}).returncode
    finally:
        os.unlink(tmp)
    if rc == 0:
        write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
    return rc == 0


def _alias_for(ps: str, roles_map: dict) -> str:
    """Return the [org.roles] alias for a permission set, creating a non-clashing one if missing."""
    alias = next((k for k, v in roles_map.items() if v == ps), None)
    if alias is None:
        alias = role_alias(ps)
        if alias in roles_map:
            alias = slug(ps)
        base, n = alias, 2
        while alias in roles_map:
            alias, n = f"{base}-{n}", n + 1
        roles_map[alias] = ps
    return alias


def _accounts_from_inventory(org_name: str, inv: list[dict], roles_map: dict, ask_names: bool) -> list[dict]:
    accounts, used = [], set()
    for acc in inv:
        aliases = [_alias_for(ps, roles_map) for ps in acc["roles"]]
        name = short_account_name(acc["name"], org_name)
        if ask_names:
            name = _ask(f"Short name for '{acc['name']}' ({acc['id']})", name, NAME_RE.match,
                        "use lowercase letters, digits, - or _")
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        accounts.append({"name": name, "id": acc["id"], "roles": aliases})
    return accounts


def _manual_accounts(roles_map: dict) -> list[dict]:
    print("  Enter your accounts (see the access portal: name, 12-digit ID, and the roles under each account).")
    accounts = []
    while True:
        name = _ask("Account short name (e.g. devsecops)", None, NAME_RE.match, "use lowercase letters, digits, - or _")
        acc_id = _ask("Account ID (12 digits)", None, lambda v: re.fullmatch(r"\d{12}", v), "must be 12 digits")
        roles_txt = _ask("Roles, comma-separated, exactly as shown in the portal (e.g. Administrator, ReadOnly)")
        aliases = []
        for ps in [r.strip() for r in roles_txt.split(",") if r.strip()]:
            alias = next((k for k, v in roles_map.items() if v == ps), None) or role_alias(ps)
            roles_map.setdefault(alias, ps)
            aliases.append(alias)
        accounts.append({"name": name, "id": acc_id, "roles": aliases})
        if not _yes("Add another account?", False):
            return accounts


def _print_accounts(org_name: str, accounts: list[dict], roles_map: dict) -> None:
    for acc in accounts:
        roles = ", ".join(f"{a} ({roles_map.get(a, a)})" for a in acc["roles"])
        print(f"    {org_name}-{acc['name']:<20} {acc['id']}  {roles}")


def _wizard_new_org(existing: list[dict]) -> dict:
    taken = {o["name"] for o in existing}
    print("\n  New organization. Find these details in the AWS access portal (the URL in your browser's address bar).")
    name = _ask("Organization short name (e.g. acme, personal)", None,
                lambda v: NAME_RE.match(v) and v not in taken, "use lowercase letters, digits, - or _, and a new name")
    url = _ask("Access portal URL (https://xxxx.awsapps.com/start)", None,
               lambda v: re.match(r"^https://[^\s/]+(/start)?/?(#.*)?$", v), "must be https://<name>.awsapps.com/start")
    url = re.sub(r"[/#].*$", "", url.split("://", 1)[1])
    url = f"https://{url}/start"
    sso_region = _ask("Identity Center region", "us-east-1", lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v),
                      "expected a region, e.g. us-east-1")
    region = _ask("Default region for profiles", sso_region,
                  lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v), "expected a region")
    hours = _ask("Portal session length in hours (if unsure, 8)", "8",
                 lambda v: re.fullmatch(r"\d{1,4}(\.\d+)?", v), "expected a number of hours")
    org = {"name": name, "start_url": url, "sso_region": sso_region, "region": region, "login_flow": "device",
           "portal_session_hours": float(hours) if "." in hours else int(hours), "roles": {}, "accounts": []}
    if _yes("Log in now and discover your accounts and roles automatically? (recommended)"):
        try:
            if not _login_for_discovery(org):
                raise PortalError("login did not complete")
            inv = portal_inventory(name, sso_region)
            if not inv:
                raise PortalError("the portal returned no accounts")
            org["accounts"] = _accounts_from_inventory(name, inv, org["roles"], ask_names=False)
            print(f"\n  Found {len(inv)} account(s):")
            _print_accounts(name, org["accounts"], org["roles"])
            if not _yes("Use these short names for the profiles?"):
                org["roles"] = {}
                org["accounts"] = _accounts_from_inventory(name, inv, org["roles"], ask_names=True)
        except PortalError as exc:
            print(f"  ! Automatic discovery failed ({exc}). Enter the accounts manually.")
            org["accounts"] = _manual_accounts(org["roles"])
    else:
        org["accounts"] = _manual_accounts(org["roles"])
    aliases = sorted({a for acc in org["accounts"] for a in acc["roles"]})
    org["default_role"] = "admin" if "admin" in aliases else (aliases[0] if aliases else "")
    return org


def _wizard_refresh_org(org: dict) -> None:
    print(f"\n  Refreshing accounts and roles for '{org['name']}' from the portal.")
    if not _login_for_discovery(org):
        raise ConfigError("login did not complete")
    try:
        inv = portal_inventory(org["name"], org["sso_region"])
    except PortalError as exc:
        raise ConfigError(str(exc)) from None
    roles_map = org.setdefault("roles", {})
    by_id = {str(a["id"]): a for a in org.setdefault("accounts", [])}
    added, changed = [], []
    new_inv = [a for a in inv if a["id"] not in by_id]
    for acc in inv:
        if acc["id"] in by_id:
            cur = by_id[acc["id"]]
            fresh = _accounts_from_inventory(org["name"], [acc], roles_map, ask_names=False)[0]["roles"]
            extra = [r for r in fresh if r not in cur["roles"]]
            if extra:
                cur["roles"] = list(cur["roles"]) + extra
                changed.append(f"{cur['name']}: +{', '.join(extra)}")
    if new_inv:
        names_used = {a["name"] for a in org["accounts"]}
        for acc in _accounts_from_inventory(org["name"], new_inv, roles_map, ask_names=False):
            base, n = acc["name"], 2
            while acc["name"] in names_used:
                acc["name"], n = f"{base}-{n}", n + 1
            names_used.add(acc["name"])
            org["accounts"].append(acc)
            added.append(f"{acc['name']} ({acc['id']})")
    if not added and not changed:
        print("  ✓ No changes: your orgs.toml already has every account and role.")
    for a in added:
        print(f"    + account {a}")
    for c in changed:
        print(f"    + roles {c}")


def cmd_init(args) -> int:
    print("\n=== rolehop wizard: AWS IAM Identity Center organizations ===")
    orgs: list[dict] = []
    if CONFIG_PATH.exists():
        try:
            orgs = load_orgs()
        except ConfigError as exc:
            print(f"  ✗ Your {CONFIG_PATH} has a problem: {exc}\n  Fix it or move it aside, then run this again.")
            return 2
        print(f"  You already have {CONFIG_PATH} with: {', '.join(o['name'] for o in orgs)}")
        print("  (If you edited it by hand, comments will be lost on save; a backup is made first.)")
    changed = False
    while True:
        if orgs:
            print("\n  What would you like to do?\n    1) Add another organization\n"
                  "    2) Refresh an organization's accounts/roles (from the portal)\n    3) Finish")
            choice = _ask("Choice", "3", lambda v: v in ("1", "2", "3"), "choose 1, 2 or 3")
        else:
            choice = "1"
        if choice == "3":
            break
        if choice == "2":
            name = _ask("Which organization?", orgs[0]["name"], lambda v: v in {o["name"] for o in orgs},
                        f"choose one of: {', '.join(o['name'] for o in orgs)}")
            _wizard_refresh_org(next(o for o in orgs if o["name"] == name))
            changed = True
            continue
        org = _wizard_new_org(orgs)
        if not org["accounts"]:
            print("  ! No accounts: skipping this organization.")
        else:
            orgs.append(org)
            changed = True
        if not _yes("Add another organization?", False):
            break
    if not changed:
        print("  No changes.")
        return 0
    _write_orgs(orgs)
    load_orgs()  # final check with the same rules as sync
    print("\n  Next step: `rolehop sync --dry-run`, then `rolehop sync` (or let the installer do it for you).")
    return 0


def cmd_mark(args) -> int:
    """Internal, for hooks: set or clear an org's 'session expired' state."""
    if not NAME_RE.match(args.org):
        return 0
    try:
        names = {o["name"] for o in load_orgs()}
    except ConfigError:
        names = set()
    if names and args.org not in names:
        return 0
    write_state(args.org, blocked=(args.state == "blocked"),
                blocked_at=dt.datetime.now(dt.UTC).isoformat() if args.state == "blocked" else None)
    return 0


def org_of_profile(profile: str) -> str | None:
    """Return the org a profile name belongs to, according to orgs.toml."""
    try:
        for row in catalog(load_orgs()):
            if row["profile"] == profile:
                return row["org"]
    except ConfigError:
        return None
    return None


def cmd_which_org(args) -> int:
    org = org_of_profile(args.profile)
    if org:
        print(org)
        return 0
    return 1


# --------------------------------------------------------------------------- run / hook / agent / setup
def cmd_run(args) -> int:
    from rolehop import runner
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return runner.run(args.org or [], args.max, args.login, args.yes, cmd)


def cmd_hook(args) -> int:
    from rolehop import hooks
    try:
        if args.kind == "post-tool":
            return hooks.post_tool(args.format)
        return hooks.cwd_direnv()
    except Exception:  # noqa: BLE001 - a hook must never break the agent's session
        return 0


def cmd_agent(args) -> int:
    from rolehop import agents
    home = Path.home()
    if args.action == "rules":
        print(agents.rules_text())
        return 0
    if args.action == "list":
        rows = [agents.status(a, home) for a in agents.AGENTS.values()]
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print(f"{'AGENT':<10} {'DETECTED':<10} {'INSTALLED':<10} {'HOOKS':<8} INSTRUCTIONS")
        for r in rows:
            print(f"{r['agent']:<10} {'yes' if r['detected'] else '-':<10} {'yes' if r['installed'] else '-':<10} "
                  f"{r['hooks']:<8} {r['rules']}")
        return 0
    try:
        targets = agents.resolve(args.names, args.all, home)
    except KeyError as exc:
        print(f"✗ {exc.args[0]}", file=sys.stderr)
        return 2
    if not targets:
        print("No installed AI agents detected. Name them explicitly: rolehop agent install claude codex …")
        return 1
    ch = agents.Changes(args.dry_run)
    for a in targets:
        if args.action == "install":
            agents.install(a, home, ch, with_hooks=not args.no_hooks)
        else:
            agents.uninstall(a, home, ch)
    for line in ch.lines:
        print(f"  {line}")
    if args.action == "install" and not args.dry_run:
        print("  Restart your agents so they load the new instructions.")
    return 1 if any(line.startswith("✗") for line in ch.lines) else 0


def cmd_setup(args) -> int:
    from rolehop import shellrc
    print("==> 1. Shell (PATH and direnv)")
    for m in shellrc.ensure_shell_integration(dry_run=args.dry_run):
        print(f"  {m}")
    print("==> 2. Organizations")
    if not CONFIG_PATH.exists():
        if args.dry_run or not sys.stdin.isatty():
            print(f"  ! {CONFIG_PATH} not found. Run `rolehop init` (wizard), then `rolehop setup` again.")
            return 0 if args.dry_run else 1
        rc = cmd_init(args)
        if rc != 0 or not CONFIG_PATH.exists():
            return rc or 1
    else:
        print(f"  ✓ Using {CONFIG_PATH}")
    print("==> 3. Profiles in ~/.aws/config")
    rc = cmd_sync(argparse.Namespace(dry_run=args.dry_run, prune=False, quiet=True))
    if rc != 0:
        return rc
    print("==> 4. AI agents")
    if args.no_agents:
        print("  (skipped: --no-agents)")
    else:
        rc = cmd_agent(argparse.Namespace(action="install", names=args.agents or [], all=False,
                                          dry_run=args.dry_run, no_hooks=False, json=False))
        if rc == 1 and not args.agents:
            print("  (no agents detected; install later with: rolehop agent install <agent>)")
    print("==> Done")
    print('  Next: awslogin  ·  rolehop status  ·  tell your agent "use whatever AWS profiles and roles you need"')
    return 0


def awslogin_main() -> int:
    return main(["login", *sys.argv[1:]])


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rolehop", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"rolehop {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True,
                          metavar="{setup,init,sync,login,status,list,refresh,remove,run,agent,envrc,discover,upgrade,uninstall}")

    s = sub.add_parser("sync", help="write the profiles to ~/.aws/config")
    s.add_argument("--dry-run", action="store_true", help="show the changes without writing anything")
    s.add_argument("--prune", action="store_true", help="remove blocks for orgs that no longer exist")
    s.add_argument("--quiet", action="store_true", help="don't show the diff")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("login", help="run aws sso login only where needed")
    s.add_argument("orgs", nargs="*")
    s.add_argument("--device", action="store_true", help="force the device code flow")
    s.add_argument("--force", action="store_true", help="log in even if a session exists")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("status", help="session status per org")
    s.add_argument("orgs", nargs="*")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="catalog of profiles")
    s.add_argument("--json", action="store_true")
    s.add_argument("--org")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("envrc", help="print an .envrc for direnv")
    s.add_argument("profile")
    s.add_argument("--state", help="profile for the Terraform/Terragrunt remote state")
    s.add_argument("--state-var", default="AWS_STATE_PROFILE",
                   help="variable that holds the state profile (default: AWS_STATE_PROFILE)")
    s.set_defaults(func=cmd_envrc)

    s = sub.add_parser("init", help="wizard: asks for your organizations and creates orgs.toml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("discover", help="list an org's actual accounts and roles")
    s.add_argument("org")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("setup", help="all-in-one: PATH, wizard if no config, profiles and AI agents")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-agents", action="store_true", help="don't install instructions/hooks for agents")
    s.add_argument("--agent", dest="agents", action="append", help="a specific agent (repeatable); default: detected ones")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run", help="check/renew the session before leaving an agent to work")
    s.add_argument("--org", action="append", help="org to check (repeatable); default: all")
    s.add_argument("--max", default="4h", help="job duration: 90m, 3h, 1h30m (default: 4h)")
    s.add_argument("--login", action="store_true", help="renew the session first (full window from now)")
    s.add_argument("--yes", action="store_true", help="start even if the remaining time can't be estimated")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run (optional)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("agent", help="instructions and hooks for AI agents (claude, codex, kiro, copilot…)")
    s.add_argument("action", choices=["list", "install", "uninstall", "rules"])
    s.add_argument("names", nargs="*", help="agents; default: detected ones")
    s.add_argument("--all", action="store_true", help="all supported agents")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-hooks", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_agent)

    from rolehop import manage, upgrade
    manage.register(sub)
    upgrade.register(sub)

    s = sub.add_parser("hook")
    s.add_argument("kind", choices=["post-tool", "cwd"])
    s.add_argument("--format", default="claude", choices=["claude", "codex", "copilot", "gemini", "qwen", "text"])
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("_mark")
    s.add_argument("org")
    s.add_argument("state", choices=["blocked", "clear"])
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("_which-org")
    s.add_argument("profile")
    s.set_defaults(func=cmd_which_org)

    args = ap.parse_args(argv)
    if args.cmd not in ("hook", "_mark", "_which-org", "uninstall", "upgrade") and not getattr(args, "dry_run", False):
        try:
            for msg in migrate_legacy():
                print(msg, file=sys.stderr)
        except OSError as exc:
            print(f"! Could not migrate the {LEGACY_NAME} configuration: {exc}", file=sys.stderr)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
