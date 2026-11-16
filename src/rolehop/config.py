"""orgs.toml: loading, validation, the profile catalog and serialization."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from rolehop import errors, paths

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Values written to ~/.aws/config: no whitespace, newlines, brackets or comment characters.
VAL_RE = re.compile(r"^[^\s\[\]#;]+$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s\[\]#;]*)?$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ARN_RE = re.compile(r"^arn:aws[\w-]*:iam::\d{12}:role/[\w+=,.@/-]+$")


def load_orgs(path: Path | None = None) -> list[dict]:
    """Load and validate orgs.toml (default: the configured path), filling in defaults. Raise ConfigError on any problem."""
    path = path or paths.CONFIG_PATH
    if not path.exists():
        raise errors.ConfigError(f"{path} not found. Copy orgs.example.toml there and edit it.")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise errors.ConfigError(f"{path} is not valid TOML: {exc}") from None
    orgs = data.get("org", [])
    if not orgs:
        raise errors.ConfigError(f"{path} does not define any [[org]].")
    seen = set()
    for org in orgs:
        if not isinstance(org, dict):
            raise errors.ConfigError("Each [[org]] must be a table.")
        for key in ("name", "start_url", "sso_region"):
            if not isinstance(org.get(key), str) or not org.get(key):
                raise errors.ConfigError(f"An org is missing '{key}' (a quoted string).")
        name = org["name"]
        if not NAME_RE.match(name):
            raise errors.ConfigError(f"Invalid org name: {name!r} (use lowercase letters, digits, - or _).")
        if name in seen:
            raise errors.ConfigError(f"Duplicate org: {name}")
        seen.add(name)
        org.setdefault("region", org["sso_region"])
        org.setdefault("login_flow", "device")
        org.setdefault("portal_session_hours", 8)
        org.setdefault("roles", {})
        org.setdefault("accounts", [])
        org.setdefault("assume_roles", [])
        if not URL_RE.match(org["start_url"]):
            raise errors.ConfigError(f"[{name}] invalid start_url: {org['start_url']!r} (expected https://<name>.awsapps.com/start)")
        for key in ("sso_region", "region"):
            if not isinstance(org[key], str) or not REGION_RE.match(org[key]):
                raise errors.ConfigError(f"[{name}] invalid {key}: {org[key]!r} (e.g. us-east-1)")
        if org["login_flow"] not in ("device", "pkce"):
            raise errors.ConfigError(f"[{name}] login_flow must be \"device\" or \"pkce\".")
        if not isinstance(org["portal_session_hours"], (int, float)) or org["portal_session_hours"] <= 0:
            raise errors.ConfigError(f"[{name}] portal_session_hours must be a number of hours.")
        if not isinstance(org["roles"], dict):
            raise errors.ConfigError(f"[{name}] [org.roles] must be a table (alias = \"PermissionSet\").")
        for alias, ps in org["roles"].items():
            if not NAME_RE.match(alias) or not isinstance(ps, str) or not VAL_RE.match(ps):
                raise errors.ConfigError(f"[{name}] invalid role in [org.roles]: {alias} = {ps!r}")
        if not isinstance(org["accounts"], list) or not isinstance(org["assume_roles"], list):
            raise errors.ConfigError(f"[{name}] accounts and assume_roles must be arrays of tables.")
        for acc in org["accounts"]:
            if not isinstance(acc, dict) or not acc.get("name") or not acc.get("id"):
                raise errors.ConfigError(f"[{name}] each account needs 'name' and 'id'.")
            if not isinstance(acc["name"], str) or not NAME_RE.match(acc["name"]):
                raise errors.ConfigError(f"[{name}] invalid account name: {acc['name']!r} "
                                  "(a string of lowercase letters, digits, - or _)")
            if not re.fullmatch(r"\d{12}", str(acc["id"])):
                raise errors.ConfigError(f"[{name}] invalid account id for {acc['name']}: {acc['id']!r} (must be 12 digits)")
            acc["id"] = str(acc["id"])
            if not isinstance(acc.get("roles"), list) or not acc["roles"]:
                raise errors.ConfigError(f"[{name}] roles for account {acc['name']} must be a list, e.g. [\"admin\"].")
            for role in acc["roles"]:
                if not isinstance(role, str) or not VAL_RE.match(role):
                    raise errors.ConfigError(f"[{name}] invalid role in account {acc['name']}: {role!r}")
            if "region" in acc and (not isinstance(acc["region"], str) or not REGION_RE.match(acc["region"])):
                raise errors.ConfigError(f"[{name}] invalid region in account {acc['name']}: {acc['region']!r}")
        sources = {f"{a['name']}-{k}" for a in org["accounts"] for k, _ in role_pairs(org, a)}
        for ar in org["assume_roles"]:
            for k in ("name", "role_arn", "source"):
                if not isinstance(ar, dict) or not isinstance(ar.get(k), str) or not ar.get(k):
                    raise errors.ConfigError(f"[{name}] each [[org.assume_roles]] needs '{k}'.")
            if not NAME_RE.match(ar["name"]):
                raise errors.ConfigError(f"[{name}] invalid name in assume_roles: {ar['name']!r}")
            if not ARN_RE.match(ar["role_arn"]):
                raise errors.ConfigError(f"[{name}] invalid role_arn: {ar['role_arn']!r}")
            if ar["source"] not in sources:
                raise errors.ConfigError(f"[{name}] unknown source in assume_roles: {ar['source']!r} "
                                  f"(valid: {', '.join(sorted(sources))})")
            if "external_id" in ar and (not isinstance(ar["external_id"], str) or not VAL_RE.match(ar["external_id"])):
                raise errors.ConfigError(f"[{name}] invalid external_id in assume_roles {ar['name']}")
            if "duration_seconds" in ar and not isinstance(ar["duration_seconds"], int):
                raise errors.ConfigError(f"[{name}] duration_seconds must be an integer")
    for org in orgs:
        pp = org.get("probe_profile")
        if pp and pp not in {r["profile"] for r in catalog([org])}:
            raise errors.ConfigError(f"[{org['name']}] probe_profile {pp!r} is not one of this org's profiles.")
    from collections import Counter
    dups = sorted(p for p, c in Counter(r["profile"] for r in catalog(orgs)).items() if c > 1)
    if dups:
        raise errors.ConfigError(f"Duplicate profiles: {', '.join(dups)}. Check orgs.toml for repeated accounts or roles.")
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


def select(orgs: list[dict], names: list[str]) -> list[dict]:
    """Return the named orgs (all of them if names is empty). Raise ConfigError on unknown names."""
    if not names:
        return orgs
    by = {o["name"]: o for o in orgs}
    missing = [n for n in names if n not in by]
    if missing:
        raise errors.ConfigError(f"Unknown org: {', '.join(missing)}. Available: {', '.join(by)}")
    return [by[n] for n in names]


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


def write_orgs(orgs: list[dict]) -> None:
    text = toml_dump(orgs)
    tomllib.loads(text)  # sanity check: never write broken TOML
    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if paths.CONFIG_PATH.exists():
        backup = paths.CONFIG_PATH.with_name(f"{paths.CONFIG_PATH.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(paths.CONFIG_PATH, backup)
        print(f"  ✓ Backup: {backup}")
    tmp = paths.CONFIG_PATH.with_name(paths.CONFIG_PATH.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(paths.CONFIG_PATH)
    print(f"  ✓ Saved {paths.CONFIG_PATH}")


def alias_for(ps: str, roles_map: dict) -> str:
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
