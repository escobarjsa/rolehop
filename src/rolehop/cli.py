"""rolehop: declarative AWS IAM Identity Center profiles for any number of organizations.

Generates native `sso-session` profiles in ~/.aws/config from orgs.toml. Login and token refresh are handled
by the AWS CLI and SDKs: rolehop never writes tokens or keys.

Subcommands:
  setup                        all-in-one: PATH, wizard if no config, profiles and AI agents
  init                         wizard: organizations, account and role discovery, orgs.toml
  sync [--dry-run] [--prune]   write the managed blocks to ~/.aws/config
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
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - clear message when run on an old Python
    sys.exit("rolehop requires Python 3.11 or later (for tomllib).")

from rolehop import __version__ as VERSION  # noqa: E402
from rolehop import awsconfig, config, errors, paths, portal, session, ui, wizard  # noqa: E402
from rolehop.i18n import _  # noqa: E402


def cmd_sync(args) -> int:
    orgs = config.load_orgs()
    current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
    new_free, kept, problems = awsconfig.plan_sync(orgs, current, args.prune, set(getattr(args, "prune_only", None) or []))
    if problems:
        ui.echo("✗ " + _("Some sections in {path}, outside the blocks managed by rolehop, conflict with generated profiles:",
                         path=paths.AWS_CONFIG), err=True)
        for org, items in problems.items():
            for it in items:
                ui.echo(f"    [{org}] {it}", err=True)
        ui.echo("  → " + _("rename or remove them by hand (back up the file first), then run: {cmd}",
                           cmd="rolehop sync"), err=True)
        return 2
    assert new_free is not None
    if kept:
        ui.echo("! " + _("Kept the blocks of organizations no longer in orgs.toml: {orgs}", orgs=", ".join(kept)))
        ui.echo("  → " + _("to remove them, run: {cmd}", cmd="rolehop sync --prune"))
    total = sum(len(config.catalog([o])) for o in orgs)
    if new_free == current:
        ui.echo("✓ " + _("{path} already up to date ({total} profile(s)).", path=paths.AWS_CONFIG, total=total))
        return 0
    if not args.quiet:
        diff = difflib.unified_diff(current.splitlines(True), new_free.splitlines(True),
                                    fromfile=str(paths.AWS_CONFIG), tofile=f"{paths.AWS_CONFIG} (new)")
        sys.stdout.writelines(diff)
    if args.dry_run:
        ui.echo()
        ui.echo("(" + _("dry run: nothing was written") + ")")
        return 0
    awsconfig.write_aws_config(new_free)
    ui.echo("✓ " + _("{path} updated: {total} profile(s) in {count} organization(s).",
                     path=paths.AWS_CONFIG, total=total, count=len(orgs)))
    return 0


def status_line(r: dict) -> tuple[str, str, str]:
    """(kind, detail, fix) for one org's session check, shared by `status` and `doctor`."""
    org = r.get("org", "?")
    if r.get("ok"):
        return "ok", _("active · {left} · account {account}", left=session.fmt_left(r.get("estimated_left_min")),
                       account=r.get("account") or "?"), ""
    if r.get("needs_sync"):
        return "error", _("profiles are not in {path} yet", path=ui.short(paths.AWS_CONFIG)), _("run: {cmd}", cmd="rolehop sync")
    if r.get("expired") or r.get("blocked"):
        return "error", _("session expired"), _("run: {cmd}", cmd=f"rolehop login {org}")
    doctor = _("run: {cmd}", cmd="rolehop doctor")
    code = r.get("code")
    if code == "aws_missing":
        return "error", _("the aws command was not found"), doctor
    if code == "no_profiles":
        return "error", _("this organization has no profiles in orgs.toml"), doctor
    if code == "timeout":
        retry = _("try again, or check your network with: {cmd}", cmd="rolehop doctor")
        return "error", _("AWS STS did not answer in time"), retry
    if code == "bad_response":
        return "error", _("unexpected answer from aws sts"), doctor
    return "error", _("no session ({reason})", reason=r.get("error") or "?"), _("run: {cmd}", cmd=f"rolehop login {org}")


