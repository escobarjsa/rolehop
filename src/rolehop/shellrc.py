"""Shell integration: adds ~/.local/bin to PATH and the direnv hook, idempotently and with a backup."""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

MARK_BEGIN = "# >>> rolehop (managed by rolehop setup) >>>"
MARK_END = "# <<< rolehop <<<"
# Markers written by older versions: recognized, replaced on setup and removed on uninstall.
LEGACY_MARKS: list[tuple[str, str]] = [
    ("# >>> rolehop (gestionado por rolehop setup) >>>", "# <<< rolehop <<<"),  # rolehop <= 1.4.0 (Spanish)
    ("# >>> awsorg (gestionado por awsorg setup) >>>", "# <<< awsorg <<<"),      # previous name (awsorg <= 1.2.0)
]
ALL_MARKS: list[tuple[str, str]] = [(MARK_BEGIN, MARK_END), *LEGACY_MARKS]


def detect_shell() -> str:
    """zsh, bash or fish, based on $SHELL (defaults to bash)."""
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in ("zsh", "bash", "fish") else "bash"


def rc_file(shell: str, home: Path) -> Path:
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config/fish/config.fish"
    return home / ".bashrc"


def bin_dirs() -> list[Path]:
    """Directories where uv, pipx or the installer put executables."""
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


def find_block(text: str, begin: str, end: str) -> tuple[int, int] | None:
    """(start, end) of the first well-formed begin…end block (the end marker is searched after the begin marker)."""
    s = text.find(begin)
    if s < 0:
        return None
    e = text.find(end, s + len(begin))
    return None if e < 0 else (s, e + len(end))


def find_any_block(text: str, marks: list[tuple[str, str]]) -> tuple[int, int] | None:
    """Like find_block, trying each (begin, end) pair in order; returns the first match."""
    for begin, end in marks:
        if (span := find_block(text, begin, end)) is not None:
            return span
    return None


def _strip_managed(text: str, shell: str) -> str:
    """The rc text without our blocks (current and legacy), so we don't count ourselves."""
    text = text.replace(block_for(shell, True), "")
    for begin, end in ALL_MARKS:
        while (span := find_block(text, begin, end)) is not None:
            text = text[:span[0]] + text[span[1]:]
    return text


def ensure_shell_integration(dry_run: bool = False, home: Path | None = None, shell: str | None = None) -> list[str]:
    """Adds (or updates) the managed block in the shell rc file. Returns messages for the user."""
    home = home or Path.home()
    shell = shell or detect_shell()
    rc = rc_file(shell, home)
    target = rc.resolve() if rc.is_symlink() else rc
    current = target.read_text() if target.exists() else ""
    has_direnv = shutil.which("direnv") is not None
    already_direnv = f"direnv hook {shell}" in _strip_managed(current, shell)
    add_direnv = has_direnv and not already_direnv
    wanted = block_for(shell, add_direnv)
    msgs: list[str] = []
    span = find_any_block(current, ALL_MARKS)
    if span is not None:
        start, end = span
        if current[end:end + 1] == "\n":
            end += 1
        new = current[:start] + wanted + current[end:]
    else:
        sep = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
        new = current + sep + wanted
    for begin, end in LEGACY_MARKS:  # drop any leftover legacy blocks
        while (old := find_block(new, begin, end)) is not None:
            new = new[:old[0]].rstrip("\n") + "\n" + new[old[1]:].lstrip("\n")
    if new == current:
        msgs.append(f"✓ {rc} already has ~/.local/bin on PATH" + (" and direnv" if has_direnv else ""))
        return msgs
    if dry_run:
        msgs.append(f"[dry-run] would add to {rc}: ~/.local/bin on PATH" + (" + direnv hook" if add_direnv else ""))
        return msgs
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        msgs.append(f"✓ backup: {backup}")
    target.write_text(new)
    msgs.append(f"✓ {rc}: ~/.local/bin on PATH" + (" and direnv hook" if add_direnv else ""))
    on_path = any(str(d) in os.environ.get("PATH", "").split(os.pathsep) for d in bin_dirs())
    if not on_path:
        msgs.append(f"! Open a new terminal (or run: exec {shell}) for the PATH change to take effect.")
    return msgs


def remove_shell_integration(home: Path | None = None) -> list[str]:
    """Removes our block (current and legacy) from the zsh, bash and fish rc files, with a backup."""
    home = home or Path.home()
    msgs = []
    for shell in ("zsh", "bash", "fish"):
        rc = rc_file(shell, home)
        target = rc.resolve() if rc.is_symlink() else rc
        if not target.exists():
            continue
        current = target.read_text()
        new = current
        for begin, end in ALL_MARKS:
            while (span := find_block(new, begin, end)) is not None:
                head, tail = new[:span[0]], new[span[1]:]
                new = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
        if new == current:
            continue
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        target.write_text(new)
        msgs.append(f"✓ {rc}: rolehop block removed (backup: {backup.name})")
    return msgs
