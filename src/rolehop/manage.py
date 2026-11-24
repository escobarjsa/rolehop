"""Maintenance commands: `rolehop refresh` (reload permissions from the access portal), `remove` and `uninstall`.

Every command that modifies files validates the result before writing anything, makes a backup and supports --dry-run.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import hashlib
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from rolehop import awsconfig, cli, config, errors, paths, portal, session, ui
from rolehop.i18n import _


def _edit_note() -> None:
    """Shown before asking or writing, so the user can still say no."""
    ui.hint(_("orgs.toml is rewritten in full: hand-written comments are not kept (a backup is saved)."))


# --------------------------------------------------------------------------- helpers
def _confirm(question: str, assume_yes: bool, command: list[str]) -> bool:
    """Ask before a destructive change. Without a terminal, print the same command with --yes to copy."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        ui.echo("✗ " + _("Confirmation required, but there is no terminal to ask in."), err=True)
        ui.fix(_("run: {cmd}", cmd=shlex.join([*command, "--yes"])), indent=2, err=True)
        return False
    return input(f"{question} {_('[y/N]')} ").strip().lower().startswith(("s", "y"))


def _validate(orgs: list[dict]) -> None:
    """Validate a set of orgs with the same rules as `sync`, without touching orgs.toml."""
    text = config.toml_dump(orgs)
    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".orgs-", suffix=".toml", dir=paths.CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        config.load_orgs(Path(tmp))
    finally:
        os.unlink(tmp)


