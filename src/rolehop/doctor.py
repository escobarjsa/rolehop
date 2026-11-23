"""`rolehop doctor`: check the installation and explain how to fix each problem.

Offline by default (no AWS or GitHub calls); ``--online`` also probes every session with STS and checks for a
newer release.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from rolehop import __version__, awsconfig, config, errors, paths, portal, session, ui
from rolehop.i18n import _

MIN_AWS_CLI = (2, 22, 0)
AWS_CLI_INSTALL = "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "error"
    detail: str
    fix: str = ""


def aws_cli_version() -> tuple[int, ...] | None:
    """AWS CLI version as a tuple, () if it cannot be read, or None if `aws` is not on PATH."""
    exe = shutil.which("aws")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    m = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", out.stdout + out.stderr)
    return tuple(int(x) for x in m.groups()) if m else ()


def _run(cmd: str) -> str:
    return _("run: {cmd}", cmd=cmd)


def run_checks(online: bool = False) -> list[Check]:
    from rolehop import agents, cli, shellrc, upgrade

    checks: list[Check] = []
    tool = upgrade.installer()
    if tool:
        checks.append(Check("rolehop", "ok", _("{version} (installed with {tool})", version=__version__, tool=tool)))
    else:
        checks.append(Check("rolehop", "ok", _("{version} (not installed with uv or pipx)", version=__version__)))
    checks.append(Check("python", "ok", platform.python_version()))

    version = aws_cli_version()
    minimum = ".".join(map(str, MIN_AWS_CLI))
    if version is None:
        checks.append(Check("aws-cli", "error", _("`aws` was not found on PATH"),
                            _("install AWS CLI v2: {url}", url=AWS_CLI_INSTALL)))
    elif version == ():
        checks.append(Check("aws-cli", "warn", _("could not read the AWS CLI version"), _run("aws --version")))
    elif version < MIN_AWS_CLI:
        checks.append(Check("aws-cli", "warn", _("{version} is older than {minimum}", version=".".join(map(str, version)),
                                                minimum=minimum), _("upgrade AWS CLI v2: {url}", url=AWS_CLI_INSTALL)))
    else:
        checks.append(Check("aws-cli", "ok", ".".join(map(str, version))))

    exe, login = shutil.which("rolehop"), shutil.which("awslogin")
    if not exe:
        checks.append(Check("path", "warn", _("`rolehop` is not on PATH"),
                            _("run: {cmd}, then open a new terminal", cmd="rolehop setup")))
    elif not login:
        checks.append(Check("path", "warn", _("the `awslogin` shortcut is not on PATH"),
                            _run("uv tool install --force rolehop")))
    elif Path(login).resolve().parent != Path(exe).resolve().parent:
        checks.append(Check("path", "warn", _("`awslogin` resolves to {path}, not next to rolehop (an old copy?)", path=login),
                            _("remove {path}, or reinstall with --force", path=login)))
    else:
        checks.append(Check("path", "ok", _("`rolehop` and `awslogin` in {path}", path=Path(exe).parent)))
    for old in ("awsorg", "claude-aws-run"):
        if found := shutil.which(old):
            checks.append(Check("legacy", "warn", _("the old `{command}` command is still on PATH ({path})",
                                                    command=old, path=found), _run(f"rm {found}")))

    orgs: list[dict] = []
    try:
        orgs = config.load_orgs()
        total = len(config.catalog(orgs))
        checks.append(Check("orgs.toml", "ok", _("{path}: {orgs} org(s), {profiles} profile(s)",
                                                 path=ui.short(paths.CONFIG_PATH), orgs=len(orgs), profiles=total)))
    except errors.ConfigError as exc:
        checks.append(Check("orgs.toml", "error", str(exc), _("run: {cmd} (or fix the file)", cmd="rolehop init")))

    if orgs:
        current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
        try:
            new, _unused, problems = awsconfig.plan_sync(orgs, current)
        except errors.ConfigError as exc:
            checks.append(Check("aws-config", "error", str(exc),
                                _("fix {path} by hand, then run: {cmd}", path=ui.short(paths.AWS_CONFIG), cmd="rolehop sync")))
        else:
            if problems:
                items = ", ".join(f"[{o}] {c}" for o, cs in problems.items() for c in cs)
                checks.append(Check("aws-config", "error", _("sections conflict with generated profiles: {items}", items=items),
                                    _("rename or remove them, then run: {cmd}", cmd="rolehop sync")))
            elif new != current:
                checks.append(Check("aws-config", "warn", _("{path} is out of date", path=ui.short(paths.AWS_CONFIG)),
                                    _run("rolehop sync")))
            else:
                checks.append(Check("aws-config", "ok", _("{path} is in sync", path=ui.short(paths.AWS_CONFIG))))

    static = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN") if os.environ.get(v)]
    if static:
        checks.append(Check("env", "warn", _("static credentials are set ({names}); they override AWS_PROFILE",
                                             names=", ".join(static)),
                            _("unset {names} and remove them from your shell rc", names=" ".join(static))))

    out_of_sync = any(c.name == "aws-config" and c.status != "ok" for c in checks)
    for org in ([] if out_of_sync else orgs):  # sessions can't be checked until the profiles exist
        name = org["name"]
        if online:  # same wording as `rolehop status`; a missing session is a warning here, not an error
            kind, detail, fix, _group = cli.status_line({"org": name, **session.check_org(org)})
            checks.append(Check(f"session:{name}", "ok" if kind == "ok" else "warn", detail, fix))
            continue
        if session.read_state(name).get("blocked"):
            checks.append(Check(f"session:{name}", "warn", _("a recent command saw the session expire"),
                                _run(f"rolehop login {name}")))
            continue
        try:
            portal.cached_token(name)
            checks.append(Check(f"session:{name}", "ok", _("cached SSO token is valid")))
        except errors.PortalError as exc:
            reason = exc.reason if isinstance(exc, portal.LoginNeeded) else str(exc)
            checks.append(Check(f"session:{name}", "warn", reason, _run(f"rolehop login {name}")))

    shell = shellrc.detect_shell()
    rc = shellrc.rc_file(shell, Path.home())
    text = rc.read_text() if rc.exists() else ""
    if any(begin in text for begin, _end in shellrc.ALL_MARKS):
        checks.append(Check("shell", "ok", _("{path} is set up (PATH and direnv)", path=ui.short(rc))))
    else:
        checks.append(Check("shell", "warn", _("{path} is not set up (PATH may be missing ~/.local/bin)", path=ui.short(rc)),
                            _run("rolehop setup")))
    if not shutil.which("direnv"):
        checks.append(Check("direnv", "ok", _("not installed (optional, for per-repository defaults)")))

    home = Path.home()
    detected = [agents.status(a, home) for a in agents.AGENTS.values()]
    detected = [s for s in detected if s["detected"]]
    if not detected:
        checks.append(Check("agents", "ok", _("no AI coding agents detected")))
    for st in detected:
        if st["installed"]:
            checks.append(Check(f"agent:{st['agent']}", "ok", _("rules installed ({path})", path=st["rules"])))
        else:
            checks.append(Check(f"agent:{st['agent']}", "warn", _("detected, but its rules are not installed"),
                                _run(f"rolehop agent install {st['agent']}")))

    if online:
        try:
            rel = upgrade.latest_release()
            if upgrade._parse(rel["version"]) > upgrade._parse(__version__):
                checks.append(Check("release", "warn", _("{version} is available", version=rel["version"]),
                                    _run("rolehop upgrade")))
            else:
                checks.append(Check("release", "ok", _("{version} is the latest release", version=__version__)))
        except Exception as exc:  # noqa: BLE001 - informational only
            checks.append(Check("release", "warn", _("could not check for updates: {error}", error=exc),
                                _("try again later: {cmd}", cmd="rolehop upgrade --check")))
    return checks


def cmd_doctor(args) -> int:
    if args.json:  # machine-readable output is language-independent
        from rolehop import i18n
        i18n.set_language("en")
    checks = run_checks(online=args.online)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
        return 1 if any(c.status == "error" for c in checks) else 0
    width = max((len(c.name) for c in checks), default=0)
    for c in checks:
        ui.item(c.status, c.name, c.detail, c.fix if c.status != "ok" else "", width=width)
    warns = sum(c.status == "warn" for c in checks)
    errs = sum(c.status == "error" for c in checks)
    ui.echo()
    if errs:
        ui.echo("✗ " + _("{errors} error(s), {warnings} warning(s).", errors=errs, warnings=warns))
    elif warns:
        ui.echo("! " + _("{warnings} warning(s), no errors.", warnings=warns))
    else:
        ui.echo("✓ " + _("Everything looks good."))
    return 1 if errs else 0


def register(sub) -> None:
    s = sub.add_parser("doctor", help=_("check the installation and explain how to fix problems"))
    s.add_argument("--json", action="store_true", help=_("machine-readable output"))
    s.add_argument("--online", action="store_true", help=_("also probe sessions with STS and check for a newer release"))
    s.set_defaults(func=cmd_doctor)