def cmd_status(args) -> int:
    orgs = config.select(config.load_orgs(), args.orgs)
    results = [session.check_org(o) for o in orgs]
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if all(r["ok"] for r in results) else 1
    width = max((len(r["org"]) for r in results), default=0)
    for r in results:
        kind, detail, fix = status_line(r)
        ui.item(kind, r["org"], detail, fix, width=width)
        ui.debug(f"{r['org']}: checked with profile {r.get('probe_profile')}; cached role credentials can show "
                 "active for a while after the portal session ends")
    ready = sum(r["ok"] for r in results)
    if len(results) > 1 and ready < len(results):
        ui.echo()
        ui.echo(_("{ready} of {total} organizations ready.", ready=ready, total=len(results)))
    return 0 if ready == len(results) else 1


def cmd_language(args) -> int:
    from rolehop import i18n
    if args.code is None:
        lang, source = i18n.resolve()
        sources = {"flag": _("--lang option"), "env": "ROLEHOP_LANG", "settings": str(paths.SETTINGS_PATH),
                   "system": _("system locale"), "default": _("default")}
        ui.echo(_("Language: {name} ({code}), from {source}.", name=i18n.NAMES[lang], code=lang, source=sources[source]))
        ui.echo(_("Available: {list}. Change it with: rolehop language <code>  (auto = follow the system)",
                  list=", ".join(f"{c} ({n})" for c, n in i18n.NAMES.items())))
        return 0
    i18n.write_language(None if args.code == "auto" else args.code)
    i18n.set_language(None)
    lang, source = i18n.resolve()
    ui.echo("✓ " + _("Language set to {name} ({code}).", name=i18n.NAMES[lang], code=lang))
    if args.code != "auto" and source != "settings":
        ui.echo("! " + _("ROLEHOP_LANG or --lang is overriding it in this shell."))
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
        ui.echo(f"→ {org['name']}: {' '.join(cmd)}")
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
            ui.echo("  → " + _("try again, or check the setup with: {cmd}", cmd="rolehop doctor"), err=True)
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
        ui.echo("→ " + _("add an organization with: {cmd}", cmd="rolehop init"))
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
        ui.echo("  → " + _("use letters, digits and _, not starting with a digit (e.g. {example})",
                           example="TF_STATE_PROFILE"), err=True)
        return 2
    names = {r["profile"] for r in config.catalog(config.load_orgs())}
    for p in [args.profile] + ([args.state] if args.state else []):
        if p not in names:
            ui.echo("✗ " + _("Unknown profile: {profile}", profile=p), err=True)
            ui.echo("  → " + _("see the available profiles with: {cmd}", cmd="rolehop list"), err=True)
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
        ui.echo("✗ " + str(exc), err=True)
        ui.echo("  → " + _("sign in first: {cmd}", cmd=f"rolehop login {org['name']}"), err=True)
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
                    f"{r['hooks']:<{wh}} {r['rules']}")
        return 0
    try:
        targets = agents.resolve(args.names, args.all, home)
    except KeyError as exc:
        ui.echo("✗ " + str(exc.args[0]), err=True)
        ui.echo("  → " + _("see the supported agents with: {cmd}", cmd="rolehop agent list"), err=True)
        return 2
    if not targets:
        ui.echo(_("No installed AI agents detected."))
        ui.echo("→ " + _("name them explicitly: {cmd}", cmd="rolehop agent install claude codex …"))
        return 1
    ch = agents.Changes(args.dry_run)
    for a in targets:
        if args.action == "install":
            agents.install(a, home, ch, with_hooks=not args.no_hooks)
        else:
            agents.uninstall(a, home, ch)
    for line in ch.lines:
        ui.echo(f"  {line}")
    if args.action == "install" and not args.dry_run:
        ui.echo("  " + _("Restart your agents so they load the new instructions."))
    return 1 if any(line.startswith("✗") for line in ch.lines) else 0