def _precheck_sync(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Compute the ~/.aws/config that `sync` would produce, before writing anything, and check that it is valid."""
    current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
    conflicts = awsconfig.plan_sync(orgs, current, prune_only=set(prune_only or []))[2]  # ConfigError if invalid
    problems = [f"[{org}] {c}" for org, items in conflicts.items() for c in items]
    if problems:
        raise errors.ConfigError(_("Sections in {path} outside the managed blocks conflict with new profiles: {items}. "
                                   "Rename or delete them and try again.", path=ui.short(paths.AWS_CONFIG),
                                   items=", ".join(problems)))


def _save(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Validate and write orgs.toml only if `sync` can apply it afterwards (never leaves the two files out of sync)."""
    _validate(orgs)
    _precheck_sync(orgs, prune_only)
    config.write_orgs(orgs)


def _sync(prune_only: list[str] | None = None) -> int:
    return cli.cmd_sync(argparse.Namespace(dry_run=False, prune=False, quiet=True, prune_only=prune_only or []))


def _token_cache(session: str) -> Path:
    return paths.SSO_CACHE / (hashlib.sha1(session.encode(), usedforsecurity=False).hexdigest() + ".json")


def _forget_org_local(name: str, dry_run: bool) -> list[str]:
    """Delete the rolehop state and the cached SSO token of an org (signs it out on this machine)."""
    msgs = []
    state, token = session.state_file(name), _token_cache(name)
    if state.exists():
        if not dry_run:
            state.unlink()
        msgs.append("- " + _("local state of {org}: {path}", org=name, path=ui.short(state)) if dry_run
                    else "✓ " + _("Deleted the local state of {org}: {path}", org=name, path=ui.short(state)))
    if token.exists():
        if not dry_run:
            token.unlink()
        msgs.append("- " + _("cached SSO token of {org}: {path}", org=name, path=ui.short(token)) if dry_run
                    else "✓ " + _("Deleted the cached SSO token of {org}: {path}", org=name, path=ui.short(token)))
    return msgs


def _drop_probe_if_missing(org: dict, changes: list[str]) -> None:
    pp = org.get("probe_profile")
    if pp and pp not in {r["profile"] for r in config.catalog([org])}:
        del org["probe_profile"]
        changes.append("! " + _("probe_profile {profile} no longer exists: removing it (status will use default_role)",
                                profile=pp))


def _profiles(org: dict) -> set[str]:
    return {r["profile"] for r in config.catalog([org])}


# --------------------------------------------------------------------------- refresh
def plan_refresh(org: dict, inventory: list[dict], prune: bool) -> tuple[dict, list[str], set[str]]:
    """Compare an org with the live inventory from the access portal. Return (updated org, changes, removed profiles).

    Writes nothing. Raise ConfigError if the result would rename existing profiles, if --prune would leave the org
    empty, or if the portal returned no accounts with --prune.
    """
    name = org["name"]
    if prune and not inventory:
        raise errors.ConfigError(_("[{org}] the access portal returned no accounts: not applying --prune "
                                   "(temporary loss of access?). Without --prune, accounts are only added.", org=name))
    before = _profiles(org)
    org = copy.deepcopy(org)
    roles_map = org.setdefault("roles", {})
    # Roles written with the exact permission set name (no alias) are kept that way,
    # because creating an alias for them would change their profile names.
    direct = {r for a in org.get("accounts", []) for r in a["roles"] if r not in roles_map and r not in roles_map.values()}

    def ref(ps: str) -> str:
        return ps if ps in direct else config.alias_for(ps, roles_map)

    live_by_id = {str(a["id"]): a for a in inventory}
    protected = {ar["source"] for ar in org.get("assume_roles", [])}  # "<account>-<role>" used by assume_roles
    changes: list[str] = []
    removed: set[str] = set()
    kept: list[dict] = []
    for acc in org.get("accounts", []):
        pairs = config.role_pairs(org, acc)
        live = live_by_id.get(str(acc["id"]))
        if live is None:
            locked = [(r, k) for r, (k, _ps) in zip(acc["roles"], pairs, strict=True) if f"{acc['name']}-{k}" in protected]
            if not prune:
                changes.append("! " + _("Account {account} ({id}): no longer listed in the access portal "
                                        "(use --prune to remove it)", account=acc["name"], id=acc["id"]))
                kept.append(acc)
                continue
            gone = {f"{name}-{acc['name']}-{k}" for k, _ps in pairs} - {f"{name}-{acc['name']}-{k}" for _r, k in locked}
            removed |= gone
            if not locked:
                changes.append("- " + _("Account {account} ({id}): you no longer have access", account=acc["name"], id=acc["id"]))
                continue
            acc["roles"] = [r for r, _k in locked]
            keep = ", ".join(f"{acc['name']}-{k}" for _r, k in locked)
            changes.append("! " + _("Account {account} ({id}): no longer listed in the access portal; keeping only "
                                    "{profiles} (used by assume_roles)", account=acc["name"], id=acc["id"], profiles=keep))
            kept.append(acc)
            continue
        live_ps = set(live["roles"])
        roles: list[str] = []
        for role, (key, ps) in zip(acc["roles"], pairs, strict=True):
            profile = f"{name}-{acc['name']}-{key}"
            if ps in live_ps:
                roles.append(role)
            elif prune and f"{acc['name']}-{key}" not in protected:
                removed.add(profile)
                changes.append("- " + _("Role {profile} ({role}): revoked", profile=profile, role=ps))
            else:
                roles.append(role)
                if prune:
                    msg = _("Role {profile} ({role}): revoked in the access portal (used by assume_roles)",
                            profile=profile, role=ps)
                else:
                    msg = _("Role {profile} ({role}): revoked in the access portal (use --prune to remove it)",
                            profile=profile, role=ps)
                changes.append("! " + msg)
        have = {ps for _k, ps in pairs}
        for ps in live["roles"]:
            if ps not in have:
                r = ref(ps)
                roles.append(r)
                key = next(k for rr, (k, _ps) in zip(roles, config.role_pairs(org, {"roles": roles}), strict=True) if rr == r)
                changes.append("+ " + _("Role {profile} ({role})", profile=f"{name}-{acc['name']}-{key}", role=ps))
        if not roles:
            changes.append("- " + _("Account {account} ({id}): no roles left", account=acc["name"], id=acc["id"]))
            continue
        acc["roles"] = roles
        kept.append(acc)
    known = {str(a["id"]) for a in org.get("accounts", [])}
    used = {a["name"] for a in kept}
    for live in inventory:
        if str(live["id"]) in known or not live["roles"]:
            continue
        base = config.short_account_name(live["name"], name)
        acc_name, n = base, 2
        while acc_name in used:
            acc_name, n = f"{base}-{n}", n + 1
        used.add(acc_name)
        acc = {"name": acc_name, "id": str(live["id"]), "roles": [ref(ps) for ps in live["roles"]]}
        kept.append(acc)
        keys = [k for k, _ps in config.role_pairs(org, acc)]
        changes.append("+ " + _("Account {account} ({id}): {roles}", account=acc_name, id=acc["id"], roles=", ".join(keys)))
    org["accounts"] = kept
    _drop_probe_if_missing(org, changes)
    lost = before - _profiles(org) - removed
    if lost:
        raise errors.ConfigError(_("[{org}] refresh would rename existing profiles ({profiles}); not applied. "
                                   "Check [org.roles] in orgs.toml.", org=name, profiles=", ".join(sorted(lost))))
    if not org["accounts"] and not org.get("assume_roles"):
        raise errors.ConfigError(_("[{org}] --prune would leave the organization with no profiles; not applied. "
                                   "If you no longer use it, run: {cmd}", org=name, cmd=f"rolehop remove {name}"))
    return org, changes, removed


def _read_portal(org: dict) -> list[dict]:
    """Read the access-portal inventory, letting the AWS CLI renew an expired token once if needed."""
    return portal.inventory(org)


def cmd_refresh(args) -> int:
    orgs = config.load_orgs()
    targets = {o["name"] for o in config.select(orgs, args.orgs)}
    if args.login:
        rc = cli.cmd_login(argparse.Namespace(orgs=sorted(targets), force=False, device=False))
        if rc != 0:
            ui.echo("✗ " + _("Login failed: nothing was changed."), err=True)
            return 1
    updated, failed, removals, any_change = [], [], set(), False
    for org in orgs:
        if org["name"] not in targets:
            updated.append(org)
            continue
        try:
            inventory = _read_portal(org)
            new_org, changes, removed = plan_refresh(org, inventory, args.prune)
        except errors.PortalError as exc:
            reason = exc.reason if isinstance(exc, portal.LoginNeeded) else str(exc)
            ui.echo("✗ " + _("{org}: could not read the access portal ({reason}).", org=org["name"], reason=reason),
                    err=True)
            login_needed = isinstance(exc, portal.LoginNeeded)
            ui.fix(_("run: {cmd}", cmd=f"rolehop login {org['name']}" if login_needed else "rolehop doctor"),
                   indent=2, err=True)
            failed.append(org["name"])
            updated.append(org)
            continue
        except errors.ConfigError as exc:
            ui.echo(f"✗ {exc}", err=True)
            failed.append(org["name"])
            updated.append(org)
            continue
        if changes:
            ui.echo(_("{org} · accounts in the access portal: {count}", org=org["name"], count=len(inventory)))
            for c in changes:
                ui.echo(f"  {c}")
        else:
            ui.echo("✓ " + _("{org}: no changes · accounts in the access portal: {count}",
                             org=org["name"], count=len(inventory)))
        any_change = any_change or new_org != org
        removals |= removed
        updated.append(new_org)
    rc_failed = 1 if failed else 0
    command = ["rolehop", "refresh", *args.orgs, *(["--prune"] if args.prune else [])]
    if args.dry_run:
        if any_change:
            ui.dry_run_footer(shlex.join(command))
        return rc_failed
    if any_change:
        _edit_note()
        if removals and not _confirm(_("Profiles to remove: {count}. Continue?", count=len(removals)), args.yes, command):
            return 1
        try:
            _save(updated)
        except errors.ConfigError as exc:
            ui.echo("✗ " + _("Nothing was written: {error}", error=exc), err=True)
            return 2
    # Always keep ~/.aws/config in sync with orgs.toml, even when nothing changed this time.
    return _sync() or rc_failed


# --------------------------------------------------------------------------- remove
def _resolve(orgs: list[dict], target: str) -> tuple[str, dict, Any]:
    """Return ('org'|'profile'|'assume'|'account', org, item). Raise if the name does not exist or is ambiguous."""
    matches: list[tuple[str, dict, Any]] = []
    for org in orgs:
        if target == org["name"]:
            matches.append(("org", org, None))
        for acc in org["accounts"]:
            if target == f"{org['name']}-{acc['name']}":
                matches.append(("account", org, acc))
            for role, (key, _ps) in zip(acc["roles"], config.role_pairs(org, acc), strict=True):
                if target == f"{org['name']}-{acc['name']}-{key}":
                    matches.append(("profile", org, (acc, role, key)))
        for ar in org["assume_roles"]:
            if target == f"{org['name']}-{ar['name']}":
                matches.append(("assume", org, ar))
    if not matches:
        raise errors.ConfigError(_("'{target}' not found among your organizations, accounts or profiles. "
                                   "See `rolehop list`.", target=target))
    if len(matches) > 1:
        labels = {"org": _("organization"), "account": _("account"), "profile": _("profile"), "assume": _("chained role")}
        found = ", ".join(_("{kind} in {org}", kind=labels[k], org=o["name"]) for k, o, _item in matches)
        raise errors.ConfigError(_("'{target}' is ambiguous ({found}). Edit orgs.toml by hand to remove the one you want.",
                                   target=target, found=found))
    return matches[0]


def cmd_remove(args) -> int:
    orgs = config.load_orgs()
    plan: list[str] = []
    removed_orgs: list[str] = []
    for target in args.targets:
        kind, org, item = _resolve(orgs, target)
        if kind == "org":
            orgs = [o for o in orgs if o["name"] != org["name"]]
            removed_orgs.append(org["name"])
            plan.append("- " + _("organization {org}, with its sso-session and local session · profiles: {count}",
                                 org=org["name"], count=len(config.catalog([org]))))
            continue
        sources = {ar["source"] for ar in org["assume_roles"]}
        if kind == "assume":
            org["assume_roles"] = [a for a in org["assume_roles"] if a is not item]
            plan.append("- " + _("profile {profile} (chained role)", profile=target))
        elif kind == "account":
            own = {f"{item['name']}-{k}" for k, _ps in config.role_pairs(org, item)}
            used = sorted(own & sources)
            if used:
                raise errors.ConfigError(_("Account {account} is the source of assume_roles ({profiles}): remove them first.",
                                           account=target, profiles=", ".join(used)))
            org["accounts"] = [a for a in org["accounts"] if a is not item]
            plan.append("- " + _("account {account} ({id}) · profiles: {count}",
                                 account=target, id=item["id"], count=len(item["roles"])))
        else:
            acc, role, key = item
            if f"{acc['name']}-{key}" in sources:
                raise errors.ConfigError(_("Profile {profile} is the source of an assume_role: remove that chained role first.",
                                           profile=target))
            acc["roles"] = [r for r in acc["roles"] if r != role]
            plan.append("- " + _("profile {profile}", profile=target))
            if not acc["roles"]:
                org["accounts"] = [a for a in org["accounts"] if a is not acc]
                plan.append("  " + _("(account {account} has no roles left: removing it)",
                                     account=f"{org['name']}-{acc['name']}"))
        if not org["accounts"] and not org["assume_roles"]:
            raise errors.ConfigError(_("Organization {org} would be left empty: use `rolehop remove {org}`.", org=org["name"]))
        notes: list[str] = []
        _drop_probe_if_missing(org, notes)
        plan += [f"  {n}" for n in notes]
    if not orgs:
        raise errors.ConfigError(_("Cannot remove every organization: use `rolehop uninstall` instead."))
    ui.echo(_("The following will be removed:"))
    for line in plan:
        ui.echo(f"  {line}")
    _validate(orgs)
    _precheck_sync(orgs, removed_orgs)
    command = ["rolehop", "remove", *args.targets]
    if args.dry_run:
        for name in removed_orgs:
            for m in _forget_org_local(name, True):
                ui.echo(f"  {m}")
        ui.dry_run_footer(shlex.join(command))
        return 0
    _edit_note()
    if not _confirm(_("Continue?"), args.yes, command):
        return 1
    _save(orgs, removed_orgs)
    rc = _sync(prune_only=removed_orgs)
    if rc == 0:
        for name in removed_orgs:
            for m in _forget_org_local(name, False):
                ui.echo(m)
    return rc


# --------------------------------------------------------------------------- uninstall
def _strip_managed_blocks(current: str) -> tuple[str, list[str]]:
    """Return ~/.aws/config without the rolehop (and awsorg) blocks. Only touches the gaps the blocks leave behind."""
    free, blocks = awsconfig.split_blocks(current)
    new = free
    for token in re.findall(r"\0ROLEHOP:[^\0]+\0\n", free):
        new = awsconfig.drop_placeholder(new, token)
    configparser.RawConfigParser(strict=True, interpolation=None).read_string(new)
    return new, sorted(blocks)


def _purge_targets() -> list[Path]:
    """Files and folders `uninstall --purge` deletes, among those that exist."""
    files = [paths.CONFIG_PATH, paths.SETTINGS_PATH, paths.SETTINGS_PATH.with_name(paths.SETTINGS_PATH.name + ".tmp"),
             *sorted(paths.CONFIG_PATH.parent.glob(f"{paths.CONFIG_PATH.name}.bak-*"))]
    legacy = [paths.LEGACY_CONFIG_PATH.parent, paths.LEGACY_STATE_DIR]
    return [f for f in files if f.exists()] + [d for d in legacy if d.is_dir()]


def cmd_uninstall(args) -> int:
    from rolehop import agents, shellrc

    try:
        org_names = [o["name"] for o in config.load_orgs()]
    except errors.ConfigError:
        org_names = []
    current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
    aws_error = None
    try:
        new_aws, blocks = _strip_managed_blocks(current)
    except (errors.ConfigError, configparser.Error) as exc:
        new_aws, blocks, aws_error = current, [], str(exc)
    sessions = sorted(set(org_names) | set(blocks))
    tokens = [] if aws_error else [name for name in sessions if _token_cache(name).exists()]
    home = Path.home()
    installed = [a.name for a in agents.AGENTS.values() if agents.has_leftovers(a, home)]
    rc_files = shellrc.installed_rc_files(home)
    purge = _purge_targets() if args.purge else []
    aws_dir = paths.AWS_CONFIG.parent
    aws_backups = sorted(aws_dir.glob(f"{paths.AWS_CONFIG.name}.bak-*")) if aws_dir.exists() else []
    tool = "pipx uninstall rolehop" if "pipx" in sys.prefix else "uv tool uninstall rolehop"
    aws_path = ui.short(paths.AWS_CONFIG)

    # The plan lists only what is actually on this machine.
    plan: list[str] = []
    if blocks:
        plan.append(_("profile blocks in {path}: {names}", path=aws_path, names=", ".join(blocks)))
    if installed:
        plan.append(_("rules and hooks of these AI agents: {names}", names=", ".join(installed)))
    if rc_files:
        plan.append(_("the shell integration block (PATH and direnv) in: {paths}",
                      paths=", ".join(ui.short(f) for f in rc_files)))
    if paths.STATE_DIR.is_dir():
        plan.append(_("the local state ({path})", path=ui.short(paths.STATE_DIR)))
    if tokens:
        plan.append(_("cached SSO tokens for: {names}", names=", ".join(tokens)))
    if purge:
        plan.append(_("ALSO your configuration: {paths}", paths=", ".join(ui.short(f) for f in purge)))
    if not plan and not aws_error:
        ui.echo("✓ " + _("Nothing to uninstall: this machine has nothing set up by rolehop."))
        if paths.CONFIG_PATH.exists() and not args.purge:
            ui.hint("  " + _("Keeping {path}; add --purge to delete it too.", path=ui.short(paths.CONFIG_PATH)))
        ui.fix(_("to also remove the command itself, run: {cmd}", cmd=tool), indent=2)
        return 0

    ui.echo(_("rolehop uninstall will remove:"))
    if aws_error:
        ui.echo("  ! " + _("{path}: cannot clean it up ({error}). Skipping it, along with your SSO sessions.",
                           path=aws_path, error=aws_error))
    for line in plan:
        ui.echo("  - " + line)
    if paths.CONFIG_PATH.exists() and not args.purge:
        ui.hint("  " + _("Keeping {path}; add --purge to delete it too.", path=ui.short(paths.CONFIG_PATH)))
    if aws_backups:
        ui.hint("  " + _("Backups left in place ({name}.bak-*): {count}. They contain your profiles; delete them if "
                         "you no longer need them.", count=len(aws_backups), name=aws_path))
    command = ["rolehop", "uninstall", *(["--purge"] if args.purge else [])]
    if args.dry_run:
        ui.dry_run_footer(shlex.join(command))
        return 0
    if not _confirm(_("Uninstall?"), args.yes, command):
        return 1

    if not aws_error and blocks:
        awsconfig.write_aws_config(new_aws)
        ui.echo("✓ " + _("{path}: profiles removed", path=aws_path))
    ch = agents.Changes(False)
    for a in agents.AGENTS.values():
        agents.uninstall(a, home, ch)
    for line in ch.lines:
        ui.echo(line)
    for m in shellrc.remove_shell_integration():
        ui.echo(m)
    if not aws_error:
        for name in sessions:
            for m in _forget_org_local(name, False):
                ui.echo(m)
    if paths.STATE_DIR.is_dir():
        shutil.rmtree(paths.STATE_DIR)
        ui.echo("✓ " + _("Deleted {path}", path=ui.short(paths.STATE_DIR)))
    for path in purge:
        if path.is_dir():
            shutil.rmtree(path)
            ui.echo("✓ " + _("Deleted {path} (awsorg)", path=ui.short(path)))
        elif path.exists():
            path.unlink()
            ui.echo("✓ " + _("Deleted {path}", path=ui.short(path)))
    if args.purge:
        try:
            paths.CONFIG_PATH.parent.rmdir()  # only if it is now empty
        except OSError:
            pass
    ui.echo()
    if aws_error:
        ui.echo("✗ " + _("Uninstall incomplete: {path} could not be cleaned up.", path=aws_path), err=True)
        ui.fix(_("remove the managed blocks from {path} by hand, then run: {cmd}", path=aws_path,
                 cmd="rolehop uninstall"), indent=2, err=True)
        return 1
    ui.echo("✓ " + _("Uninstalled. To also remove the command itself:"))
    ui.fix(_("run: {cmd}", cmd=tool), indent=2)
    return 0


def register(sub) -> None:
    s = sub.add_parser("refresh", help=_("reload accounts and roles from the access portal (after your permissions change)"))
    s.add_argument("orgs", nargs="*", help=_("organizations (default: all)"))
    s.add_argument("--prune", action="store_true", help=_("also remove revoked accounts and roles (asks for confirmation)"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--login", action="store_true", help=_("log in first if needed"))
    s.add_argument("--yes", action="store_true", help=_("do not ask for confirmation when removing profiles"))
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("remove", help=_("remove an organization, an account (<org>-<account>) or a profile from this machine"))
    s.add_argument("targets", nargs="+", metavar="org|account|profile",
                   help=_("what to remove: an organization, an account (<org>-<account>) or a profile"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--yes", action="store_true", help=_("do not ask for confirmation"))
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("uninstall", help=_("remove everything set up on this machine (profiles, agents, shell, state)"))
    s.add_argument("--purge", action="store_true", help=_("also delete orgs.toml and the legacy awsorg configuration"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--yes", action="store_true", help=_("do not ask for confirmation"))
    s.set_defaults(func=cmd_uninstall)
