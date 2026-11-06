"""`awsorg run`: comprobar (y renovar) la sesión antes de dejar trabajando a un agente, y avisar si algo quedó bloqueado."""

from __future__ import annotations

import re
import subprocess

MARGIN_MIN = 15


def parse_minutes(value: str) -> int | None:
    """90m | 3h | 1h30m | 120 → minutos; None si no es válida."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m?)?", value.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def fmt(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def run(orgs_names: list[str], max_value: str, login: bool, yes: bool, command: list[str]) -> int:
    from awsorg import cli
    max_min = parse_minutes(max_value)
    if not max_min:
        print(f"✗ Duración inválida: {max_value} (usa 90m, 3h, 1h30m)")
        return 2
    need = max_min + MARGIN_MIN
    orgs = cli.select(cli.load_orgs(), orgs_names)
    if login:
        print("→ Renovando la sesión para tener la ventana completa desde ahora…")
        rc = cli.cmd_login(type("A", (), {"orgs": [o["name"] for o in orgs], "force": True, "device": False})())
        if rc != 0:
            print("✗ El login falló.")
            return 1
    fail = False
    for org in orgs:
        r = cli.check_org(org)
        left = r.get("estimated_left_min")
        if not r["ok"]:
            print(f"✗ {org['name']}: sin sesión. Ejecuta: awslogin {org['name']}   (o usa --login)")
            fail = True
        elif left is None:
            if yes:
                print(f"! {org['name']}: sesión activa, tiempo restante desconocido (sigo por --yes)")
            else:
                print(f"! {org['name']}: sesión activa pero no sé cuánto queda. Usa --login (recomendado) o --yes.")
                fail = True
        elif left < need:
            best = max(left - MARGIN_MIN, 0)
            if login:
                print(f"✗ {org['name']}: la sesión del portal dura ~{fmt(left)} y pediste {max_value} "
                      f"(+{MARGIN_MIN}m de margen). Usa como máximo --max {fmt(best)} o divide el trabajo.")
            else:
                print(f"✗ {org['name']}: quedan ~{fmt(left)} y el trabajo necesita {fmt(need)} (con margen). "
                      f"Usa --login para renovar, o --max {fmt(best)}.")
            fail = True
        else:
            print(f"✓ {org['name']}: quedan ~{fmt(left)} (necesitas {fmt(need)}).")
    if fail:
        print(f"No arranco: la sesión no alcanza para --max {max_value}.")
        return 75
    if not command:
        print(f"✓ Listo: la sesión alcanza para ~{max_value}. Puedes dejar trabajando a tu agente.")
        print("  Si expira antes, puedes aprobar el login desde el celular (código de dispositivo: awslogin).")
        return 0
    print(f"→ Ejecutando: {' '.join(command)}")
    try:
        rc = subprocess.run(command).returncode
    except FileNotFoundError:
        print(f"✗ No se encontró el comando: {command[0]}")
        return 127
    blocked = [o["name"] for o in orgs if cli.read_state(o["name"]).get("blocked")]
    if blocked:
        print(f"\n⚠ La sesión AWS terminó durante el trabajo en: {' '.join(blocked)}")
        print(f"  1) awslogin {' '.join(blocked)}")
        print("  2) Reanuda la conversación de tu agente (en Claude Code: claude --continue).")
    return rc
