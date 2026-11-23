"""`rolehop agent`: installs the rules (and hooks, where the agent supports them) into AI coding agents.

Each agent gets:
  - global rules: either a dedicated file in a rules directory, or a marker-delimited block
    inside a single shared file (AGENTS.md, GEMINI.md…);
  - optionally, post-shell-command hooks that detect an expired SSO session.
Everything is idempotent, backed up and uninstallable.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from rolehop import ui
from rolehop.i18n import _
from rolehop.shellrc import find_any_block, find_block

HINT = "\x00hint:"  # prefix of a Changes line that is a dim tip, not something to act on

MD_BEGIN = "<!-- >>> rolehop (managed by rolehop, do not edit) >>> -->"
MD_END = "<!-- <<< rolehop <<< -->"
LEGACY_HOOK_MARKERS = ("awsorg/hooks/aws-expired.sh", "awsorg/hooks/aws-cwd-direnv.sh")  # old kit scripts
# Markers written by older versions: recognized and replaced on install, removed on uninstall.
LEGACY_MD: list[tuple[str, str]] = [
    ("<!-- >>> rolehop (gestionado por rolehop, no editar) >>> -->", "<!-- <<< rolehop <<< -->"),  # rolehop <= 1.4.0
    ("<!-- >>> awsorg (gestionado por awsorg, no editar) >>> -->", "<!-- <<< awsorg <<< -->"),      # awsorg <= 1.2.0
]
ALL_MD: list[tuple[str, str]] = [(MD_BEGIN, MD_END), *LEGACY_MD]
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
    rules_path: str | None              # relative to HOME
    rules_mode: str = "block"           # "file" (dedicated file) | "block" (block in a shared file) | "ui"
    frontmatter: str = ""
    hooks: str | None = None            # "claude" | "codex" | "copilot" | "gemini" | "qwen"

    def detected(self, home: Path) -> bool:
        return any(shutil.which(b) for b in self.binaries) or any((home / d).is_dir() for d in self.config_dirs)


AGENTS: dict[str, Agent] = {a.key: a for a in [
    Agent("claude", "Claude Code", ("claude",), (".claude",), ".claude/rules/aws.md", "file", hooks="claude"),
    Agent("codex", "OpenAI Codex CLI", ("codex",), (".codex",), ".codex/AGENTS.md", hooks="codex"),
    Agent("kiro", "Kiro", ("kiro-cli", "kiro"), (".kiro",), ".kiro/steering/aws.md", "file",
          frontmatter="---\ninclusion: always\n---\n\n"),
    Agent("copilot", "GitHub Copilot CLI", ("copilot",), (".copilot",), ".copilot/copilot-instructions.md",
          hooks="copilot"),
    Agent("gemini", "Gemini CLI", ("gemini",), (".gemini",), ".gemini/GEMINI.md", hooks="gemini"),
    Agent("qwen", "Qwen Code", ("qwen",), (".qwen",), ".qwen/QWEN.md", hooks="qwen"),
    Agent("opencode", "OpenCode", ("opencode",), (".config/opencode",), ".config/opencode/AGENTS.md"),
    Agent("amp", "Amp", ("amp",), (".config/amp",), ".config/amp/AGENTS.md"),
    Agent("pi", "Pi", ("pi",), (".pi/agent",), ".pi/agent/AGENTS.md"),
    Agent("devin", "Devin Desktop / Windsurf", ("devin", "windsurf"), (".devin", ".codeium/windsurf"),
          ".devin/global_rules.md"),
    Agent("cursor", "Cursor", ("cursor-agent", "cursor"), (".cursor",), None, "ui"),
]}


def tips(agent: Agent) -> list[str]:
    """Background facts about an agent, shown dimmed: nothing to do (translated when shown, not at import)."""
    if agent.key == "kiro":
        return [_("Kiro: rules only (steering), no hooks. Kiro's post-command hooks do not reliably "
                  "receive command output.")]
    return []


def notes(agent: Agent) -> list[str]:
    """Extra steps the user must take after installing for an agent (translated when shown, not at import)."""
    if agent.key == "codex":
        return [_("Codex: new hooks run only after you approve them. Open Codex and type /hooks.")]
    if agent.key == "opencode":
        return [_("OpenCode: if ~/.config/opencode/AGENTS.md did not exist before, OpenCode stops reading "
                  "~/.claude/CLAUDE.md as a fallback. Copy anything you still need from CLAUDE.md into it.")]
    if agent.key == "cursor":
        return [_("Cursor: run `rolehop agent rules` and paste the output into Cursor Settings → Rules. "
                  "Cursor also runs the Claude Code hooks when they are installed.")]
    return []


def rules_text() -> str:
    return resources.files("rolehop").joinpath("data/agent-rules.md").read_text(encoding="utf-8")


def rolehop_command() -> str:
    """Absolute path of the rolehop executable (hooks do not always inherit PATH)."""
    found = shutil.which("rolehop")  # stable PATH location (e.g. ~/.local/bin/rolehop) that survives reinstalls
    if found:
        return str(Path(found).absolute())
    argv0 = Path(sys.argv[0])
    if argv0.name == "rolehop" and argv0.exists():
        return str(argv0.absolute())
    local = Path.home() / ".local/bin/rolehop"
    return str(local) if local.exists() else "rolehop"


# --------------------------------------------------------------------------- file helpers
class Changes:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.lines: list[str] = []
        self.changed = False  # something was (or, in a dry run, would be) written or removed
        self._backed: set[Path] = set()

    def hint(self, text: str) -> None:
        self.lines.append(HINT + text)

    def write(self, path: Path, text: str, label: str) -> None:
        target = path.resolve() if path.is_symlink() else path
        shown = ui.short(path)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            self.lines.append("✓ " + _("{label}: already up to date ({path})", label=label, path=shown))
            return
        self.changed = True
        if self.dry_run:
            self.lines.append(_("[dry-run]") + " " + _("{label}: would write {path}", label=label, path=shown))
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        backup: Path | None = None
        if existed and target not in self._backed:
            backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(target, backup)
            self._backed.add(target)
        target.write_text(text, encoding="utf-8")
        if backup is not None:
            self.lines.append("✓ " + _("{label}: updated {path} (backup: {backup})",
                                       label=label, path=shown, backup=backup.name))
        elif existed:
            self.lines.append("✓ " + _("{label}: updated {path}", label=label, path=shown))
        else:
            self.lines.append("✓ " + _("{label}: created {path}", label=label, path=shown))

    def remove(self, path: Path, label: str) -> None:
        if not path.exists():
            return
        self.changed = True
        if self.dry_run:
            self.lines.append(_("[dry-run]") + " " + _("{label}: would remove {path}", label=label, path=ui.short(path)))
            return
        path.unlink()
        self.lines.append("✓ " + _("{label}: removed {path}", label=label, path=ui.short(path)))


def _with_block(current: str, body: str) -> str:
    block = f"{MD_BEGIN}\n{body.rstrip()}\n{MD_END}\n"
    span = find_any_block(current, ALL_MD)
    if span is not None:
        start, end = span
        if current[end:end + 1] == "\n":
            end += 1
        current = current[:start] + block + current[end:]
        for begin, end_mark in LEGACY_MD:  # drop any leftover legacy blocks
            current = _drop(current, begin, end_mark)
        return current
    sep = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    return current + sep + block


def _drop(current: str, begin: str, end_mark: str) -> str:
    while (span := find_block(current, begin, end_mark)) is not None:
        current = current[:span[0]].rstrip("\n") + "\n" + current[span[1]:].lstrip("\n")
    return current


def _without_block(current: str) -> str:
    out = current
    for begin, end_mark in ALL_MD:
        out = _drop(out, begin, end_mark)
    if out == current:
        return current
    return out if out.strip() else ""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)  # ValueError if malformed: the caller warns and changes nothing
    if not isinstance(data, dict):
        raise ValueError(_("not a JSON object"))
    return data


# --------------------------------------------------------------------------- per-agent hooks
def _hook_cmd(fmt: str) -> str:
    return f"{rolehop_command()} hook post-tool --format {fmt}"


def _is_ours(cmd: str) -> bool:
    return (("rolehop" in cmd or "awsorg" in cmd) and " hook " in cmd) or any(m in cmd for m in LEGACY_HOOK_MARKERS)


def _merge_nested_hooks(data: dict, event: str, matcher: str | None, command: str, timeout: int = 15) -> dict:
    """Claude/Codex/Gemini/Qwen format: {"hooks": {Event: [{"matcher":..., "hooks":[{type,command}]}]}}."""
    hooks = data.setdefault("hooks", {})
    groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
    for g in groups:  # remove our previous versions (including the old kit scripts)
        g["hooks"] = [h for h in g.get("hooks", []) if not _is_ours(str(h.get("command", "")))]
    groups = [g for g in groups if g.get("hooks")]
    entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
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
    }[agent.hooks or ""]


def install_hooks(agent: Agent, home: Path, ch: Changes) -> None:
    path = _hooks_target(agent, home)
    fmt = agent.hooks
    if fmt == "copilot":
        data: dict[str, Any] = {"version": 1, "hooks": {
            "PostToolUse": [{"type": "command", "bash": _hook_cmd("copilot"), "timeoutSec": 15}],
            "PostToolUseFailure": [{"type": "command", "bash": _hook_cmd("copilot"), "timeoutSec": 15}],
        }}
        ch.write(path, json.dumps(data, indent=2) + "\n", _("{agent} hooks", agent=agent.name))
        ch.remove(path.with_name("awsorg.json"), _("{agent} legacy hooks (awsorg)", agent=agent.name))
        return
    try:
        data = _read_json(path)
    except ValueError as exc:
        ch.lines.append("✗ " + _("{agent}: {path} is not valid JSON ({error}). Hooks not installed. "
                                 "Fix the file, then run: rolehop agent install {key}",
                                 agent=agent.name, path=ui.short(path), error=exc, key=agent.key))
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
    ch.write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", _("{agent} hooks", agent=agent.name))


def uninstall_hooks(agent: Agent, home: Path, ch: Changes) -> None:
    path = _hooks_target(agent, home)
    if agent.hooks == "copilot":
        ch.remove(path, _("{agent} hooks", agent=agent.name))
        ch.remove(path.with_name("awsorg.json"), _("{agent} legacy hooks (awsorg)", agent=agent.name))
        return
    try:
        data = _read_json(path)
    except ValueError as exc:
        ch.lines.append("✗ " + _("{agent}: {path} is not valid JSON ({error}). Hooks not removed. "
                                 "Fix the file, then run: rolehop agent uninstall {key}",
                                 agent=agent.name, path=ui.short(path), error=exc, key=agent.key))
        return
    if not data:
        return
    data = _strip_nested_hooks(data)
    if agent.hooks == "claude" and isinstance(data.get("permissions", {}).get("deny"), list):
        data["permissions"]["deny"] = [r for r in data["permissions"]["deny"] if r not in CLAUDE_DENY + LEGACY_DENY]
    ch.write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", _("{agent} hooks", agent=agent.name))


# --------------------------------------------------------------------------- install / uninstall
def install(agent: Agent, home: Path, ch: Changes, with_hooks: bool = True) -> None:
    if agent.rules_mode == "ui":
        ch.lines.append("! " + _("{agent}: no global rules file; add the rules by hand (see below).",
                                 agent=agent.name))
    else:
        assert agent.rules_path is not None  # every non-UI agent has a rules file
        path = home / agent.rules_path
        if agent.rules_mode == "file":
            ch.write(path, agent.frontmatter + rules_text(), _("{agent} rules", agent=agent.name))
        else:
            target = path.resolve() if path.is_symlink() else path
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            ch.write(path, _with_block(current, rules_text()), _("{agent} rules", agent=agent.name))
    if with_hooks and agent.hooks:
        install_hooks(agent, home, ch)
    for note in notes(agent):
        ch.lines.append(f"! {note}")
    for tip in tips(agent):
        ch.hint(tip)


def uninstall(agent: Agent, home: Path, ch: Changes) -> None:
    if agent.rules_path:
        path = home / agent.rules_path
        if agent.rules_mode == "file":
            ch.remove(path, _("{agent} rules", agent=agent.name))
        elif path.exists():
            new = _without_block(path.read_text(encoding="utf-8"))
            if new:
                ch.write(path, new, _("{agent} rules", agent=agent.name))
            else:
                ch.remove(path, _("{agent} rules", agent=agent.name))
    if agent.hooks:
        uninstall_hooks(agent, home, ch)


def status(agent: Agent, home: Path) -> dict:
    installed = False
    if agent.rules_path:
        p = home / agent.rules_path
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            installed = agent.rules_mode == "file" or any(begin in text for begin, _ in ALL_MD)
    return {"agent": agent.key, "name": agent.name, "detected": agent.detected(home), "installed": installed,
            "rules": str(home / agent.rules_path) if agent.rules_path else "(UI)", "hooks": agent.hooks or "-"}


def has_leftovers(agent: Agent, home: Path) -> bool:
    """True if this agent has anything rolehop installed: rules, hooks or (Claude Code) deny rules."""
    if status(agent, home)["installed"]:
        return True
    if not agent.hooks:
        return False
    path = _hooks_target(agent, home)
    for candidate in ([path, path.with_name("awsorg.json")] if agent.hooks == "copilot" else [path]):
        if not candidate.exists():
            continue
        if agent.hooks == "copilot":
            return True
        try:
            data = _read_json(candidate)
        except (ValueError, OSError):
            return False
        hooks = data.get("hooks")
        groups = [g for gs in hooks.values() if isinstance(gs, list) for g in gs] if isinstance(hooks, dict) else []
        commands = [str(h.get("command", "")) for g in groups if isinstance(g, dict)
                    for h in g.get("hooks", []) if isinstance(h, dict)]
        perms = data.get("permissions") if agent.hooks == "claude" else None
        deny = perms.get("deny") if isinstance(perms, dict) else None
        ours_denied = isinstance(deny, list) and bool(set(map(str, deny)) & set(CLAUDE_DENY + LEGACY_DENY))
        if ours_denied or any(_is_ours(c) for c in commands):
            return True
    return False


def resolve(names: list[str], use_all: bool, home: Path) -> list[Agent]:
    if use_all:
        return list(AGENTS.values())
    if names:
        unknown = [n for n in names if n not in AGENTS]
        if unknown:
            raise KeyError(_("Unknown agent: {names}. Available: {available}",
                             names=", ".join(unknown), available=", ".join(AGENTS)))
        return [AGENTS[n] for n in names]
    return [a for a in AGENTS.values() if a.detected(home)]
