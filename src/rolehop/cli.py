"""rolehop: declarative AWS IAM Identity Center profiles for any number of organizations.

Generates native `sso-session` profiles in ~/.aws/config from orgs.toml. Login and token refresh are handled
by the AWS CLI and SDKs: rolehop never writes tokens or keys.

Subcommands:
  setup                        all-in-one: PATH, wizard if no config, profiles and AI agents
  init                         wizard: organizations, account and role discovery, orgs.toml
  sync [--dry-run|--diff] [--prune]  write the managed blocks to ~/.aws/config
  login [org ...] [--force]    run aws sso login only where needed (shortcut: awslogin)
  status [org ...] [--json]    session status per org
  list [--json] [--org ORG]    catalog of available profiles
  run [--login] [--max 6h] [-- CMD]   check the session before leaving an agent to work
  agent list|install|uninstall|rules  instructions and hooks for AI agents
  envrc PROFILE [--state P]    print an .envrc for direnv
  discover ORG                 list an org's actual accounts and roles (requires a session)
  refresh [org ...] [--prune]  reload accounts and roles from the access portal
  remove TARGET ...            remove an organization, an account or a profile
  upgrade [--check]            upgrade to the latest GitHub release (checksum-verified)
  doctor [--json]              check the installation and explain how to fix problems
  completion SHELL             print a shell completion script (bash, zsh, fish)
  uninstall [--purge]          remove everything rolehop added to this machine

Config: $ROLEHOP_CONFIG or ~/.config/rolehop/orgs.toml
Target: $AWS_CONFIG_FILE or ~/.aws/config
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - clear message when run on an old Python
    sys.exit("rolehop requires Python 3.11 or later (for tomllib).")

from rolehop import __version__ as VERSION  # noqa: E402
from rolehop import awsconfig, config, errors, paths, portal, session, ui, wizard  # noqa: E402
from rolehop.i18n import _  # noqa: E402


def sync_summary(current: str, new: str, orgs: list[dict]) -> list[str]:
    """One line per organization whose managed block changes ("+ acme: added · profiles: 3")."""
    old_blocks, new_blocks = awsconfig.split_blocks(current)[1], awsconfig.split_blocks(new)[1]
    counts = {o["name"]: len(config.catalog([o])) for o in orgs}
    names = [o["name"] for o in orgs] + sorted(n for n in old_blocks if n not in counts)
    lines = []
    for name in names:
        if name not in old_blocks and name in new_blocks:
            lines.append("+ " + _("{org}: added · profiles: {count}", org=name, count=counts.get(name, 0)))
        elif name in old_blocks and name not in new_blocks:
            lines.append("- " + _("{org}: removed", org=name))
        elif name in old_blocks and old_blocks[name] != new_blocks[name]:
            lines.append("~ " + _("{org}: updated · profiles: {count}", org=name, count=counts.get(name, 0)))
    return lines


def cmd_sync(args) -> int:
    embedded = getattr(args, "embedded", False)  # inside `rolehop setup`: indented, and setup prints the dry-run footer
    pad = "  " if embedded else ""
    orgs = config.load_orgs()
    target = ui.short(paths.AWS_CONFIG)
    current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
    new_free, kept, problems = awsconfig.plan_sync(orgs, current, args.prune, set(getattr(args, "prune_only", None) or []))
    if problems:
        ui.echo(pad + "✗ " + _("Some sections in {path}, outside the blocks managed by rolehop, conflict with generated "
                               "profiles:", path=target), err=True)
        for org, items in problems.items():
            for it in items:
                ui.echo(f"{pad}    [{org}] {it}", err=True)
        ui.fix(_("rename or remove them by hand (back up the file first), then run: {cmd}", cmd="rolehop sync"),
               indent=len(pad) + 2, err=True)
        return 2
    assert new_free is not None
    if kept:
        ui.echo(pad + "! " + _("Kept the blocks of organizations no longer in orgs.toml: {orgs}", orgs=", ".join(kept)))
        ui.fix(_("to remove them, run: {cmd}", cmd="rolehop sync --prune"), indent=len(pad) + 2)
    total = len(config.catalog(orgs))
    if new_free == current:
        ui.echo(pad + "✓ " + _("{path} is already up to date. Profiles: {total}.", path=target, total=total))
        return 0
    if not args.quiet:
        for line in sync_summary(current, new_free, orgs):
            ui.echo(pad + line)
        if args.dry_run or getattr(args, "diff", False):
            ui.echo()
            sys.stdout.writelines(difflib.unified_diff(current.splitlines(True), new_free.splitlines(True),
                                                       fromfile=target, tofile=f"{target} (new)"))
    if args.dry_run:
        if embedded:
            ui.echo(pad + _("[dry-run]") + " " + _("would update {path} · profiles: {total}", path=target, total=total))
        else:
            ui.dry_run_footer(shlex.join(["rolehop", "sync", *(["--prune"] if args.prune else [])]))
        return 0
    awsconfig.write_aws_config(new_free, indent=pad)
    ui.echo(pad + "✓ " + _("Updated {path}. Profiles: {total}, organizations: {count}.",
                           path=target, total=total, count=len(orgs)))
    return 0


def status_line(r: dict) -> tuple[str, str, str, str]:
    """(kind, detail, fix, group) for one org's session check, shared by `status`, `run` and `doctor`.

    kind follows the traffic-light rule: "ok" done, "warn" the user has to act (sync or sign in), "error" something
    is broken. group is the command that fixes several orgs at once ("rolehop sync", "rolehop login"), or "".
    """
    org = r.get("org", "?")
    code = r.get("code")
    if r.get("ok"):
        return "ok", _("active · {left} · account {account}", left=session.fmt_left(r.get("estimated_left_min")),
                       account=r.get("account") or "?"), "", ""
    login = _("run: {cmd}", cmd=f"rolehop login {org}")
    doctor = _("run: {cmd}", cmd="rolehop doctor")
    if code == "needs_sync" or r.get("needs_sync"):
        return ("warn", _("profiles are not in {path} yet", path=ui.short(paths.AWS_CONFIG)),
                _("run: {cmd}", cmd="rolehop sync"), "rolehop sync")
    if code == "not_signed_in":
        return "warn", _("not signed in"), login, "rolehop login"
    if code == "expired" or r.get("expired") or r.get("blocked"):
        return "warn", _("session expired"), login, "rolehop login"
    if code == "forbidden":
        return ("error", _("access denied for {profile} (was the role revoked?)", profile=r.get("probe_profile") or "?"),
                _("run: {cmd}", cmd=f"rolehop refresh {org}"), "")
    if code == "network":
        return "error", _("could not reach AWS ({reason})", reason=r.get("error") or "?"), doctor, ""
    if code == "aws_missing":
        return "error", _("the aws command was not found"), doctor, ""
    if code == "no_profiles":
        return "error", _("this organization has no profiles in orgs.toml"), doctor, ""
    if code == "timeout":
        return ("error", _("AWS STS did not answer in time"),
                _("try again, or check your network with: {cmd}", cmd="rolehop doctor"), "")
    if code == "bad_response":
        return "error", _("unexpected answer from aws sts"), doctor, ""
    return "error", _("AWS returned an error: {reason}", reason=r.get("error") or "?"), doctor, ""


def print_status(results: list[dict]) -> None:
    """Aligned status lines; a fix shared by several orgs is printed once at the end ("→ run: rolehop login")."""
    rows = [(r, *status_line(r)) for r in results]
    groups = [row[4] for row in rows if row[4]]
    shared = {g for g in groups if groups.count(g) > 1}
    width = max((len(r.get("org", "")) for r in results), default=0)
    for r, kind, detail, fix, group in rows:
        ui.item(kind, r.get("org", "?"), detail, "" if group in shared else fix, width=width)
        ui.debug(f"{r.get('org')}: checked with profile {r.get('probe_profile')} (code={r.get('code')}); cached role "
                 "credentials can show active for a while after the portal session ends")
    for group in ("rolehop sync", "rolehop login"):
        if group in shared:
            ui.fix(_("run: {cmd}", cmd=group), indent=width + 4)


def cmd_status(args) -> int:
    orgs = config.select(config.load_orgs(), args.orgs)
    results = [session.check_org(o) for o in orgs]
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if all(r["ok"] for r in results) else 1
    print_status(results)
    ready = sum(bool(r["ok"]) for r in results)
    if len(results) > 1 and ready < len(results):
        ui.echo()
        ui.echo(_("{ready} of {total} organizations ready.", ready=ready, total=len(results)))
    return 0 if ready == len(results) else 1


def cmd_language(args) -> int:
    from rolehop import i18n
    sources = {"flag": _("--lang option"), "env": "ROLEHOP_LANG", "settings": ui.short(paths.SETTINGS_PATH),
               "system": _("system locale"), "default": _("default")}
    if args.code is None:
        lang, source = i18n.resolve()
        ui.echo(_("Language: {name} ({code}) · source: {source}", name=i18n.NAMES[lang], code=lang,
                  source=sources[source]))
        ui.hint(_("Available: {list}. Change it with: rolehop language <code>  (auto = use the system language)",
                  list=", ".join(f"{c} ({n})" for c, n in i18n.NAMES.items())))
        return 0
    try:
        i18n.write_language(None if args.code == "auto" else args.code)
    except (OSError, i18n.SettingsError) as exc:
        ui.echo("✗ " + _("Could not save the language: {error}", error=exc), err=True)
        ui.fix(_("edit {path} by hand and set: language = \"{code}\"", path=ui.short(paths.SETTINGS_PATH),
                 code=args.code), indent=2, err=True)
        return 1
    i18n.set_language(None)
    lang, source = i18n.resolve()
    if args.code == "auto":
        ui.echo("✓ " + _("Language: automatic (follows the system), now {name} ({code}).", name=i18n.NAMES[lang],
                         code=lang))
    else:
        ui.echo("✓ " + _("Saved language: {name} ({code}).", name=i18n.NAMES[args.code], code=args.code))
    if source in ("env", "flag") and args.code != "auto":
        ui.echo("! " + _("{override} overrides it in this terminal ({name}).", override=sources[source],
                         name=i18n.NAMES[lang]))
        if source == "env":
            ui.fix(_("run: {cmd}", cmd="unset ROLEHOP_LANG"), indent=2)
    return 0


def cmd_login(args) -> int:
    orgs = config.select(config.load_orgs(), args.orgs)
    rc = 0
    if not shutil.which("aws"):
        raise config.FixableError(_("aws command not found."),
                                  _("install AWS CLI v2, then run: {cmd}", cmd="rolehop doctor"))
    for org in orgs:
        if not args.force and not session.read_state(org["name"]).get("blocked"):
            r = session.check_org(org)
            if r["ok"]:
                ui.echo("✓ " + _("{org}: already has a session ({left}). Use --force to renew it.",
                                 org=org["name"], left=session.fmt_left(r.get("estimated_left_min"))))
                continue
        device = args.device or org["login_flow"] == "device"
        cmd = ["aws", "sso", "login", "--sso-session", org["name"]]
        if device:
            cmd.append("--use-device-code")
        ui.echo(_("Running: {cmd}", cmd=" ".join(cmd)))  # → is only for commands the user should run
        p = subprocess.run(cmd)
        ui.debug(f"aws sso login exited with {p.returncode}")
        if p.returncode == 0:
            session.write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
            r = session.check_org(org)
            if r["ok"]:
                ui.echo("✓ " + _("{org}: logged in · account {account}", org=org["name"], account=r["account"]))
            else:
                ui.echo("✓ " + _("{org}: logged in", org=org["name"]))
        else:
            ui.echo("✗ " + _("{org}: login failed (exit code {code}).", org=org["name"], code=p.returncode), err=True)
            ui.fix(_("try again, or check the setup with: {cmd}", cmd="rolehop doctor"), indent=2, err=True)
            rc = 1
    return rc


def cmd_list(args) -> int:
    orgs = config.load_orgs()
    if args.org:
        orgs = config.select(orgs, [args.org])
    rows = config.catalog(orgs)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        ui.echo(_("No profiles defined."))
        ui.fix(_("add an organization with: {cmd}", cmd="rolehop init"), indent=2)
        return 0
    hp, ho, ha, hr = _("PROFILE"), _("ORG"), _("ACCOUNT"), _("ROLE")
    w = max([len(hp)] + [len(r["profile"]) for r in rows])
    wo = max([len(ho)] + [len(r["org"]) for r in rows])
    wa = max([len(ha)] + [len(r["account"]) for r in rows])
    ui.echo(f"{hp:<{w}}  {ho:<{wo}}  {ha:<{wa}}  {'ID':<12}  {hr}")
    for r in rows:
        role = r["permission_set"] or r["role"]
        extra = "" if r["type"] == "sso" else f"  [{r['type']}]"
        ui.echo(f"{r['profile']:<{w}}  {r['org']:<{wo}}  {r['account']:<{wa}}  {r['account_id']:<12}  {role}{extra}")
    return 0


def cmd_envrc(args) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.state_var):
        ui.echo("✗ " + _("Invalid variable name: {name}", name=args.state_var), err=True)
        ui.fix(_("use letters, digits and _, not starting with a digit (e.g. {example})", example="TF_STATE_PROFILE"),
               indent=2, err=True)
        return 2
    names = {r["profile"] for r in config.catalog(config.load_orgs())}
    for p in [args.profile] + ([args.state] if args.state else []):
        if p not in names:
            ui.echo("✗ " + _("Unknown profile: {profile}", profile=p), err=True)
            ui.fix(_("see the available profiles with: {cmd}", cmd="rolehop list"), indent=2, err=True)
            return 2
    print("# .envrc generated by rolehop: default AWS profile for this repo.")
    print("# AI agents can use another profile by prefixing a command with AWS_PROFILE=<profile>.")
    print(f"export AWS_PROFILE={args.profile}")
    if args.state:
        print(f"export {args.state_var}={args.state}")
    print("unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN")
    return 0


def cmd_discover(args) -> int:
    org = config.select(config.load_orgs(), [args.org])[0]
    try:
        inv = portal.portal_inventory(org["name"], org["sso_region"])
    except errors.PortalError as exc:
        if isinstance(exc, portal.LoginNeeded):
            ui.echo("✗ " + ui.sentence(exc.reason), err=True)
            ui.fix(_("run: {cmd}", cmd=f"rolehop login {org['name']}"), indent=2, err=True)
        else:
            ui.echo("✗ " + ui.sentence(str(exc)), err=True)
            ui.fix(_("run: {cmd}", cmd="rolehop doctor"), indent=2, err=True)
        return 1
    print(f"# Accounts and roles visible in {org['name']} ({len(inv)}). Paste into orgs.toml and adjust the names.")
    for acc in inv:
        print(f'[[org.accounts]]\nname = "{config.short_account_name(acc["name"], org["name"])}"   # {acc["name"]}\n'
              f'id = "{acc["id"]}"\nroles = {json.dumps(acc["roles"])}\n')
    return 0


def cmd_mark(args) -> int:
    """Internal, for hooks: set or clear an org's 'session expired' state."""
    if not config.NAME_RE.match(args.org):
        return 0
    try:
        names = {o["name"] for o in config.load_orgs()}
    except errors.ConfigError:
        names = set()
    if names and args.org not in names:
        return 0
    session.write_state(args.org, blocked=(args.state == "blocked"),
                blocked_at=dt.datetime.now(dt.UTC).isoformat() if args.state == "blocked" else None)
    return 0


