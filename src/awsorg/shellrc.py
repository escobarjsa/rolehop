"""Integración con la shell: PATH a ~/.local/bin y hook de direnv, de forma idempotente y con copia de seguridad."""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

MARK_BEGIN = "# >>> awsorg (gestionado por awsorg setup) >>>"
MARK_END = "# <<< awsorg <<<"


def detect_shell() -> str:
    """zsh, bash o fish según $SHELL (por defecto bash)."""
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in ("zsh", "bash", "fish") else "bash"


def rc_file(shell: str, home: Path) -> Path:
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config/fish/config.fish"
    return home / ".bashrc"


def bin_dirs() -> list[Path]:
    """Carpetas donde uv/pipx/el instalador dejan los comandos."""
    dirs = [Path.home() / ".local/bin"]
    uv_bin = os.environ.get("UV_TOOL_BIN_DIR") or os.environ.get("XDG_BIN_HOME")
    if uv_bin:
        dirs.insert(0, Path(uv_bin))
    return dirs


def block_for(shell: str, with_direnv: bool) -> str:
    lines = [MARK_BEGIN]
    if shell == "fish":
        lines.append("fish_add_path -g $HOME/.local/bin")
        if with_direnv:
            lines.append("type -q direnv; and direnv hook fish | source")
    else:
        lines.append('case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac')
        if with_direnv:
            lines.append(f'command -v direnv >/dev/null 2>&1 && eval "$(direnv hook {shell})"')
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def ensure_shell_integration(dry_run: bool = False, home: Path | None = None, shell: str | None = None) -> list[str]:
    """Añade (o actualiza) el bloque gestionado en el rc de la shell. Devuelve mensajes para el usuario."""
    home = home or Path.home()
    shell = shell or detect_shell()
    rc = rc_file(shell, home)
    target = rc.resolve() if rc.is_symlink() else rc
    current = target.read_text() if target.exists() else ""
    has_direnv = shutil.which("direnv") is not None
    already_direnv = f"direnv hook {shell}" in current.replace(block_for(shell, True), "")
    wanted = block_for(shell, has_direnv and not already_direnv)
    msgs: list[str] = []
    if MARK_BEGIN in current and MARK_END in current:
        start = current.index(MARK_BEGIN)
        end = current.index(MARK_END) + len(MARK_END)
        if current[end:end + 1] == "\n":
            end += 1
        new = current[:start] + wanted + current[end:]
    else:
        sep = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
        new = current + sep + wanted
    if new == current:
        msgs.append(f"✓ {rc} ya tiene ~/.local/bin en el PATH" + (" y direnv" if has_direnv else ""))
        return msgs
    if dry_run:
        msgs.append(f"[dry-run] añadiría a {rc}: PATH con ~/.local/bin" + (" + hook de direnv" if has_direnv else ""))
        return msgs
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        msgs.append(f"✓ copia de seguridad: {backup}")
    target.write_text(new)
    msgs.append(f"✓ {rc}: ~/.local/bin en el PATH" + (" y hook de direnv" if has_direnv else ""))
    on_path = any(str(d) in os.environ.get("PATH", "").split(os.pathsep) for d in bin_dirs())
    if not on_path:
        msgs.append(f"! Abre una terminal nueva (o ejecuta: exec {shell}) para que el PATH se aplique.")
    return msgs
