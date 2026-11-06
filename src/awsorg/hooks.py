"""Hooks para agentes de IA: detectar la sesión SSO expirada tras un comando de shell y cargar direnv al cambiar de carpeta.

Nunca bloquean nada ni leen credenciales. Ante cualquier error salen en silencio (código 0).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ¿El comando toca AWS? Programa AWS al inicio de un segmento (tras ; & | ( o $( ), admitiendo envoltorios
# (time, nice, nohup, timeout N) y asignaciones VAR=valor delante; o un AWS_PROFILE= explícito.
AWS_CMD_RE = re.compile(
    r"(^|[;&|(]|\$\()\s*((time|nice|nohup|timeout\s+\d+[smh]?)\s+)*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(aws|awsorg|terraform|tofu|terragrunt|cdk|sam|eksctl|kubectl)(\s|$)|AWS_PROFILE=")
PROFILE_RE = re.compile(r"AWS_PROFILE=([A-Za-z0-9_.-]+)")
PROFILE_FLAG_RE = re.compile(r"--profile[ =]([A-Za-z0-9_.-]+)")
ORG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Campos donde cada agente entrega el comando y su salida (se prueban en orden).
COMMAND_KEYS = (("tool_input", "command"), ("toolArgs", "command"), ("tool_input", "cmd"), ("command",))
STDERR_KEYS = (("tool_response", "stderr"), ("tool_result", "stderr"), ("stderr",))
ERROR_KEYS = (("error",), ("tool_response", "error"), ("tool_result", "error"), ("toolResult", "error"))
# Salida combinada (stdout+stderr juntos) en agentes que no separan stderr (Gemini, Qwen, Copilot).
COMBINED_KEYS = (("tool_response", "llmContent"), ("tool_response", "returnDisplay"), ("tool_response", "output"),
                 ("toolResult", "textResultForLlm"), ("tool_result", "textResultForLlm"), ("tool_response",))
TOOL_KEYS = (("tool_name",), ("toolName",), ("tool",))
SHELL_TOOLS = {"bash", "shell", "run_shell_command", "execute_command", "exec_command", "local_shell", "terminal",
               "execute_bash"}
# Con salida combinada no se acepta el genérico "ExpiredToken" (aparece en datos legítimos, p. ej. CloudTrail).
STRICT_EXPIRED_RE = re.compile(
    r"Error when retrieving token from sso|The SSO session associated with this profile has expired"
    r"|UnauthorizedSSOToken|Token has expired and refresh failed|refresh cached SSO token failed"
    r"|InvalidGrantException|SSO session.{0,40}(has expired|is invalid)|Error loading SSO Token")


def _get(data: dict, *paths: tuple[str, ...]) -> str:
    for path in paths:
        cur = data
        for key in path:
            if isinstance(cur, str) and cur.lstrip().startswith("{"):  # p. ej. toolArgs de Copilot como JSON
                try:
                    cur = json.loads(cur)
                except ValueError:
                    cur = None
            cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str) and cur:
            return cur
    return ""


def _org_for(profile: str) -> str:
    from awsorg import cli
    org = None
    try:
        for row in cli.catalog(cli.load_orgs()):
            if row["profile"] == profile:
                org = row["org"]
                break
    except cli.ConfigError:
        pass
    org = org or (profile.split("-", 1)[0] if profile else "")
    return org if ORG_RE.match(org or "") else ""


def _herdr_title(text: str | None) -> None:
    pane = os.environ.get("HERDR_PANE_ID")
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not pane or not herdr:
        return
    args = [herdr, "pane", "report-metadata", "--source", "awsorg"]
    args += ["--title", text] if text else ["--clear-title"]
    try:
        subprocess.Popen(args + [pane], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def _emit(fmt: str, event: str, message: str) -> None:
    """Devuelve contexto al agente en su formato."""
    default_event = {"claude": "PostToolUse", "codex": "PostToolUse", "qwen": "PostToolUse", "gemini": "AfterTool",
                     "copilot": "PostToolUse"}
    if fmt in ("claude", "codex", "qwen", "gemini"):
        out = {"hookSpecificOutput": {"hookEventName": event or default_event[fmt], "additionalContext": message}}
    elif fmt == "copilot":
        out = {"additionalContext": message,
               "hookSpecificOutput": {"hookEventName": event or "PostToolUse", "additionalContext": message}}
    else:  # genérico: texto plano por stdout
        print(message)
        return
    print(json.dumps(out, ensure_ascii=False))


def post_tool(fmt: str = "claude", stdin: str | None = None) -> int:
    """Hook tras un comando de shell: detecta la sesión SSO expirada y avisa al agente."""
    from awsorg import cli
    try:
        data = json.loads(stdin if stdin is not None else sys.stdin.read())
    except (ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    tool = _get(data, *TOOL_KEYS).lower()
    if tool and tool not in SHELL_TOOLS:
        return 0
    cmd = _get(data, *COMMAND_KEYS)
    if not AWS_CMD_RE.search(cmd):
        return 0
    event = _get(data, ("hook_event_name",), ("hookEventName",), ("event",))
    error_text = _get(data, *ERROR_KEYS)
    failed = "Failure" in event or bool(error_text)
    if failed:
        expired = bool(cli.EXPIRED_RE.search(error_text or _get(data, *COMBINED_KEYS)))
    else:
        stderr = _get(data, *STDERR_KEYS)
        if stderr or ("tool_response" in data and isinstance(data["tool_response"], dict)
                      and "stderr" in data["tool_response"]):
            expired = bool(cli.EXPIRED_RE.search(stderr))
        else:
            expired = bool(STRICT_EXPIRED_RE.search(_get(data, *COMBINED_KEYS)))

    state_dir = cli.STATE_DIR
    if not expired:
        if failed:
            return 0
        marked = [p for p in state_dir.glob("*.json")] if state_dir.exists() else []
        if not any('"blocked": true' in p.read_text() or '"blocked_at": "' in p.read_text() for p in marked):
            return 0

    m = PROFILE_RE.findall(cmd) or PROFILE_FLAG_RE.findall(cmd)
    profile = m[-1] if m else os.environ.get("AWS_PROFILE", "")
    org = _org_for(profile)

    if expired:
        who = org or "<org>"
        if org:
            cli.write_state(org, blocked=True, blocked_at=cli.dt.datetime.now(cli.dt.timezone.utc).isoformat())
        _herdr_title(f"⚠ AWS: ejecuta awslogin {who}")
        msg = (f"AWS SSO: la sesión de la organización '{who}' terminó (perfil: {profile or 'desconocido'}). "
               f"DETENTE con AWS: no reintentes ni cambies de perfil para esquivarlo. Pide al usuario que ejecute "
               f"en su terminal: awslogin {who} . Mientras tanto puedes seguir con tareas que no usen AWS. "
               f"Cuando confirme, continúa desde donde quedaste.")
        _emit(fmt, event, msg)
        return 0

    if org:
        f = state_dir / f"{org}.json"
        if f.exists() and ('"blocked": true' in f.read_text() or '"blocked_at": "' in f.read_text()):
            cli.write_state(org, blocked=False, blocked_at=None)
            _herdr_title(None)
    return 0


def cwd_direnv(stdin: str | None = None) -> int:
    """Hook CwdChanged (Claude Code): carga el .envrc aprobado de la nueva carpeta vía $CLAUDE_ENV_FILE."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    direnv = shutil.which("direnv")
    if not env_file or not direnv:
        return 0
    try:
        data = json.loads(stdin if stdin is not None else sys.stdin.read())
    except (ValueError, OSError):
        return 0
    new_cwd = data.get("new_cwd") or data.get("cwd") if isinstance(data, dict) else None
    if not new_cwd or not Path(new_cwd).is_dir():
        return 0
    try:
        out = subprocess.run([direnv, "export", "bash"], cwd=new_cwd, capture_output=True, text=True, timeout=10)
        if out.stdout:
            with open(env_file, "a") as fh:
                fh.write(out.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0
