"""`rolehop run`: check (and renew) the session before leaving an agent unattended, and report anything that got blocked."""

from __future__ import annotations

import re
import subprocess

from rolehop import config, session, ui
from rolehop.i18n import _

MARGIN_MIN = 15


def parse_minutes(value: str) -> int | None:
    """Parse 90m | 3h | 1h30m | 120 into minutes; None if invalid."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m?)?", value.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def fmt(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def run(orgs_names: list[str], max_value: str, login: bool, yes: bool, command: list[str]) -> int:
    from rolehop import cli
    max_min = parse_minutes(max_value)
    if not max_min:
        ui.echo("✗ " + _("Invalid duration: {value}. Use a value like 90m, 3h or 1h30m.", value=max_value), err=True)
        return 2
    need = max_min + MARGIN_MIN
    orgs = config.select(config.load_orgs(), orgs_names)
    if login:
        ui.echo(_("Renewing the session so the full window starts now…"))
        rc = cli.cmd_login(type("A", (), {"orgs": [o["name"] for o in orgs], "force": True, "device": False})())
        if rc != 0:
            ui.echo("✗ " + _("Login failed: not starting."), err=True)
            return 1
    fail = False
    groups: set[str] = set()
    width = max((len(o["name"]) for o in orgs), default=0)
    for org in orgs:
        name = org["name"]
        r = session.check_org(org)
        left = r.get("estimated_left_min")
        if not r["ok"]:
            kind, detail, fix, group = cli.status_line(r)
            ui.item(kind, name, detail, "" if group else fix, width=width)  # sync/login are solved by one command below
            groups.add(group)
            fail = True
        elif left is None:
            if yes:
                ui.item("warn", name, _("session active, time left unknown (continuing because of --yes)"), width=width)
            else:
                ui.item("warn", name, _("session active, but the time left is unknown"),
                        _("run: {cmd}", cmd=f"rolehop run --login --max {max_value}"), width=width)
                fail = True
        elif left < need:
            best = fmt(max(left - MARGIN_MIN, 0))
            if login:
                ui.item("error", name, _("the access portal session lasts ~{left}; you asked for {max} (+{margin}m margin)",
                                         left=fmt(left), max=max_value, margin=MARGIN_MIN),
                        _("split the work, or run: {cmd}", cmd=f"rolehop run --max {best}"), width=width)
            else:
                ui.item("warn", name, _("~{left} left; the work needs {need} (margin included)",
                                        left=fmt(left), need=fmt(need)),
                        _("run: {cmd}", cmd=f"rolehop run --login --max {max_value}"), width=width)
            fail = True
        else:
            ui.item("ok", name, _("~{left} left (you need {need})", left=fmt(left), need=fmt(need)), width=width)
    if fail:
        ui.echo(err=True)
        ui.echo("✗ " + _("Nothing was started: the session does not last long enough for --max {max}.", max=max_value),
                err=True)
        if "rolehop sync" in groups:
            ui.fix(_("run: {cmd}", cmd="rolehop sync"), indent=2, err=True)
        if "rolehop login" in groups:
            ui.fix(_("run: {cmd}", cmd=f"rolehop run --login --max {max_value}"), indent=2, err=True)
        return 75
    if not command:
        ui.echo()
        ui.echo("✓ " + _("Ready: the session lasts at least {max}. You can leave your agent working.", max=max_value))
        ui.hint(_("If it expires earlier, approve the login from your phone (device code: rolehop login)."))
        return 0
    ui.echo(_("Running: {cmd}", cmd=" ".join(command)))
    try:
        rc = subprocess.run(command).returncode
    except FileNotFoundError:
        ui.echo("✗ " + _("Command not found: {cmd}", cmd=command[0]), err=True)
        return 127
    blocked = [o["name"] for o in orgs if session.read_state(o["name"]).get("blocked")]
    if blocked:
        ui.echo()
        ui.echo("! " + _("The AWS session ended during the work in: {orgs}", orgs=" ".join(blocked)))
        ui.fix(_("run: {cmd}", cmd=f"rolehop login {' '.join(blocked)}"), indent=2)
        ui.fix(_("then resume your agent's conversation (in Claude Code: claude --continue)"), indent=2)
    return rc
