"""Managed blocks in ~/.aws/config: rendering, parsing, conflict detection and atomic writes."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil

from rolehop import config, errors, paths, ui
from rolehop.i18n import _

MARK_NAMES = f"(?:rolehop|{paths.LEGACY_NAME})"  # block marker names accepted when reading ~/.aws/config


def begin_marker(org: str) -> str:
    # split_blocks accepts any text between "rolehop:<org> " and ">>>", so older (Spanish) markers still match.
    return f"# >>> rolehop:{org} (managed by rolehop, do not edit) >>>"


def end_marker(org: str) -> str:
    return f"# <<< rolehop:{org} <<<"


def render_block(org: dict) -> str:
    """Render an org's managed block (sso-session plus profiles) for ~/.aws/config."""
    n = org["name"]
    out = [begin_marker(n), f"[sso-session {n}]",
           f"sso_start_url = {org['start_url']}",
           f"sso_region = {org['sso_region']}",
           "sso_registration_scopes = sso:account:access", ""]
    for acc in org["accounts"]:
        for key, ps in config.role_pairs(org, acc):
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
            raise errors.ConfigError(_("Block rolehop:{org} appears twice in {path}. "
                                       "Keep only one (back up the file first) and try again.",
                                       org=m["org"], path=paths.AWS_CONFIG))
        blocks[m["org"]] = m.group(0).rstrip("\n")
        return f"\0ROLEHOP:{m['org']}\0\n"

    if "\0" in text:
        raise errors.ConfigError(_("{path} contains null bytes: check it or restore a .bak backup.",
                                   path=paths.AWS_CONFIG))
    free = pattern.sub(repl, text)
    leftover = re.findall(rf"^# (?:>>>|<<<) {MARK_NAMES}:([a-z0-9_-]+)", free, re.M)
    if leftover:
        raise errors.ConfigError(_("Incomplete or tampered block markers for {orgs} in {path}. "
                                   "Fix or remove that block by hand (back up the file first).",
                                   orgs=", ".join(sorted(set(leftover))), path=paths.AWS_CONFIG))
    return free, blocks


SECTION_RE = re.compile(r"^\[(?:profile\s+)?(?P<name>[^\]]+?)\]\s*$", re.M)


SSO_SESSION_RE = re.compile(r"^\[sso-session\s+(?P<name>[^\]]+?)\]\s*$", re.M)


def profile_exists(name: str) -> bool:
    """True if ~/.aws/config defines ``[profile <name>]`` (or ``[default]`` for "default")."""
    try:
        text = paths.AWS_CONFIG.read_text()
    except OSError:
        return False
    return any(m["name"].strip() == name for m in SECTION_RE.finditer(text) if not m.group(0).startswith("[sso-session"))


def conflicts(free_text: str, org: dict) -> list[str]:
    """Return sections outside managed blocks that would clash with the generated ones."""
    clean = re.sub(r"\0ROLEHOP:[^\0]+\0", "", free_text)
    existing_profiles = {m["name"].strip() for m in SECTION_RE.finditer(clean)
                         if not m.group(0).startswith("[sso-session")}
    existing_sessions = {m["name"].strip() for m in SSO_SESSION_RE.finditer(clean)}
    wanted = {r["profile"] for r in config.catalog([org])}
    found = sorted(wanted & existing_profiles)
    if org["name"] in existing_sessions:
        found.insert(0, f"sso-session {org['name']}")
    return found


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
        raise errors.ConfigError(_("internal error: the result contains unresolved placeholders. Nothing was written."))
    import configparser
    try:
        configparser.RawConfigParser(strict=True, interpolation=None).read_string(new_free)
    except configparser.Error as exc:
        raise errors.ConfigError(_("the result would not be a valid {path}; nothing was written: {error}",
                                   path=paths.AWS_CONFIG, error=exc)) from None
    return new_free, kept, {}


def write_aws_config(text: str) -> None:
    """Write ~/.aws/config atomically (0600, fsync), with a backup, following symlinks."""
    target = paths.AWS_CONFIG.resolve() if paths.AWS_CONFIG.is_symlink() else paths.AWS_CONFIG
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
        ui.echo()
        ui.echo("✓ " + _("Backup: {path}", path=backup))
    ui.debug(f"writing {target} atomically (mode 0600)")
    tmp = target.with_name(target.name + ".rolehop-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
