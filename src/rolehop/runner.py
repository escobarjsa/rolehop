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
    width = max((len(o["name"]) for o in orgs), default=0)
    for org in orgs:
        name = org["name"]
        r = session.check_org(org)
        left = r.get("estimated_left_min")
        if not r["ok"]:
            ui.item("error", name, _("no session"), _("run: {cmd}", cmd=f"rolehop login {name}"), width=width)
            fail = True
        elif left is None:
            if yes:
                ui.item("warn", name, _("session active, time left unknown (continuing because of --yes)"), width=width)
            else:
                ui.item("warn", name, _("session active, but the time left is unknown"),
                        _("rerun with --login (recommended) or --yes"), width=width)
                fail = True
        elif left < need:
            best = fmt(max(left - MARGIN_MIN, 0))
            if login:
                ui.item("error", name, _("the access portal session lasts ~{left}; you asked for {max} (+{margin}m margin)",
                                         left=fmt(left), max=max_value, margin=MARGIN_MIN),
                        _("Use at most --max {best} or split the work.", best=best), width=width)
            else:
                ui.item("error", name, _("~{left} left; the work needs {need} (margin included)",
                                         left=fmt(left), need=fmt(need)),
                        _("rerun with --login to renew, or use --max {best}", best=best), width=width)
            fail = True
        else:
            ui.item("ok", name, _("~{left} left (you need {need})", left=fmt(left), need=fmt(need)), width=width)
    if fail:
        ui.echo()
        ui.echo("✗ " + _("Not starting: the session will not last for --max {max}.", max=max_value))
        return 75
    if not command:
        ui.echo()
        ui.echo("✓ " + _("Ready: the session will last ~{max}. You can leave your agent working.", max=max_value))
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
        ui.echo("→ " + _("run: {cmd}", cmd=f"rolehop login {' '.join(blocked)}"))
        ui.echo("→ " + _("then resume your agent's conversation (in Claude Code: claude --continue)"))
    return rc
