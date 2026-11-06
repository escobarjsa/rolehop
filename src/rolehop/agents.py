"""`rolehop agent`: instala las instrucciones (y hooks, donde el agente los soporta) en agentes de IA de código.

Cada agente tiene:
  - instrucciones globales: un archivo propio en un directorio de reglas, o un bloque con marcadores
    dentro de un archivo único (AGENTS.md, GEMINI.md…);
  - opcionalmente, hooks tras ejecutar comandos de shell para detectar la sesión SSO expirada.
Todo es idempotente, con copia de seguridad y desinstalable.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

MD_BEGIN = "<!-- >>> rolehop (gestionado por rolehop, no editar) >>> -->"
MD_END = "<!-- <<< rolehop <<< -->"
LEGACY_HOOK_MARKERS = ("awsorg/hooks/aws-expired.sh", "awsorg/hooks/aws-cwd-direnv.sh")  # scripts del kit
# Nombre anterior de la herramienta (awsorg, hasta 1.2.0): se reconoce y se reemplaza al instalar.
LEGACY_MD = ("<!-- >>> awsorg (gestionado por awsorg, no editar) >>> -->", "<!-- <<< awsorg <<< -->")
LEGACY_DENY = ["Bash(awsorg login *)", "Bash(awsorg run --login *)"]
CLAUDE_DENY = [
    "Read(~/.aws/**)", "Edit(~/.aws/**)",
    "Bash(aws configure export-credentials *)", "Bash(aws configure get *)", "Bash(aws sso get-role-credentials *)",
    "Bash(aws sso login *)", "Bash(awslogin *)", "Bash(rolehop login *)", "Bash(rolehop run --login *)",
    "Bash(claude-aws-run --login *)", "Bash(assume *)", "Bash(granted *)", "Bash(direnv allow *)",
]


@dataclass
class Agent:
    key: str
    name: str
    binaries: tuple[str, ...]
    config_dirs: tuple[str, ...]
    rules_path: str | None              # relativo a HOME
    rules_mode: str = "block"           # "file" (archivo propio) | "block" (bloque en archivo único) | "ui"
    frontmatter: str = ""
    hooks: str | None = None            # "claude" | "codex" | "copilot" | "gemini" | "qwen"
    notes: list[str] = field(default_factory=list)

    def detected(self, home: Path) -> bool:
        return any(shutil.which(b) for b in self.binaries) or any((home / d).is_dir() for d in self.config_dirs)


AGENTS: dict[str, Agent] = {a.key: a for a in [
    Agent("claude", "Claude Code", ("claude",), (".claude",), ".claude/rules/aws.md", "file", hooks="claude"),
    Agent("codex", "OpenAI Codex CLI", ("codex",), (".codex",), ".codex/AGENTS.md", hooks="codex",
          notes=["Codex pide confiar en los hooks nuevos: abre Codex y apruébalos con /hooks."]),
    Agent("kiro", "Kiro (CLI e IDE)", ("kiro-cli", "kiro"), (".kiro",), ".kiro/steering/aws.md", "file",
          frontmatter="---\ninclusion: always\n---\n\n",
          notes=["Kiro: solo instrucciones (steering). Sus hooks post-shell no entregan la salida de forma fiable."]),
    Agent("copilot", "GitHub Copilot CLI", ("copilot",), (".copilot",), ".copilot/copilot-instructions.md",
          hooks="copilot"),
    Agent("gemini", "Gemini CLI", ("gemini",), (".gemini",), ".gemini/GEMINI.md", hooks="gemini"),
    Agent("qwen", "Qwen Code", ("qwen",), (".qwen",), ".qwen/QWEN.md", hooks="qwen"),
    Agent("opencode", "OpenCode", ("opencode",), (".config/opencode",), ".config/opencode/AGENTS.md",
          notes=["OpenCode: si no tenías ~/.config/opencode/AGENTS.md, dejará de usar ~/.claude/CLAUDE.md como "
                 "respaldo. Añade allí lo que necesites de CLAUDE.md."]),
    Agent("amp", "Amp", ("amp",), (".config/amp",), ".config/amp/AGENTS.md"),
    Agent("pi", "Pi", ("pi",), (".pi/agent",), ".pi/agent/AGENTS.md"),
    Agent("devin", "Devin Desktop / Windsurf", ("devin", "windsurf"), (".devin", ".codeium/windsurf"),
          ".devin/global_rules.md"),
    Agent("cursor", "Cursor", ("cursor-agent", "cursor"), (".cursor",), None, "ui",
          notes=["Cursor no tiene un archivo de reglas global: pega `rolehop agent rules` en Settings → Rules. "
                 "Cursor también ejecuta los hooks de Claude Code si los tienes instalados."]),
]}


def rules_text() -> str:
    return resources.files("rolehop").joinpath("data/agent-rules.md").read_text(encoding="utf-8")


def rolehop_command() -> str:
    """Ruta absoluta del ejecutable rolehop (los hooks no siempre heredan el PATH)."""
    found = shutil.which("rolehop")  # ruta estable del PATH (p. ej. ~/.local/bin/rolehop), sobrevive a reinstalaciones
    if found:
        return str(Path(found).absolute())
    argv0 = Path(sys.argv[0])
    if argv0.name == "rolehop" and argv0.exists():
        return str(argv0.absolute())
    local = Path.home() / ".local/bin/rolehop"
    return str(local) if local.exists() else "rolehop"


# --------------------------------------------------------------------------- utilidades de archivo
class Changes:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.lines: list[str] = []
        self._backed: set[Path] = set()

    def write(self, path: Path, text: str, label: str) -> None:
        target = path.resolve() if path.is_symlink() else path
        if target.exists() and target.read_text(encoding="utf-8") == text:
            self.lines.append(f"✓ {label}: ya al día ({path})")
            return
        if self.dry_run:
            self.lines.append(f"[dry-run] {label}: escribiría {path}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target not in self._backed:
            backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(target, backup)
            self._backed.add(target)
        target.write_text(text, encoding="utf-8")
        self.lines.append(f"✓ {label}: {path}")

    def remove(self, path: Path, label: str) -> None:
        if not path.exists():
            return
        if self.dry_run:
            self.lines.append(f"[dry-run] {label}: borraría {path}")
            return
        path.unlink()
        self.lines.append(f"✓ {label}: borrado {path}")


def _with_block(current: str, body: str) -> str:
    block = f"{MD_BEGIN}\n{body.rstrip()}\n{MD_END}\n"
    if MD_BEGIN in current and MD_END in current:
        current = _drop(current, *LEGACY_MD)  # si también quedó el bloque antiguo, fuera
    begin, end_mark = (MD_BEGIN, MD_END) if MD_BEGIN in current else LEGACY_MD
    if begin in current and end_mark in current:
        start = current.index(begin)
        end = current.index(end_mark) + len(end_mark)
        if current[end:end + 1] == "\n":
            end += 1
        return current[:start] + block + current[end:]
    sep = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    return current + sep + block


def _drop(current: str, begin: str, end_mark: str) -> str:
    if begin not in current or end_mark not in current:
        return current
    start = current.index(begin)
    end = current.index(end_mark) + len(end_mark)
    return current[:start].rstrip("\n") + "\n" + current[end:].lstrip("\n")


def _without_block(current: str) -> str:
    out = _drop(_drop(current, MD_BEGIN, MD_END), *LEGACY_MD)
    if out == current:
        return current
    return out if out.strip() else ""


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)  # ValueError si está roto: el llamador avisa y no toca nada
    if not isinstance(data, dict):
        raise ValueError("no es un objeto JSON")
    return data


# --------------------------------------------------------------------------- hooks por agente
def _hook_cmd(fmt: str) -> str:
    return f"{rolehop_command()} hook post-tool --format {fmt}"


def _is_ours(cmd: str) -> bool:
    return (("rolehop" in cmd or "awsorg" in cmd) and " hook " in cmd) or any(m in cmd for m in LEGACY_HOOK_MARKERS)


def _merge_nested_hooks(data: dict, event: str, matcher: str | None, command: str, timeout: int = 15) -> dict:
    """Formato Claude/Codex/Gemini/Qwen: {"hooks": {Evento: [{"matcher":..., "hooks":[{type,command}]}]}}."""
    hooks = data.setdefault("hooks", {})
    groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
    for g in groups:  # quitar versiones anteriores nuestras (incluidos los scripts del kit)
        g["hooks"] = [h for h in g.get("hooks", []) if not _is_ours(str(h.get("command", "")))]
    groups = [g for g in groups if g.get("hooks")]
    entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher:
        entry = {"matcher": matcher, **entry}
    groups.append(entry)
    hooks[event] = groups
    return data


def _strip_nested_hooks(data: dict) -> dict:
    for event, groups in list(data.get("hooks", {}).items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict):
                g["hooks"] = [h for h in g.get("hooks", []) if not _is_ours(str(h.get("command", "")))]
                if g["hooks"]:
                    kept.append(g)
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]
    if data.get("hooks") == {}:
        del data["hooks"]
    return data


def _hooks_target(agent: Agent, home: Path) -> Path:
    return {
        "claude": home / ".claude/settings.json",
        "codex": home / ".codex/hooks.json",
        "gemini": home / ".gemini/settings.json",
        "qwen": home / ".qwen/settings.json",
        "copilot": home / ".copilot/hooks/rolehop.json",
    }[agent.hooks]


def install_hooks(agent: Agent, home: Path, ch: Changes) -> None:
    path = _hooks_target(agent, home)
    fmt = agent.hooks
    if fmt == "copilot":
        data = {"version": 1, "hooks": {
            "PostToolUse": [{"type": "command", "bash": _hook_cmd("copilot"), "timeoutSec": 15}],
            "PostToolUseFailure": [{"type": "command", "bash": _hook_cmd("copilot"), "timeoutSec": 15}],
        }}
        ch.write(path, json.dumps(data, indent=2) + "\n", f"{agent.name} hooks")
        ch.remove(path.with_name("awsorg.json"), f"{agent.name} hooks antiguos (awsorg)")
        return
    try:
        data = _read_json(path)
    except ValueError as exc:
        ch.lines.append(f"✗ {agent.name}: {path} no es JSON válido ({exc}); no toco sus hooks.")
        return
    if fmt == "claude":
        _merge_nested_hooks(data, "PostToolUse", "Bash", _hook_cmd("claude"))
        _merge_nested_hooks(data, "PostToolUseFailure", "Bash", _hook_cmd("claude"))
        _merge_nested_hooks(data, "CwdChanged", None, f"{rolehop_command()} hook cwd")
        perms = data.setdefault("permissions", {})
        deny = perms.setdefault("deny", [])
        for rule in CLAUDE_DENY:
            if rule not in deny:
                deny.append(rule)
    elif fmt == "codex":
        _merge_nested_hooks(data, "PostToolUse", "Bash", _hook_cmd("codex"))
    elif fmt == "gemini":
        _merge_nested_hooks(data, "AfterTool", "run_shell_command", _hook_cmd("gemini"))
    elif fmt == "qwen":
        _merge_nested_hooks(data, "PostToolUse", "^run_shell_command$", _hook_cmd("qwen"))
        _merge_nested_hooks(data, "PostToolUseFailure", "^run_shell_command$", _hook_cmd("qwen"))
    ch.write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", f"{agent.name} hooks")


def uninstall_hooks(agent: Agent, home: Path, ch: Changes) -> None:
    path = _hooks_target(agent, home)
    if agent.hooks == "copilot":
        ch.remove(path, f"{agent.name} hooks")
        ch.remove(path.with_name("awsorg.json"), f"{agent.name} hooks antiguos (awsorg)")
        return
    try:
        data = _read_json(path)
    except ValueError:
        ch.lines.append(f"✗ {agent.name}: {path} no es JSON válido; no lo toco.")
        return
    if not data:
        return
    data = _strip_nested_hooks(data)
    if agent.hooks == "claude" and isinstance(data.get("permissions", {}).get("deny"), list):
        data["permissions"]["deny"] = [r for r in data["permissions"]["deny"] if r not in CLAUDE_DENY + LEGACY_DENY]
    ch.write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", f"{agent.name} hooks")


# --------------------------------------------------------------------------- instalar / desinstalar
def install(agent: Agent, home: Path, ch: Changes, with_hooks: bool = True) -> None:
    if agent.rules_mode == "ui":
        ch.lines.append(f"! {agent.name}: sin archivo global de reglas.")
    else:
        path = home / agent.rules_path
        if agent.rules_mode == "file":
            ch.write(path, agent.frontmatter + rules_text(), f"{agent.name} instrucciones")
        else:
            target = path.resolve() if path.is_symlink() else path
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            ch.write(path, _with_block(current, rules_text()), f"{agent.name} instrucciones")
    if with_hooks and agent.hooks:
        install_hooks(agent, home, ch)
    for note in agent.notes:
        ch.lines.append(f"! {note}")


def uninstall(agent: Agent, home: Path, ch: Changes) -> None:
    if agent.rules_path:
        path = home / agent.rules_path
        if agent.rules_mode == "file":
            ch.remove(path, f"{agent.name} instrucciones")
        elif path.exists():
            new = _without_block(path.read_text(encoding="utf-8"))
            if new:
                ch.write(path, new, f"{agent.name} instrucciones")
            else:
                ch.remove(path, f"{agent.name} instrucciones")
    if agent.hooks:
        uninstall_hooks(agent, home, ch)


def status(agent: Agent, home: Path) -> dict:
    installed = False
    if agent.rules_path:
        p = home / agent.rules_path
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            installed = agent.rules_mode == "file" or MD_BEGIN in text or LEGACY_MD[0] in text
    return {"agent": agent.key, "name": agent.name, "detected": agent.detected(home), "installed": installed,
            "rules": str(home / agent.rules_path) if agent.rules_path else "(UI)", "hooks": agent.hooks or "-"}


def resolve(names: list[str], use_all: bool, home: Path) -> list[Agent]:
    if use_all:
        return list(AGENTS.values())
    if names:
        unknown = [n for n in names if n not in AGENTS]
        if unknown:
            raise KeyError(f"agente desconocido: {', '.join(unknown)}. Disponibles: {', '.join(AGENTS)}")
        return [AGENTS[n] for n in names]
    return [a for a in AGENTS.values() if a.detected(home)]
