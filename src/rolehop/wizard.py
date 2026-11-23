"""`rolehop init`: interactive wizard that discovers accounts and roles and writes orgs.toml."""

from __future__ import annotations

import re
import sys
from typing import Any

from rolehop import config, errors, paths, portal, ui
from rolehop.i18n import _


def ask(prompt: str, default: str | None = None, validate=None, error: str = "") -> str:
    """Prompt until the answer passes validate; an empty answer takes the default."""
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise errors.ConfigError(_("No input available: run `rolehop init` in an interactive terminal.")) from None
        if not val and default is not None:
            val = default
        if val and (validate is None or validate(val)):
            return val
        ui.echo("    ✗ " + ui.sentence(error or _("invalid value")))


def yes(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. Accepts y/yes and s/si/sí/sim (Spanish and Portuguese)."""
    d = _("Y/n") if default else _("y/N")
    try:
        val = input(f"  {prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not val else val.startswith(("y", "s"))


def name_error() -> str:
    return _("use lowercase letters, digits, - or _")


def accounts_from_inventory(org_name: str, inv: list[dict], roles_map: dict, ask_names: bool) -> list[dict]:
    accounts, used = [], set()
    for acc in inv:
        aliases = [config.alias_for(ps, roles_map) for ps in acc["roles"]]
        name = config.short_account_name(acc["name"], org_name)
        if ask_names:
            name = ask(_("Short name for '{account}' ({id})", account=acc["name"], id=acc["id"]), name,
                       config.NAME_RE.match, name_error())
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        accounts.append({"name": name, "id": acc["id"], "roles": aliases})
    return accounts


def manual_accounts(roles_map: dict) -> list[dict]:
    ui.echo("  " + _("Enter your accounts (see the access portal: name, 12-digit ID, and the roles under each account)."))
    accounts = []
    while True:
        name = ask(_("Account short name (e.g. devsecops)"), None, config.NAME_RE.match, name_error())
        acc_id = ask(_("Account ID (12 digits)"), None, lambda v: re.fullmatch(r"\d{12}", v), _("must be 12 digits"))
        roles_txt = ask(_("Roles, comma-separated, exactly as shown in the access portal (e.g. Administrator, ReadOnly)"))
        aliases = []
        for ps in [r.strip() for r in roles_txt.split(",") if r.strip()]:
            alias = next((k for k, v in roles_map.items() if v == ps), None) or config.role_alias(ps)
            roles_map.setdefault(alias, ps)
            aliases.append(alias)
        accounts.append({"name": name, "id": acc_id, "roles": aliases})
        if not yes(_("Add another account?"), False):
            return accounts


def print_accounts(org_name: str, accounts: list[dict], roles_map: dict) -> None:
    for acc in accounts:
        roles = ", ".join(f"{a} ({roles_map.get(a, a)})" for a in acc["roles"])
        ui.echo(f"    {org_name}-{acc['name']:<20} {acc['id']}  {roles}")


def new_org(existing: list[dict]) -> dict:
    taken = {o["name"] for o in existing}
    ui.echo()
    ui.echo("  " + _("New organization. Find these details in the AWS access portal "
                     "(the URL in your browser's address bar)."))
    name = ask(_("Organization short name (e.g. acme, personal)"), None,
               lambda v: config.NAME_RE.match(v) and v not in taken,
               _("use lowercase letters, digits, - or _, and a new name"))
    url = ask(_("Access portal URL (https://xxxx.awsapps.com/start)"), None,
              lambda v: re.match(r"^https://[^\s/]+(/start)?/?(#.*)?$", v),
              _("must be https://<name>.awsapps.com/start"))
    url = re.sub(r"[/#].*$", "", url.split("://", 1)[1])
    url = f"https://{url}/start"
    sso_region = ask(_("Identity Center region"), "us-east-1", lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v),
                     _("expected a region, e.g. us-east-1"))
    region = ask(_("Default region for profiles"), sso_region,
                 lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v), _("expected a region, e.g. us-east-1"))
    hours = ask(_("Access portal session length in hours (if unsure, 8)"), "8",
                lambda v: re.fullmatch(r"\d{1,4}(\.\d+)?", v), _("expected a number of hours"))
    org: dict[str, Any] = {"name": name, "start_url": url, "sso_region": sso_region, "region": region,
                           "login_flow": "device",
                           "portal_session_hours": float(hours) if "." in hours else int(hours),
                           "roles": {}, "accounts": []}
    if yes(_("Sign in now and discover your accounts and roles automatically? (recommended)")):
        try:
            if not portal.login_for_discovery(org):
                raise errors.PortalError(_("sign-in did not complete"))
            inv = portal.portal_inventory(name, sso_region)
            if not inv:
                raise errors.PortalError(_("the access portal returned no accounts"))
            org["accounts"] = accounts_from_inventory(name, inv, org["roles"], ask_names=False)
            ui.echo()
            ui.echo("  " + _("Accounts found: {count}", count=len(inv)))
            print_accounts(name, org["accounts"], org["roles"])
            if not yes(_("Use these short names for the profiles?")):
                org["roles"] = {}
                org["accounts"] = accounts_from_inventory(name, inv, org["roles"], ask_names=True)
        except errors.PortalError as exc:
            ui.echo("  ! " + _("Automatic discovery failed ({error}). Enter the accounts manually.", error=exc))
            org["accounts"] = manual_accounts(org["roles"])
    else:
        org["accounts"] = manual_accounts(org["roles"])
    aliases = sorted({a for acc in org["accounts"] for a in acc["roles"]})
    org["default_role"] = "admin" if "admin" in aliases else (aliases[0] if aliases else "")
    return org


def refresh_org(org: dict) -> None:
    ui.echo()
    ui.echo("  " + _("Refreshing accounts and roles for '{org}' from the access portal.", org=org["name"]))
    if not portal.login_for_discovery(org):
        raise config.FixableError(_("Sign-in to '{org}' did not complete.", org=org["name"]),
                                  _("run: {cmd}", cmd=f"rolehop login {org['name']} && rolehop refresh {org['name']}"))
    try:
        inv = portal.portal_inventory(org["name"], org["sso_region"])
    except errors.PortalError as exc:
        raise errors.ConfigError(str(exc)) from None
    roles_map = org.setdefault("roles", {})
    by_id = {str(a["id"]): a for a in org.setdefault("accounts", [])}
    added, changed = [], []
    new_inv = [a for a in inv if a["id"] not in by_id]
    for acc in inv:
        if acc["id"] in by_id:
            cur = by_id[acc["id"]]
            fresh = accounts_from_inventory(org["name"], [acc], roles_map, ask_names=False)[0]["roles"]
            extra = [r for r in fresh if r not in cur["roles"]]
            if extra:
                cur["roles"] = list(cur["roles"]) + extra
                changed.append(f"{cur['name']}: +{', '.join(extra)}")
    if new_inv:
        names_used = {a["name"] for a in org["accounts"]}
        for acc in accounts_from_inventory(org["name"], new_inv, roles_map, ask_names=False):
            base, n = acc["name"], 2
            while acc["name"] in names_used:
                acc["name"], n = f"{base}-{n}", n + 1
            names_used.add(acc["name"])
            org["accounts"].append(acc)
            added.append(f"{acc['name']} ({acc['id']})")
    if not added and not changed:
        ui.echo("  ✓ " + _("No changes: your orgs.toml already has every account and role."))
    for a in added:
        ui.echo("    + " + _("account {account}", account=a))
    for c in changed:
        ui.echo("    + " + _("roles {roles}", roles=c))


def cmd_init(args) -> int:
    if not sys.stdin.isatty():  # fail before showing a menu nobody can answer
        ui.echo("✗ " + _("The wizard needs an interactive terminal to ask its questions."), err=True)
        ui.fix(_("open a terminal and run: {cmd}", cmd="rolehop init"), indent=2, err=True)
        return 2
    ui.echo()
    ui.echo("=== rolehop: " + _("setup wizard for AWS IAM Identity Center organizations") + " ===")
    orgs: list[dict] = []
    if paths.CONFIG_PATH.exists():
        try:
            orgs = config.load_orgs()
        except errors.ConfigError as exc:
            ui.echo("  ✗ " + _("Your {path} has a problem: {error}", path=ui.short(paths.CONFIG_PATH), error=exc))
            ui.fix(_("fix it or move it aside, then run: {cmd}", cmd="rolehop init"), indent=4)
            return 2
        ui.echo("  " + _("You already have {path} with: {orgs}", path=ui.short(paths.CONFIG_PATH),
                         orgs=", ".join(o["name"] for o in orgs)))
        ui.hint("  " + _("If you edited it by hand, comments will be lost on save (a backup is made first)."))
    changed = False
    while True:
        if orgs:
            ui.echo()
            ui.echo("  " + _("What would you like to do?"))
            ui.echo("    1) " + _("Add another organization"))
            ui.echo("    2) " + _("Refresh an organization's accounts and roles (from the access portal)"))
            ui.echo("    3) " + _("Finish"))
            choice = ask(_("Choice"), "3", lambda v: v in ("1", "2", "3"), _("choose 1, 2 or 3"))
        else:
            choice = "1"
        if choice == "3":
            break
        if choice == "2":
            name = ask(_("Which organization?"), orgs[0]["name"], lambda v: v in {o["name"] for o in orgs},
                       _("choose one of: {orgs}", orgs=", ".join(o["name"] for o in orgs)))
            refresh_org(next(o for o in orgs if o["name"] == name))
            changed = True
            continue
        org = new_org(orgs)
        if not org["accounts"]:
            ui.echo("  ! " + _("No accounts: skipping this organization."))
        else:
            orgs.append(org)
            changed = True
        if not yes(_("Add another organization?"), False):
            break
    if not changed:
        ui.echo("  " + _("No changes."))
        return 0
    config.write_orgs(orgs)
    config.load_orgs()  # final check with the same rules as sync
    if getattr(args, "cmd", "init") != "setup":  # `rolehop setup` writes the profiles itself right after
        ui.echo()
        ui.echo("  " + _("Next, review and write the profiles to {path}:", path=ui.short(paths.AWS_CONFIG)))
        ui.fix(_("run: {cmd}", cmd="rolehop sync --dry-run"), indent=2)
        ui.fix(_("run: {cmd}", cmd="rolehop sync"), indent=2)
    return 0