def cmd_which_org(args) -> int:
    org = session.org_of_profile(args.profile)
    if org:
        print(org)
        return 0
    return 1


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
        ha, hd, hi, hh, hr, y = _("AGENT"), _("DETECTED"), _("INSTALLED"), _("HOOKS"), _("INSTRUCTIONS"), _("yes")
        wa, wd, wi = (max(10, len(h) + 1) for h in (ha, hd, hi))
        wh = max(8, len(hh) + 1)
        ui.echo(f"{ha:<{wa}} {hd:<{wd}} {hi:<{wi}} {hh:<{wh}} {hr}")
        for r in rows:
            ui.echo(f"{r['agent']:<{wa}} {y if r['detected'] else '-':<{wd}} {y if r['installed'] else '-':<{wi}} "
                    f"{r['hooks']:<{wh}} {ui.short(r['rules'])}")
        return 0
    try:
        targets = agents.resolve(args.names, args.all, home)
    except KeyError as exc:
        ui.echo("✗ " + str(exc.args[0]), err=True)
        ui.fix(_("see the supported agents with: {cmd}", cmd="rolehop agent list"), indent=2, err=True)
        return 2
    if not targets:
        ui.echo(_("No installed AI agents detected."))
        ui.fix(_("name them explicitly: {cmd}", cmd=f"rolehop agent {args.action} claude codex …"), indent=2)
        return 1
    ch = agents.Changes(args.dry_run)
    for a in targets:
        if args.action == "install":
            agents.install(a, home, ch, with_hooks=not args.no_hooks)
        else:
            agents.uninstall(a, home, ch)
    for line in ch.lines:
        if line.startswith(agents.HINT):
            ui.hint("  " + line[len(agents.HINT):])
        else:
            ui.echo(f"  {line}")
    if args.action == "install" and ch.changed and not args.dry_run:
        ui.echo("  " + _("Restart your agents so they load the new instructions."))
    if args.dry_run and not getattr(args, "embedded", False):
        names = ["--all"] if args.all else list(args.names)
        ui.dry_run_footer(shlex.join(["rolehop", "agent", args.action, *names,
                                      *(["--no-hooks"] if getattr(args, "no_hooks", False) else [])]))
    return 1 if any(line.startswith("✗") for line in ch.lines) else 0


