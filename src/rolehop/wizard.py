"""`rolehop init`: interactive wizard that discovers accounts and roles and writes orgs.toml."""

from __future__ import annotations

import re
from typing import Any

from rolehop import config, errors, paths, portal


def ask(prompt: str, default: str | None = None, validate=None, error: str = "invalid value") -> str:
    """Prompt until the answer passes validate; an empty answer takes the default."""
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise errors.ConfigError("input closed: run the wizard in an interactive terminal") from None
        if not val and default is not None:
            val = default
        if val and (validate is None or validate(val)):
            return val
        print(f"    ✗ {error}")


def yes(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. Accepts y/yes and, for compatibility, s/si/sí."""
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not val else val.startswith(("y", "s"))


def accounts_from_inventory(org_name: str, inv: list[dict], roles_map: dict, ask_names: bool) -> list[dict]:
    accounts, used = [], set()
    for acc in inv:
        aliases = [config.alias_for(ps, roles_map) for ps in acc["roles"]]
        name = config.short_account_name(acc["name"], org_name)
        if ask_names:
            name = ask(f"Short name for '{acc['name']}' ({acc['id']})", name, config.NAME_RE.match,
                        "use lowercase letters, digits, - or _")
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        accounts.append({"name": name, "id": acc["id"], "roles": aliases})
    return accounts


def manual_accounts(roles_map: dict) -> list[dict]:
    print("  Enter your accounts (see the access portal: name, 12-digit ID, and the roles under each account).")
    accounts = []
    while True:
        name = ask("Account short name (e.g. devsecops)", None, config.NAME_RE.match, "use lowercase letters, digits, - or _")
        acc_id = ask("Account ID (12 digits)", None, lambda v: re.fullmatch(r"\d{12}", v), "must be 12 digits")
        roles_txt = ask("Roles, comma-separated, exactly as shown in the portal (e.g. Administrator, ReadOnly)")
        aliases = []
        for ps in [r.strip() for r in roles_txt.split(",") if r.strip()]:
            alias = next((k for k, v in roles_map.items() if v == ps), None) or config.role_alias(ps)
            roles_map.setdefault(alias, ps)
            aliases.append(alias)
        accounts.append({"name": name, "id": acc_id, "roles": aliases})
        if not yes("Add another account?", False):
            return accounts


def print_accounts(org_name: str, accounts: list[dict], roles_map: dict) -> None:
    for acc in accounts:
        roles = ", ".join(f"{a} ({roles_map.get(a, a)})" for a in acc["roles"])
        print(f"    {org_name}-{acc['name']:<20} {acc['id']}  {roles}")


def new_org(existing: list[dict]) -> dict:
    taken = {o["name"] for o in existing}
    print("\n  New organization. Find these details in the AWS access portal (the URL in your browser's address bar).")
    name = ask("Organization short name (e.g. acme, personal)", None,
                lambda v: config.NAME_RE.match(v) and v not in taken, "use lowercase letters, digits, - or _, and a new name")
    url = ask("Access portal URL (https://xxxx.awsapps.com/start)", None,
               lambda v: re.match(r"^https://[^\s/]+(/start)?/?(#.*)?$", v), "must be https://<name>.awsapps.com/start")
    url = re.sub(r"[/#].*$", "", url.split("://", 1)[1])
    url = f"https://{url}/start"
    sso_region = ask("Identity Center region", "us-east-1", lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v),
                      "expected a region, e.g. us-east-1")
    region = ask("Default region for profiles", sso_region,
                  lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v), "expected a region")
    hours = ask("Portal session length in hours (if unsure, 8)", "8",
                 lambda v: re.fullmatch(r"\d{1,4}(\.\d+)?", v), "expected a number of hours")
    org: dict[str, Any] = {"name": name, "start_url": url, "sso_region": sso_region, "region": region,
                           "login_flow": "device",
           "portal_session_hours": float(hours) if "." in hours else int(hours), "roles": {}, "accounts": []}
    if yes("Log in now and discover your accounts and roles automatically? (recommended)"):
        try:
            if not portal.login_for_discovery(org):
                raise errors.PortalError("login did not complete")
            inv = portal.portal_inventory(name, sso_region)
            if not inv:
                raise errors.PortalError("the portal returned no accounts")
            org["accounts"] = accounts_from_inventory(name, inv, org["roles"], ask_names=False)
            print(f"\n  Found {len(inv)} account(s):")
            print_accounts(name, org["accounts"], org["roles"])
            if not yes("Use these short names for the profiles?"):
                org["roles"] = {}
                org["accounts"] = accounts_from_inventory(name, inv, org["roles"], ask_names=True)
        except errors.PortalError as exc:
            print(f"  ! Automatic discovery failed ({exc}). Enter the accounts manually.")
            org["accounts"] = manual_accounts(org["roles"])
    else:
        org["accounts"] = manual_accounts(org["roles"])
    aliases = sorted({a for acc in org["accounts"] for a in acc["roles"]})
    org["default_role"] = "admin" if "admin" in aliases else (aliases[0] if aliases else "")
    return org


def refresh_org(org: dict) -> None:
    print(f"\n  Refreshing accounts and roles for '{org['name']}' from the portal.")
    if not portal.login_for_discovery(org):
        raise errors.ConfigError("login did not complete")
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
        print("  ✓ No changes: your orgs.toml already has every account and role.")
    for a in added:
        print(f"    + account {a}")
    for c in changed:
        print(f"    + roles {c}")


def cmd_init(args) -> int:
    print("\n=== rolehop wizard: AWS IAM Identity Center organizations ===")
    orgs: list[dict] = []
    if paths.CONFIG_PATH.exists():
        try:
            orgs = config.load_orgs()
        except errors.ConfigError as exc:
            print(f"  ✗ Your {paths.CONFIG_PATH} has a problem: {exc}\n  Fix it or move it aside, then run this again.")
            return 2
        print(f"  You already have {paths.CONFIG_PATH} with: {', '.join(o['name'] for o in orgs)}")
        print("  (If you edited it by hand, comments will be lost on save; a backup is made first.)")
    changed = False
    while True:
        if orgs:
            print("\n  What would you like to do?\n    1) Add another organization\n"
                  "    2) Refresh an organization's accounts/roles (from the portal)\n    3) Finish")
            choice = ask("Choice", "3", lambda v: v in ("1", "2", "3"), "choose 1, 2 or 3")
        else:
            choice = "1"
        if choice == "3":
            break
        if choice == "2":
            name = ask("Which organization?", orgs[0]["name"], lambda v: v in {o["name"] for o in orgs},
                        f"choose one of: {', '.join(o['name'] for o in orgs)}")
            refresh_org(next(o for o in orgs if o["name"] == name))
            changed = True
            continue
        org = new_org(orgs)
        if not org["accounts"]:
            print("  ! No accounts: skipping this organization.")
        else:
            orgs.append(org)
            changed = True
        if not yes("Add another organization?", False):
            break
    if not changed:
        print("  No changes.")
        return 0
    config.write_orgs(orgs)
    config.load_orgs()  # final check with the same rules as sync
    print("\n  Next step: `rolehop sync --dry-run`, then `rolehop sync` (or let the installer do it for you).")
    return 0
