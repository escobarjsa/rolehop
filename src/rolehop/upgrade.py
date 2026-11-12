"""`rolehop upgrade`: busca el último Release de GitHub y actualiza la herramienta con el mismo instalador."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from rolehop import __version__

REPO = "escobarjsa/rolehop"
API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version.split("+")[0])[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def latest_release() -> dict:
    url = os.environ.get("ROLEHOP_RELEASES_URL", API)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "rolehop"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    tag = data.get("tag_name") or ""
    wheel = next((a["browser_download_url"] for a in data.get("assets", [])
                  if str(a.get("name", "")).endswith(".whl")), None)
    return {"tag": tag, "version": tag.lstrip("v"), "wheel": wheel, "url": data.get("html_url", "")}


def installer() -> str | None:
    """'uv' o 'pipx' según dónde está instalado rolehop; None si no se reconoce (p. ej. un venv de desarrollo)."""
    prefix = sys.prefix.replace("\\", "/")
    uv_dir = (os.environ.get("UV_TOOL_DIR") or "").replace("\\", "/").rstrip("/")
    if "/uv/tools/" in prefix or (uv_dir and prefix.startswith(uv_dir + "/")):
        return "uv"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    return None


def upgrade_command(rel: dict, tool: str | None) -> list[str] | None:
    source = rel["wheel"] or f"git+https://github.com/{REPO}@{rel['tag']}"
    if tool == "uv" and shutil.which("uv"):
        return ["uv", "tool", "install", "--force", source]
    if tool == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", source]
    return None


def cmd_upgrade(args) -> int:
    try:
        rel = latest_release()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"✗ {REPO} todavía no tiene releases publicados en GitHub.", file=sys.stderr)
        else:
            print(f"✗ GitHub respondió {exc.code} al consultar los releases.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"✗ No pude consultar los releases de GitHub: {exc}", file=sys.stderr)
        return 1
    if not rel["tag"]:
        print("✗ GitHub no devolvió ningún release.", file=sys.stderr)
        return 1
    current, latest = _parse(__version__), _parse(rel["version"])
    if latest <= current and not args.force:
        print(f"✓ rolehop {__version__} ya es la última versión ({rel['tag']}).")
        return 0
    print(f"→ Hay una versión nueva: {__version__} → {rel['version']}  {rel['url']}")
    cmd = upgrade_command(rel, installer())
    if cmd is None:
        source = rel["wheel"] or f"git+https://github.com/{REPO}@{rel['tag']}"
        print(f"  No reconozco cómo se instaló rolehop. Actualiza a mano con:\n    uv tool install --force {source}")
        return 1 if not args.check else 0
    if args.check:
        print(f"  Para actualizar: rolehop upgrade   (ejecutará: {' '.join(cmd)})")
        return 0
    print(f"→ {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"✓ Actualizado a {rel['version']}. Si cambió algo de los agentes, ejecuta: rolehop agent install")
    return rc


def register(sub) -> None:
    s = sub.add_parser("upgrade", help="actualiza rolehop al último release de GitHub")
    s.add_argument("--check", action="store_true", help="solo comprueba si hay una versión nueva")
    s.add_argument("--force", action="store_true", help="reinstala aunque ya tengas la última")
    s.set_defaults(func=cmd_upgrade)