def cmd_setup(args) -> int:
    from rolehop import agents, shellrc
    rerun = shlex.join(["rolehop", "setup", *(["--no-agents"] if args.no_agents else []),
                        *[x for name in (args.agents or []) for x in ("--agent", name)]])
    ui.echo("==> 1. " + _("Shell (PATH and direnv)"))
    for m in shellrc.ensure_shell_integration(dry_run=args.dry_run):
        ui.echo(f"  {m}")
    ui.echo("==> 2. " + _("Organizations"))
    if not paths.CONFIG_PATH.exists():
        if args.dry_run:
            ui.echo("  ! " + _("{path} not found: setup will start the wizard to create it.",
                               path=ui.short(paths.CONFIG_PATH)))
            ui.dry_run_footer(rerun)
            return 0
        if not sys.stdin.isatty():
            ui.echo("  ! " + _("{path} not found.", path=ui.short(paths.CONFIG_PATH)))
            ui.fix(_("run the wizard, then setup again: {cmd}", cmd="rolehop init && rolehop setup"), indent=4)
            return 1
        rc = wizard.cmd_init(args)
        if rc != 0 or not paths.CONFIG_PATH.exists():
            return rc or 1
    else:
        ui.echo("  ✓ " + _("Using {path}", path=ui.short(paths.CONFIG_PATH)))
    ui.echo("==> 3. " + _("Profiles in {path}", path=ui.short(paths.AWS_CONFIG)))
    rc = cmd_sync(argparse.Namespace(dry_run=args.dry_run, prune=False, quiet=True, embedded=True))
    if rc != 0:
        return rc
    ui.echo("==> 4. " + _("AI agents"))
    if args.no_agents:
        ui.echo("  (" + _("skipped: --no-agents") + ")")
    elif not args.agents and not agents.resolve([], False, Path.home()):
        ui.echo("  (" + _("no agents detected") + ")")
        ui.fix(_("install them later with: {cmd}", cmd="rolehop agent install <agent>"), indent=4)
    else:
        cmd_agent(argparse.Namespace(action="install", names=args.agents or [], all=False, dry_run=args.dry_run,
                                     no_hooks=False, json=False, embedded=True))
    if args.dry_run:
        ui.dry_run_footer(rerun)
        return 0
    ui.echo("==> " + _("Done"))
    ui.fix(_("sign in and check the sessions: {cmd}", cmd="rolehop login && rolehop status"), indent=2)
    ui.hint("  " + _('Then tell your agent: "use whatever AWS profiles and roles you need".'))
    return 0


