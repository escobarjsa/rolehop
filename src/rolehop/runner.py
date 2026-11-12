"""`rolehop run`: check (and renew) the session before leaving an agent unattended, and report anything that got blocked."""

from __future__ import annotations

import re
import subprocess

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
        print(f"✗ Invalid duration: {max_value} (use 90m, 3h, 1h30m)")
        return 2
    need = max_min + MARGIN_MIN
    orgs = cli.select(cli.load_orgs(), orgs_names)
    if login:
        print("→ Renewing the session so the full window starts now…")
        rc = cli.cmd_login(type("A", (), {"orgs": [o["name"] for o in orgs], "force": True, "device": False})())
        if rc != 0:
            print("✗ Login failed.")
            return 1
    fail = False
    for org in orgs:
        r = cli.check_org(org)
        left = r.get("estimated_left_min")
        if not r["ok"]:
            print(f"✗ {org['name']}: no session. Run: awslogin {org['name']}   (or use --login)")
            fail = True
        elif left is None:
            if yes:
                print(f"! {org['name']}: session active, time remaining unknown (continuing because of --yes)")
            else:
                print(f"! {org['name']}: session active but time remaining is unknown. Use --login (recommended) or --yes.")
                fail = True
        elif left < need:
            best = max(left - MARGIN_MIN, 0)
            if login:
                print(f"✗ {org['name']}: the access portal session lasts ~{fmt(left)} and you asked for {max_value} "
                      f"(+{MARGIN_MIN}m margin). Use at most --max {fmt(best)} or split the work.")
            else:
                print(f"✗ {org['name']}: ~{fmt(left)} left and the work needs {fmt(need)} (including margin). "
                      f"Use --login to renew, or --max {fmt(best)}.")
            fail = True
        else:
            print(f"✓ {org['name']}: ~{fmt(left)} left (you need {fmt(need)}).")
    if fail:
        print(f"Not starting: the session will not last for --max {max_value}.")
        return 75
    if not command:
        print(f"✓ Ready: the session will last ~{max_value}. You can leave your agent working.")
        print("  If it expires earlier, you can approve the login from your phone (device code: awslogin).")
        return 0
    print(f"→ Running: {' '.join(command)}")
    try:
        rc = subprocess.run(command).returncode
    except FileNotFoundError:
        print(f"✗ Command not found: {command[0]}")
        return 127
    blocked = [o["name"] for o in orgs if cli.read_state(o["name"]).get("blocked")]
    if blocked:
        print(f"\n⚠ The AWS session ended during the work in: {' '.join(blocked)}")
        print(f"  1) awslogin {' '.join(blocked)}")
        print("  2) Resume your agent's conversation (in Claude Code: claude --continue).")
    return rc
