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
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from rolehop import cli

EDIT_NOTE = "  (orgs.toml is rewritten in full: hand-written comments are not preserved; a backup is kept.)"


# --------------------------------------------------------------------------- helpers
def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("✗ Confirmation required: rerun with --yes (or preview with --dry-run).", file=sys.stderr)
        return False
    return input(f"{question} [y/N]: ").strip().lower().startswith(("s", "y"))


def _validate(orgs: list[dict]) -> None:
    """Validate a set of orgs with the same rules as `sync`, without touching orgs.toml."""
    text = cli.toml_dump(orgs)
    cli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".orgs-", suffix=".toml", dir=cli.CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        cli.load_orgs(Path(tmp))
    finally:
        os.unlink(tmp)


def _precheck_sync(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Compute the ~/.aws/config that `sync` would produce, before writing anything, and check that it is valid."""
    current = cli.AWS_CONFIG.read_text() if cli.AWS_CONFIG.exists() else ""
    _, _, conflicts = cli.plan_sync(orgs, current, prune_only=set(prune_only or []))  # ConfigError if invalid
    problems = [f"[{org}] {c}" for org, items in conflicts.items() for c in items]
    if problems:
        raise cli.ConfigError(f"Sections in {cli.AWS_CONFIG} outside the rolehop blocks conflict with "
                              f"new profiles: {', '.join(problems)}. Rename or delete them and try again.")


def _save(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Validate and write orgs.toml only if `sync` can apply it afterwards (never leaves the two files out of sync)."""
    _validate(orgs)
    _precheck_sync(orgs, prune_only)
    cli._write_orgs(orgs)


def _sync(prune_only: list[str] | None = None) -> int:
    return cli.cmd_sync(argparse.Namespace(dry_run=False, prune=False, quiet=True, prune_only=prune_only or []))


def _token_cache(session: str) -> Path:
    return cli.SSO_CACHE / (hashlib.sha1(session.encode(), usedforsecurity=False).hexdigest() + ".json")


def _forget_org_local(name: str, dry_run: bool) -> list[str]:
    """Delete the rolehop state and the cached SSO token of an org (signs it out on this machine)."""
    msgs = []
    for path, label in ((cli.state_file(name), "state"), (_token_cache(name), "cached SSO token")):
        if path.exists():
            if not dry_run:
                path.unlink()
            msgs.append(f"{'[dry-run] would delete' if dry_run else '✓ deleted'} {label} for {name}: {path}")
    return msgs


def _drop_probe_if_missing(org: dict, changes: list[str]) -> None:
    pp = org.get("probe_profile")
    if pp and pp not in {r["profile"] for r in cli.catalog([org])}:
        del org["probe_profile"]
        changes.append(f"! probe_profile {pp} no longer exists: removing it (status will use default_role)")


def _profiles(org: dict) -> set[str]:
    return {r["profile"] for r in cli.catalog([org])}


# --------------------------------------------------------------------------- refresh
def plan_refresh(org: dict, inventory: list[dict], prune: bool) -> tuple[dict, list[str], set[str]]:
    """Compare an org with the live inventory from the access portal. Return (updated org, changes, removed profiles).

    Writes nothing. Raise ConfigError if the result would rename existing profiles, if --prune would leave the org
    empty, or if the portal returned no accounts with --prune.
    """
    name = org["name"]
    if prune and not inventory:
        raise cli.ConfigError(f"[{name}] the access portal returned no accounts: not applying --prune "
                              f"(temporary loss of access?). Without --prune, accounts are only added.")
    before = _profiles(org)
    org = copy.deepcopy(org)
    roles_map = org.setdefault("roles", {})
    # Roles written with the exact permission set name (no alias) are kept that way,
    # because creating an alias for them would change their profile names.
    direct = {r for a in org.get("accounts", []) for r in a["roles"] if r not in roles_map and r not in roles_map.values()}

    def ref(ps: str) -> str:
        return ps if ps in direct else cli._alias_for(ps, roles_map)

    live_by_id = {str(a["id"]): a for a in inventory}
    protected = {ar["source"] for ar in org.get("assume_roles", [])}  # "<account>-<role>" used by assume_roles
    hint = "" if prune else " (use --prune to remove it)"
    changes: list[str] = []
    removed: set[str] = set()
    kept: list[dict] = []
    for acc in org.get("accounts", []):
        pairs = cli.role_pairs(org, acc)
        live = live_by_id.get(str(acc["id"]))
        if live is None:
            locked = [(r, k) for r, (k, _) in zip(acc["roles"], pairs, strict=True) if f"{acc['name']}-{k}" in protected]
            if not prune:
                changes.append(f"! account {acc['name']} ({acc['id']}): no longer listed in the portal{hint}")
                kept.append(acc)
                continue
            gone = {f"{name}-{acc['name']}-{k}" for k, _ in pairs} - {f"{name}-{acc['name']}-{k}" for _, k in locked}
            removed |= gone
            if not locked:
                changes.append(f"- account {acc['name']} ({acc['id']}): you no longer have access")
                continue
            acc["roles"] = [r for r, _ in locked]
            keep = ", ".join(f"{acc['name']}-{k}" for _, k in locked)
            changes.append(f"! account {acc['name']} ({acc['id']}): no longer listed in the portal; "
                           f"keeping only {keep} (used by assume_roles)")
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
                changes.append(f"- role {profile} ({ps}): revoked")
            else:
                roles.append(role)
                why = " (used by assume_roles)" if prune else hint
                changes.append(f"! role {profile} ({ps}): revoked in the portal{why}")
        have = {ps for _, ps in pairs}
        for ps in live["roles"]:
            if ps not in have:
                r = ref(ps)
                roles.append(r)
                key = next(k for rr, (k, _) in zip(roles, cli.role_pairs(org, {"roles": roles}), strict=True) if rr == r)
                changes.append(f"+ role {name}-{acc['name']}-{key} ({ps})")
        if not roles:
            changes.append(f"- account {acc['name']} ({acc['id']}): no roles left")
            continue
        acc["roles"] = roles
        kept.append(acc)
    known = {str(a["id"]) for a in org.get("accounts", [])}
    used = {a["name"] for a in kept}
    for live in inventory:
        if str(live["id"]) in known or not live["roles"]:
            continue
        base = cli.short_account_name(live["name"], name)
        acc_name, n = base, 2
        while acc_name in used:
            acc_name, n = f"{base}-{n}", n + 1
        used.add(acc_name)
        acc = {"name": acc_name, "id": str(live["id"]), "roles": [ref(ps) for ps in live["roles"]]}
        kept.append(acc)
        keys = [k for k, _ in cli.role_pairs(org, acc)]
        changes.append(f"+ account {acc_name} ({acc['id']}): {', '.join(keys)}")
    org["accounts"] = kept
    _drop_probe_if_missing(org, changes)
    lost = before - _profiles(org) - removed
    if lost:
        raise cli.ConfigError(f"[{name}] refresh would rename existing profiles ({', '.join(sorted(lost))}); "
                              f"not applied. Check [org.roles] in orgs.toml.")
    if not org["accounts"] and not org.get("assume_roles"):
        raise cli.ConfigError(f"[{name}] --prune would leave the organization with no profiles; not applied. "
                              f"If you no longer use it: rolehop remove {name}")
    return org, changes, removed


def _read_portal(org: dict) -> list[dict]:
    """Read the portal inventory with the cached token; on failure, refresh the token with an AWS command and retry."""
    try:
        return cli.portal_inventory(org["name"], org["sso_region"])
    except cli.PortalError:
        cli.check_org(org)  # best effort: the AWS CLI refreshes the cached SSO token if the session is still alive
        return cli.portal_inventory(org["name"], org["sso_region"])


def cmd_refresh(args) -> int:
    orgs = cli.load_orgs()
    targets = {o["name"] for o in cli.select(orgs, args.orgs)}
    if args.login:
        rc = cli.cmd_login(argparse.Namespace(orgs=sorted(targets), force=False, device=False))
        if rc != 0:
            print("✗ Login failed.", file=sys.stderr)
            return 1
    updated, failed, removals, any_change = [], [], set(), False
    for org in orgs:
        if org["name"] not in targets:
            updated.append(org)
            continue
        try:
            inventory = _read_portal(org)
            new_org, changes, removed = plan_refresh(org, inventory, args.prune)
        except cli.PortalError as exc:
            print(f"✗ {org['name']}: could not read the access portal ({exc}). Run: awslogin {org['name']}  "
                  f"(or use --login)", file=sys.stderr)
            failed.append(org["name"])
            updated.append(org)
            continue
        except cli.ConfigError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            failed.append(org["name"])
            updated.append(org)
            continue
        if changes:
            print(f"{org['name']}: {len(inventory)} account(s) in the portal")
            for c in changes:
                print(f"  {c}")
        else:
            print(f"✓ {org['name']}: no changes ({len(inventory)} accounts in the portal)")
        any_change = any_change or new_org != org
        removals |= removed
        updated.append(new_org)
    rc_failed = 1 if failed else 0
    if args.dry_run:
        if any_change:
            print("\n(dry run: nothing was written. Rerun without --dry-run to apply.)")
        return rc_failed
    if any_change:
        if removals and not _confirm(f"{len(removals)} profile(s) will be removed. Continue?", args.yes):
            return 1
        try:
            _save(updated)
        except cli.ConfigError as exc:
            print(f"✗ Nothing was written: {exc}", file=sys.stderr)
            return 2
        print(EDIT_NOTE)
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
            for role, (key, _) in zip(acc["roles"], cli.role_pairs(org, acc), strict=True):
                if target == f"{org['name']}-{acc['name']}-{key}":
                    matches.append(("profile", org, (acc, role, key)))
        for ar in org["assume_roles"]:
            if target == f"{org['name']}-{ar['name']}":
                matches.append(("assume", org, ar))
    if not matches:
        raise cli.ConfigError(f"'{target}' not found among your organizations, accounts or profiles. See `rolehop list`.")
    if len(matches) > 1:
        labels = {"org": "organization", "account": "account", "profile": "profile", "assume": "chained role"}
        found = ", ".join(f"{labels[k]} in {o['name']}" for k, o, _ in matches)
        raise cli.ConfigError(f"'{target}' is ambiguous ({found}). Edit orgs.toml by hand to remove the one you want.")
    return matches[0]


def cmd_remove(args) -> int:
    orgs = cli.load_orgs()
    plan: list[str] = []
    removed_orgs: list[str] = []
    for target in args.targets:
        kind, org, item = _resolve(orgs, target)
        if kind == "org":
            orgs = [o for o in orgs if o["name"] != org["name"]]
            removed_orgs.append(org["name"])
            plan.append(f"- organization {org['name']} ({len(cli.catalog([org]))} profile(s), "
                        f"its sso-session and its local session)")
            continue
        sources = {ar["source"] for ar in org["assume_roles"]}
        if kind == "assume":
            org["assume_roles"] = [a for a in org["assume_roles"] if a is not item]
            plan.append(f"- profile {target} (chained role)")
        elif kind == "account":
            own = {f"{item['name']}-{k}" for k, _ in cli.role_pairs(org, item)}
            used = sorted(own & sources)
            if used:
                raise cli.ConfigError(f"Account {target} is the source of assume_roles ({', '.join(used)}): remove them first.")
            org["accounts"] = [a for a in org["accounts"] if a is not item]
            plan.append(f"- account {target} ({item['id']}) and its {len(item['roles'])} profile(s)")
        else:
            acc, role, key = item
            if f"{acc['name']}-{key}" in sources:
                raise cli.ConfigError(f"Profile {target} is the source of an assume_role: remove that chained role first.")
            acc["roles"] = [r for r in acc["roles"] if r != role]
            plan.append(f"- profile {target}")
            if not acc["roles"]:
                org["accounts"] = [a for a in org["accounts"] if a is not acc]
                plan.append(f"  (account {org['name']}-{acc['name']} has no roles left: removing it)")
        if not org["accounts"] and not org["assume_roles"]:
            raise cli.ConfigError(f"Organization {org['name']} would be left empty: use `rolehop remove {org['name']}`.")
        notes: list[str] = []
        _drop_probe_if_missing(org, notes)
        plan += [f"  {n}" for n in notes]
    if not orgs:
        raise cli.ConfigError("Cannot remove every organization: use `rolehop uninstall` instead.")
    print("The following will be removed:")
    for line in plan:
        print(f"  {line}")
    _validate(orgs)
    _precheck_sync(orgs, removed_orgs)
    if args.dry_run:
        for name in removed_orgs:
            for m in _forget_org_local(name, True):
                print(f"  {m}")
        print("\n(dry run: nothing was written.)")
        return 0
    print(EDIT_NOTE)
    if not _confirm("Continue?", args.yes):
        return 1
    _save(orgs, removed_orgs)
    rc = _sync(prune_only=removed_orgs)
    if rc == 0:
        for name in removed_orgs:
            for m in _forget_org_local(name, False):
                print(m)
    return rc


# --------------------------------------------------------------------------- uninstall
def _strip_managed_blocks(current: str) -> tuple[str, list[str]]:
    """Return ~/.aws/config without the rolehop (and awsorg) blocks. Only touches the gaps the blocks leave behind."""
    free, blocks = cli.split_blocks(current)
    new = free
    for token in re.findall(r"\0ROLEHOP:[^\0]+\0\n", free):
        new = cli.drop_placeholder(new, token)
    configparser.RawConfigParser(strict=True, interpolation=None).read_string(new)
    return new, sorted(blocks)


def cmd_uninstall(args) -> int:
    from rolehop import agents, shellrc

    try:
        org_names = [o["name"] for o in cli.load_orgs()]
    except cli.ConfigError:
        org_names = []
    current = cli.AWS_CONFIG.read_text() if cli.AWS_CONFIG.exists() else ""
    aws_error = None
    try:
        new_aws, blocks = _strip_managed_blocks(current)
    except (cli.ConfigError, configparser.Error) as exc:
        new_aws, blocks, aws_error = current, [], str(exc)
    sessions = sorted(set(org_names) | set(blocks))
    home = Path.home()
    aws_backups = sorted(cli.AWS_CONFIG.parent.glob(f"{cli.AWS_CONFIG.name}.bak-*")) if cli.AWS_CONFIG.parent.exists() else []

    print("rolehop uninstall will remove:")
    if aws_error:
        print(f"  ! {cli.AWS_CONFIG}: cannot clean it up ({aws_error}). Skipping it, along with your SSO sessions.")
    else:
        print(f"  - {len(blocks)} profile block(s) in {cli.AWS_CONFIG}: {', '.join(blocks) or '(none)'}")
    print("  - rules and hooks for every AI agent")
    print("  - the rolehop block in your shell (PATH and direnv)")
    print(f"  - the rolehop state ({cli.STATE_DIR})")
    if not aws_error:
        print(f"  - cached SSO tokens for: {', '.join(sessions) or '(none)'}")
    if args.purge:
        print(f"  - ALSO {cli.CONFIG_PATH}, its backups and the legacy awsorg configuration")
    else:
        print(f"  (keeping {cli.CONFIG_PATH}; use --purge to delete it)")
    if aws_backups:
        print(f"  ({len(aws_backups)} {cli.AWS_CONFIG.name}.bak-* backup(s) remain: they contain your profiles; "
              f"delete them if you like)")
    if args.dry_run:
        print("\n(dry run: nothing was changed.)")
        return 0
    if not _confirm("Uninstall?", args.yes):
        return 1

    if not aws_error and blocks:
        cli.write_aws_config(new_aws)
        print(f"✓ {cli.AWS_CONFIG}: rolehop profiles removed")
    ch = agents.Changes(False)
    for a in agents.AGENTS.values():
        agents.uninstall(a, home, ch)
    for line in ch.lines:
        print(line)
    for m in shellrc.remove_shell_integration():
        print(m)
    if not aws_error:
        for name in sessions:
            for m in _forget_org_local(name, False):
                print(m)
    if cli.STATE_DIR.is_dir():
        shutil.rmtree(cli.STATE_DIR)
        print(f"✓ deleted {cli.STATE_DIR}")
    if args.purge:
        for path in [cli.CONFIG_PATH, *sorted(cli.CONFIG_PATH.parent.glob(f"{cli.CONFIG_PATH.name}.bak-*"))]:
            if path.exists():
                path.unlink()
                print(f"✓ deleted {path}")
        for legacy in (cli.LEGACY_CONFIG_PATH.parent, cli.LEGACY_STATE_DIR):
            if legacy.is_dir():
                shutil.rmtree(legacy)
                print(f"✓ deleted {legacy} (awsorg)")
        try:
            cli.CONFIG_PATH.parent.rmdir()  # only if it is now empty
        except OSError:
            pass
    tool = "pipx uninstall rolehop" if "pipx" in sys.prefix else "uv tool uninstall rolehop"
    if aws_error:
        print(f"\n✗ Uninstall incomplete: remove the rolehop blocks from {cli.AWS_CONFIG} by hand and try again.")
        return 1
    print(f"\nDone. To remove the command: {tool}")
    return 0


def register(sub) -> None:
    s = sub.add_parser("refresh", help="reload accounts and roles from the access portal (after your permissions change)")
    s.add_argument("orgs", nargs="*", help="organizations (default: all)")
    s.add_argument("--prune", action="store_true", help="also remove revoked accounts and roles (asks for confirmation)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--login", action="store_true", help="sign in first if needed")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation when removing profiles")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("remove", help="remove an organization, an account (<org>-<account>) or a profile from this machine")
    s.add_argument("targets", nargs="+", metavar="org|profile")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("uninstall", help="remove everything rolehop set up on this machine (profiles, agents, shell, state)")
    s.add_argument("--purge", action="store_true", help="also delete orgs.toml and the legacy awsorg configuration")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_uninstall)