def awslogin_main() -> int:
    return main(["login", *sys.argv[1:]])


COMMANDS = ("setup,init,sync,login,status,list,refresh,remove,run,agent,envrc,discover,doctor,language,upgrade,"
            "completion,uninstall")


HIDDEN_COMMANDS = {"hook", "_mark", "_which-org", "_complete"}


class Parser(argparse.ArgumentParser):
    """argparse with a friendlier message for unknown subcommands (no internal names, a "did you mean")."""

    def _check_value(self, action, value):
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            import difflib
            visible = [c for c in action.choices if c not in HIDDEN_COMMANDS]
            close = difflib.get_close_matches(value, visible, n=1)
            msg = _("unknown command: {cmd}", cmd=value) + ". "
            msg += (_("Did you mean: {suggestion}?", suggestion=f"rolehop {close[0]}") if close
                    else _("See the commands with: {cmd}", cmd="rolehop --help"))
            raise argparse.ArgumentError(None, msg)
        super()._check_value(action, value)


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser (also used to generate shell completion)."""
    examples = [("rolehop setup", _("set everything up (first time)")),
                ("rolehop login", _("sign in where the session expired")),
                ("rolehop status", _("check the sessions")),
                ("rolehop list", _("show the available profiles"))]
    width = max(len(cmd) for cmd, _text in examples)
    epilog = _("Examples:") + "\n" + "\n".join(f"  {cmd:<{width}}  {text}" for cmd, text in examples)
    ap = Parser(
        prog="rolehop", allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=epilog,
        description="rolehop: " + _("declarative AWS IAM Identity Center profiles for any number of organizations."))
    ap.add_argument("--version", action="version", version=f"rolehop {VERSION}")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help=_("print debug details (commands run, files written) to stderr; also ROLEHOP_DEBUG=1"))
    ap.add_argument("--lang", choices=["en", "es", "pt"],
                    help=_("language for this run (default: ROLEHOP_LANG, `rolehop language`, then the system locale)"))
    ap.add_argument("--no-color", action="store_true", help=_("disable colors (also NO_COLOR=1)"))
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{" + COMMANDS + "}")

    s = sub.add_parser("sync", help=_("write the profiles to ~/.aws/config"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--prune", action="store_true", help=_("remove blocks for organizations no longer in orgs.toml"))
    s.add_argument("--diff", action="store_true", help=_("also show the full diff (always shown with --dry-run)"))
    s.add_argument("--quiet", action="store_true", help=_("don't show the summary or the diff"))
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("login", help=_("run aws sso login only where needed (shortcut: awslogin)"))
    s.add_argument("orgs", nargs="*", help=_("organizations (default: all)"))
    s.add_argument("--device", action="store_true", help=_("force the device code flow"))
    s.add_argument("--force", action="store_true", help=_("log in even if a session exists"))
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("status", help=_("session status per organization"))
    s.add_argument("orgs", nargs="*", help=_("organizations (default: all)"))
    s.add_argument("--json", action="store_true", help=_("machine-readable output"))
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help=_("catalog of profiles"))
    s.add_argument("--json", action="store_true", help=_("machine-readable output"))
    s.add_argument("--org", help=_("only the profiles of this organization"))
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("envrc", help=_("print an .envrc for direnv"))
    s.add_argument("profile", help=_("default profile for the directory"))
    s.add_argument("--state", help=_("profile for the Terraform/Terragrunt remote state"))
    s.add_argument("--state-var", default="AWS_STATE_PROFILE",
                   help=_("variable that holds the state profile (default: AWS_STATE_PROFILE)"))
    s.set_defaults(func=cmd_envrc)

    s = sub.add_parser("init", help=_("wizard: asks for your organizations and creates orgs.toml"))
    s.set_defaults(func=wizard.cmd_init)

    s = sub.add_parser("discover", help=_("list an organization's accounts and roles from the access portal "
                                          "(requires a session)"))
    s.add_argument("org", help=_("organization name"))
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("setup", help=_("all-in-one: PATH, wizard if no config, profiles and AI agents"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--no-agents", action="store_true", help=_("don't install instructions/hooks for agents"))
    s.add_argument("--agent", dest="agents", action="append", help=_("a specific agent (repeatable); default: detected ones"))
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run", help=_("check/renew the session before leaving an agent to work"))
    s.add_argument("--org", action="append", help=_("organization to check (repeatable); default: all"))
    s.add_argument("--max", default="4h", help=_("job duration: 90m, 3h, 1h30m (default: 4h)"))
    s.add_argument("--login", action="store_true", help=_("renew the session first (full window from now)"))
    s.add_argument("--yes", action="store_true", help=_("start even if the remaining time can't be estimated"))
    s.add_argument("command", nargs=argparse.REMAINDER, help=_("-- command to run (optional)"))
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("agent", help=_("instructions and hooks for AI agents (claude, codex, kiro, copilot…)"))
    s.add_argument("action", choices=["list", "install", "uninstall", "rules"])
    s.add_argument("names", nargs="*", help=_("agents; default: detected ones"))
    s.add_argument("--all", action="store_true", help=_("all supported agents"))
    s.add_argument("--dry-run", action="store_true", help=_("show the changes without writing anything"))
    s.add_argument("--no-hooks", action="store_true", help=_("install the rules only, without hooks"))
    s.add_argument("--json", action="store_true", help=_("machine-readable output (only with list)"))
    s.set_defaults(func=cmd_agent)

    from rolehop import completion, doctor, manage, upgrade
    manage.register(sub)
    doctor.register(sub)
    upgrade.register(sub)
    completion.register(sub)

    s = sub.add_parser("hook")
    s.add_argument("kind", choices=["post-tool", "cwd"])
    s.add_argument("--format", default="claude", choices=["claude", "codex", "copilot", "gemini", "qwen", "text"])
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("_mark")
    s.add_argument("org")
    s.add_argument("state", choices=["blocked", "clear"])
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("language", help=_("show or change the language (en, es, pt)"))
    s.add_argument("code", nargs="?", choices=["en", "es", "pt", "auto"])
    s.set_defaults(func=cmd_language)

    s = sub.add_parser("_which-org")
    s.add_argument("profile")
    s.set_defaults(func=cmd_which_org)
    # Global options also work after the subcommand (`rolehop status --lang es`); SUPPRESS keeps the top-level value.
    for subparser in sub.choices.values():
        subparser.allow_abbrev = False
        subparser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        subparser.add_argument("--lang", choices=["en", "es", "pt"], default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        subparser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return ap


QUIET_COMMANDS = ("hook", "_mark", "_which-org", "_complete", "completion", "uninstall", "upgrade", "doctor", "language")


def _preparse_lang(argv: list[str]) -> str | None:
    """Read --lang before building the parser, so --help itself is translated."""
    for i, arg in enumerate(argv):
        if arg == "--":  # everything after belongs to the wrapped command (`rolehop run -- tool --lang x`)
            break
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    from rolehop import i18n
    argv = sys.argv[1:] if argv is None else argv
    i18n.set_language(_preparse_lang(argv))
    i18n.install_argparse_translations()
    for stream in (sys.stdout, sys.stderr):  # never crash on terminals that cannot show ✓ or accents
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "lang", None):
        i18n.set_language(args.lang)
    ui.set_verbose(args.verbose)
    ui.set_no_color(args.no_color)
    ui.debug(f"rolehop {VERSION} · config={paths.CONFIG_PATH} · aws_config={paths.AWS_CONFIG} · state={paths.STATE_DIR}")
    if args.cmd not in QUIET_COMMANDS and not getattr(args, "dry_run", False):
        try:
            for msg in paths.migrate_legacy():
                ui.echo(msg, err=True)
        except OSError as exc:
            ui.echo("! " + _("Could not copy the old {legacy} configuration: {error}",
                             legacy=paths.LEGACY_NAME, error=exc), err=True)
            ui.fix(_("copy {old} to {new} by hand", old=ui.short(paths.LEGACY_CONFIG_PATH), new=ui.short(paths.CONFIG_PATH)),
                   indent=2, err=True)
    try:
        return args.func(args)
    except errors.ConfigError as exc:
        ui.echo("✗ " + str(exc), err=True)
        if fix := getattr(exc, "fix", ""):
            ui.fix(fix, indent=2, err=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