def cmd_setup(args) -> int:
    from rolehop import shellrc
    ui.echo("==> 1. " + _("Shell (PATH and direnv)"))
    for m in shellrc.ensure_shell_integration(dry_run=args.dry_run):
        ui.echo(f"  {m}")
    ui.echo("==> 2. " + _("Organizations"))
    if not paths.CONFIG_PATH.exists():
        if args.dry_run or not sys.stdin.isatty():
            ui.echo("  ! " + _("{path} not found.", path=paths.CONFIG_PATH))
            ui.echo("    → " + _("run the wizard, then setup again: {cmd}", cmd="rolehop init && rolehop setup"))
            return 0 if args.dry_run else 1
        rc = wizard.cmd_init(args)
        if rc != 0 or not paths.CONFIG_PATH.exists():
            return rc or 1
    else:
        ui.echo("  ✓ " + _("Using {path}", path=paths.CONFIG_PATH))
    ui.echo("==> 3. " + _("Profiles in {path}", path=paths.AWS_CONFIG))
    rc = cmd_sync(argparse.Namespace(dry_run=args.dry_run, prune=False, quiet=True))
    if rc != 0:
        return rc
    ui.echo("==> 4. " + _("AI agents"))
    if args.no_agents:
        ui.echo("  (" + _("skipped: --no-agents") + ")")
    else:
        rc = cmd_agent(argparse.Namespace(action="install", names=args.agents or [], all=False,
                                          dry_run=args.dry_run, no_hooks=False, json=False))
        if rc == 1 and not args.agents:
            ui.echo("  (" + _("no agents detected") + ")")
            ui.echo("    → " + _("install them later with: {cmd}", cmd="rolehop agent install <agent>"))
    ui.echo("==> " + _("Done"))
    ui.echo("  → " + _("sign in and check the sessions: {cmd}", cmd="rolehop login && rolehop status"))
    ui.echo("  " + _('Then tell your agent: "use whatever AWS profiles and roles you need".'))
    return 0


def awslogin_main() -> int:
    return main(["login", *sys.argv[1:]])


COMMANDS = ("setup,init,sync,login,status,list,refresh,remove,run,agent,envrc,discover,doctor,language,upgrade,"
            "completion,uninstall")


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser (also used to generate shell completion)."""
    ap = argparse.ArgumentParser(
        prog="rolehop",
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
    s.add_argument("--quiet", action="store_true", help=_("don't show the diff"))
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("login", help=_("run aws sso login only where needed (shortcut: awslogin)"))
    s.add_argument("orgs", nargs="*")
    s.add_argument("--device", action="store_true", help=_("force the device code flow"))
    s.add_argument("--force", action="store_true", help=_("log in even if a session exists"))
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("status", help=_("session status per organization"))
    s.add_argument("orgs", nargs="*")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help=_("catalog of profiles"))
    s.add_argument("--json", action="store_true")
    s.add_argument("--org")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("envrc", help=_("print an .envrc for direnv"))
    s.add_argument("profile")
    s.add_argument("--state", help=_("profile for the Terraform/Terragrunt remote state"))
    s.add_argument("--state-var", default="AWS_STATE_PROFILE",
                   help=_("variable that holds the state profile (default: AWS_STATE_PROFILE)"))
    s.set_defaults(func=cmd_envrc)

    s = sub.add_parser("init", help=_("wizard: asks for your organizations and creates orgs.toml"))
    s.set_defaults(func=wizard.cmd_init)

    s = sub.add_parser("discover", help=_("list an organization's accounts and roles from the portal (requires a session)"))
    s.add_argument("org")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("setup", help=_("all-in-one: PATH, wizard if no config, profiles and AI agents"))
    s.add_argument("--dry-run", action="store_true")
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
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-hooks", action="store_true")
    s.add_argument("--json", action="store_true")
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
    return ap


QUIET_COMMANDS = ("hook", "_mark", "_which-org", "_complete", "completion", "uninstall", "upgrade", "doctor", "language")


def _preparse_lang(argv: list[str]) -> str | None:
    """Read --lang before building the parser, so --help itself is translated."""
    for i, arg in enumerate(argv):
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
    args = build_parser().parse_args(argv)
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
            ui.echo("  → " + _("copy {old} to {new} by hand", old=paths.LEGACY_CONFIG_PATH, new=paths.CONFIG_PATH),
                    err=True)
    try:
        return args.func(args)
    except errors.ConfigError as exc:
        ui.echo("✗ " + str(exc), err=True)
        if fix := getattr(exc, "fix", ""):
            ui.echo("  → " + fix, err=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
