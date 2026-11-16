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

from rolehop import __version__, awsconfig, config, errors, paths, portal, session

MIN_AWS_CLI = (2, 22, 0)
ICONS = {"ok": "✓", "warn": "!", "error": "✗"}


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


def run_checks(online: bool = False) -> list[Check]:
    from rolehop import agents, shellrc, upgrade

    checks: list[Check] = []
    tool = upgrade.installer()
    checks.append(Check("rolehop", "ok", f"{__version__} (installed with {tool or 'an unknown method'})"))
    checks.append(Check("python", "ok", platform.python_version()))

    version = aws_cli_version()
    if version is None:
        checks.append(Check("aws-cli", "error", "`aws` was not found on PATH",
                            "install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"))
    elif version == ():
        checks.append(Check("aws-cli", "warn", "could not read the AWS CLI version", "run: aws --version"))
    elif version < MIN_AWS_CLI:
        checks.append(Check("aws-cli", "warn", f"{'.'.join(map(str, version))} is older than 2.22.0",
                            "upgrade AWS CLI v2"))
    else:
        checks.append(Check("aws-cli", "ok", ".".join(map(str, version))))

    exe, login = shutil.which("rolehop"), shutil.which("awslogin")
    if not exe:
        checks.append(Check("path", "warn", "`rolehop` is not on PATH", "run: rolehop setup, then open a new terminal"))
    elif not login:
        checks.append(Check("path", "warn", "`awslogin` is not on PATH", "reinstall: uv tool install --force rolehop"))
    elif Path(login).resolve().parent != Path(exe).resolve().parent:
        checks.append(Check("path", "warn", f"`awslogin` resolves to {login}, not next to rolehop (an old copy?)",
                            f"remove {login} or reinstall rolehop with --force"))
    else:
        checks.append(Check("path", "ok", f"rolehop and awslogin in {Path(exe).parent}"))
    for old in ("awsorg", "claude-aws-run"):
        if found := shutil.which(old):
            checks.append(Check("legacy", "warn", f"the old `{old}` command is still on PATH ({found})",
                                f"remove it: rm {found}"))

    orgs: list[dict] = []
    try:
        orgs = config.load_orgs()
        total = len(config.catalog(orgs))
        checks.append(Check("orgs.toml", "ok", f"{paths.CONFIG_PATH}: {len(orgs)} org(s), {total} profile(s)"))
    except errors.ConfigError as exc:
        checks.append(Check("orgs.toml", "error", str(exc), "run: rolehop init (or fix the file)"))

    if orgs:
        current = paths.AWS_CONFIG.read_text() if paths.AWS_CONFIG.exists() else ""
        try:
            new, _, problems = awsconfig.plan_sync(orgs, current)
        except errors.ConfigError as exc:
            checks.append(Check("aws-config", "error", str(exc), f"fix {paths.AWS_CONFIG} by hand, then run: rolehop sync"))
        else:
            if problems:
                items = ", ".join(f"[{o}] {c}" for o, cs in problems.items() for c in cs)
                checks.append(Check("aws-config", "error", f"sections conflict with generated profiles: {items}",
                                    "rename or remove them, then run: rolehop sync"))
            elif new != current:
                checks.append(Check("aws-config", "warn", f"{paths.AWS_CONFIG} is out of date", "run: rolehop sync"))
            else:
                checks.append(Check("aws-config", "ok", f"{paths.AWS_CONFIG} is in sync"))

    static = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN") if os.environ.get(v)]
    if static:
        checks.append(Check("env", "warn", f"static credentials are set ({', '.join(static)}); they override AWS_PROFILE",
                            f"unset {' '.join(static)} and remove them from your shell rc"))

    for org in orgs:
        name = org["name"]
        if online:
            r = session.check_org(org)
            if r["ok"]:
                checks.append(Check(f"session:{name}", "ok", f"active · {session.fmt_left(r.get('estimated_left_min'))}"))
            else:
                checks.append(Check(f"session:{name}", "warn", r.get("error") or "no session", f"run: awslogin {name}"))
            continue
        if session.read_state(name).get("blocked"):
            checks.append(Check(f"session:{name}", "warn", "a recent command saw the session expire",
                                f"run: awslogin {name}"))
            continue
        try:
            portal.cached_token(name)
            checks.append(Check(f"session:{name}", "ok", "cached SSO token is valid"))
        except errors.PortalError as exc:
            checks.append(Check(f"session:{name}", "warn", str(exc), f"run: awslogin {name}"))

    shell = shellrc.detect_shell()
    rc = shellrc.rc_file(shell, Path.home())
    text = rc.read_text() if rc.exists() else ""
    if any(begin in text for begin, _ in shellrc.ALL_MARKS):
        checks.append(Check("shell", "ok", f"{rc} has the rolehop block"))
    else:
        checks.append(Check("shell", "warn", f"{rc} has no rolehop block (PATH may be missing ~/.local/bin)",
                            "run: rolehop setup"))
    if not shutil.which("direnv"):
        checks.append(Check("direnv", "ok", "not installed (optional, for per-repository defaults)"))

    home = Path.home()
    detected = [agents.status(a, home) for a in agents.AGENTS.values()]
    detected = [s for s in detected if s["detected"]]
    if not detected:
        checks.append(Check("agents", "ok", "no AI coding agents detected"))
    for st in detected:
        if st["installed"]:
            checks.append(Check(f"agent:{st['agent']}", "ok", f"rules installed ({st['rules']})"))
        else:
            checks.append(Check(f"agent:{st['agent']}", "warn", "detected but rolehop rules are not installed",
                                f"run: rolehop agent install {st['agent']}"))

    if online:
        try:
            rel = upgrade.latest_release()
            if upgrade._parse(rel["version"]) > upgrade._parse(__version__):
                checks.append(Check("release", "warn", f"{rel['version']} is available", "run: rolehop upgrade"))
            else:
                checks.append(Check("release", "ok", f"{__version__} is the latest release"))
        except Exception as exc:  # noqa: BLE001 - informational only
            checks.append(Check("release", "warn", f"could not check for updates: {exc}"))
    return checks


def cmd_doctor(args) -> int:
    checks = run_checks(online=args.online)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            print(f"{ICONS[c.status]} {c.name:<16} {c.detail}")
            if c.fix and c.status != "ok":
                print(f"  {'':<16} → {c.fix}")
        warns = sum(c.status == "warn" for c in checks)
        errs = sum(c.status == "error" for c in checks)
        print(f"\n{errs} error(s), {warns} warning(s)." if errs or warns else "\nEverything looks good.")
    return 1 if any(c.status == "error" for c in checks) else 0


def register(sub) -> None:
    s = sub.add_parser("doctor", help="check the installation and explain how to fix problems")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--online", action="store_true", help="also probe sessions with STS and check for a newer release")
    s.set_defaults(func=cmd_doctor)
