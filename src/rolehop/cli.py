"""rolehop: perfiles AWS IAM Identity Center declarativos para N organizaciones.

Genera perfiles nativos `sso-session` en ~/.aws/config a partir de orgs.toml. Login y renovación los hace
el AWS CLI o los SDK: rolehop nunca escribe tokens ni claves.

Subcomandos:
  setup                        todo en uno: PATH, asistente si falta config, perfiles y agentes de IA
  init                         asistente: organizaciones, descubrimiento de cuentas y roles, orgs.toml
  sync [--dry-run] [--prune]   escribe los bloques gestionados en ~/.aws/config
  login [org ...] [--force]    aws sso login solo donde haga falta (también: awslogin)
  status [org ...] [--json]    estado de sesión por org
  list [--json] [--org ORG]    catálogo de perfiles disponibles
  run [--login] [--max 6h] [-- CMD]   comprueba la sesión antes de dejar trabajando a un agente
  agent list|install|uninstall|rules  instrucciones y hooks para agentes de IA
  envrc PERFIL [--state P]     imprime un .envrc para direnv
  discover ORG                 lista cuentas y roles reales de una org (requiere sesión)

Configuración: $ROLEHOP_CONFIG o ~/.config/rolehop/orgs.toml
Destino:       $AWS_CONFIG_FILE o ~/.aws/config
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 — mensaje claro si se ejecuta con un Python antiguo
    sys.exit("rolehop necesita Python 3.11 o superior (por tomllib).")
import tomllib  # noqa: E402

from rolehop import __version__ as VERSION  # noqa: E402
HOME = Path.home()
LEGACY_NAME = "awsorg"  # nombre anterior de la herramienta (hasta 1.2.0): se reconocen sus archivos y marcadores


def _env(name: str) -> str | None:
    """Variable ROLEHOP_<name>, o la antigua AWSORG_<name> si no está definida."""
    return os.environ.get(f"ROLEHOP_{name}") or os.environ.get(f"{LEGACY_NAME.upper()}_{name}") or None


CONFIG_PATH = Path(_env("CONFIG") or HOME / ".config/rolehop/orgs.toml")
LEGACY_CONFIG_PATH = HOME / f".config/{LEGACY_NAME}/orgs.toml"
AWS_CONFIG = Path(os.environ.get("AWS_CONFIG_FILE") or HOME / ".aws/config")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "rolehop"
LEGACY_STATE_DIR = STATE_DIR.with_name(LEGACY_NAME)
MARK_NAMES = f"(?:rolehop|{LEGACY_NAME})"  # marcadores de bloque aceptados al leer ~/.aws/config


def migrate_legacy() -> list[str]:
    """Copia orgs.toml y el estado de awsorg a las rutas de rolehop si aún no existen. No borra nada."""
    msgs = []
    if not _env("CONFIG") and not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.is_file():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
        msgs.append(f"✓ migrado {LEGACY_CONFIG_PATH} → {CONFIG_PATH} (el original se conserva)")
    if not STATE_DIR.exists() and LEGACY_STATE_DIR.is_dir():
        shutil.copytree(LEGACY_STATE_DIR, STATE_DIR)
        msgs.append(f"✓ migrado el estado de {LEGACY_STATE_DIR} → {STATE_DIR}")
    return msgs
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Mensajes reales de AWS CLI / botocore / aws-sdk-go-v2 cuando la sesión SSO terminó.
EXPIRED_RE = re.compile(
    r"Error when retrieving token from sso|The SSO session associated with this profile has expired"
    r"|UnauthorizedSSOToken|Token has expired and refresh failed|refresh cached SSO token failed"
    r"|InvalidGrantException|SSO session.{0,40}(has expired|is invalid)|Error loading SSO Token"
    r"|ExpiredToken")
# Valores que se escriben en ~/.aws/config: sin espacios, saltos de línea, corchetes ni comentarios.
VAL_RE = re.compile(r"^[^\s\[\]#;]+$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s\[\]#;]*)?$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ARN_RE = re.compile(r"^arn:aws[\w-]*:iam::\d{12}:role/[\w+=,.@/-]+$")


def begin_marker(org: str) -> str:
    return f"# >>> rolehop:{org} (gestionado por rolehop, no editar) >>>"


def end_marker(org: str) -> str:
    return f"# <<< rolehop:{org} <<<"


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def load_orgs(path: Path = CONFIG_PATH) -> list[dict]:
    if not path.exists():
        raise ConfigError(f"No existe {path}. Copia orgs.example.toml ahí y edítalo.")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} no es TOML válido: {exc}") from None
    orgs = data.get("org", [])
    if not orgs:
        raise ConfigError(f"{path} no define ningún [[org]].")
    seen = set()
    for org in orgs:
        if not isinstance(org, dict):
            raise ConfigError("Cada [[org]] debe ser una tabla.")
        for key in ("name", "start_url", "sso_region"):
            if not isinstance(org.get(key), str) or not org.get(key):
                raise ConfigError(f"Una org no tiene '{key}' (texto entre comillas).")
        name = org["name"]
        if not NAME_RE.match(name):
            raise ConfigError(f"Nombre de org inválido: {name!r} (minúsculas, números, - o _).")
        if name in seen:
            raise ConfigError(f"Org repetida: {name}")
        seen.add(name)
        org.setdefault("region", org["sso_region"])
        org.setdefault("login_flow", "device")
        org.setdefault("portal_session_hours", 8)
        org.setdefault("roles", {})
        org.setdefault("accounts", [])
        org.setdefault("assume_roles", [])
        if not URL_RE.match(org["start_url"]):
            raise ConfigError(f"[{name}] start_url inválida: {org['start_url']!r} (https://<algo>.awsapps.com/start)")
        for key in ("sso_region", "region"):
            if not isinstance(org[key], str) or not REGION_RE.match(org[key]):
                raise ConfigError(f"[{name}] {key} inválida: {org[key]!r} (p. ej. us-east-1)")
        if org["login_flow"] not in ("device", "pkce"):
            raise ConfigError(f"[{name}] login_flow debe ser \"device\" o \"pkce\".")
        if not isinstance(org["portal_session_hours"], (int, float)) or org["portal_session_hours"] <= 0:
            raise ConfigError(f"[{name}] portal_session_hours debe ser un número de horas.")
        if not isinstance(org["roles"], dict):
            raise ConfigError(f"[{name}] [org.roles] debe ser una tabla (alias = \"PermissionSet\").")
        for alias, ps in org["roles"].items():
            if not NAME_RE.match(alias) or not isinstance(ps, str) or not VAL_RE.match(ps):
                raise ConfigError(f"[{name}] rol inválido en [org.roles]: {alias} = {ps!r}")
        if not isinstance(org["accounts"], list) or not isinstance(org["assume_roles"], list):
            raise ConfigError(f"[{name}] accounts/assume_roles deben ser listas de tablas.")
        for acc in org["accounts"]:
            if not isinstance(acc, dict) or not acc.get("name") or not acc.get("id"):
                raise ConfigError(f"[{name}] cada cuenta necesita 'name' e 'id'.")
            if not isinstance(acc["name"], str) or not NAME_RE.match(acc["name"]):
                raise ConfigError(f"[{name}] nombre de cuenta inválido: {acc['name']!r} (texto: minúsculas, números, - o _)")
            if not re.fullmatch(r"\d{12}", str(acc["id"])):
                raise ConfigError(f"[{name}] id de cuenta inválido para {acc['name']}: {acc['id']!r} (12 dígitos)")
            acc["id"] = str(acc["id"])
            if not isinstance(acc.get("roles"), list) or not acc["roles"]:
                raise ConfigError(f"[{name}] roles de la cuenta {acc['name']} debe ser una lista, p. ej. [\"admin\"].")
            for role in acc["roles"]:
                if not isinstance(role, str) or not VAL_RE.match(role):
                    raise ConfigError(f"[{name}] rol inválido en la cuenta {acc['name']}: {role!r}")
            if "region" in acc and (not isinstance(acc["region"], str) or not REGION_RE.match(acc["region"])):
                raise ConfigError(f"[{name}] region inválida en la cuenta {acc['name']}: {acc['region']!r}")
        sources = {f"{a['name']}-{k}" for a in org["accounts"] for k, _ in role_pairs(org, a)}
        for ar in org["assume_roles"]:
            for k in ("name", "role_arn", "source"):
                if not isinstance(ar, dict) or not isinstance(ar.get(k), str) or not ar.get(k):
                    raise ConfigError(f"[{name}] cada [[org.assume_roles]] necesita '{k}'.")
            if not NAME_RE.match(ar["name"]):
                raise ConfigError(f"[{name}] nombre inválido en assume_roles: {ar['name']!r}")
            if not ARN_RE.match(ar["role_arn"]):
                raise ConfigError(f"[{name}] role_arn inválido: {ar['role_arn']!r}")
            if ar["source"] not in sources:
                raise ConfigError(f"[{name}] source inexistente en assume_roles: {ar['source']!r} "
                                  f"(válidos: {', '.join(sorted(sources))})")
            if "external_id" in ar and (not isinstance(ar["external_id"], str) or not VAL_RE.match(ar["external_id"])):
                raise ConfigError(f"[{name}] external_id inválido en assume_roles {ar['name']}")
            if "duration_seconds" in ar and not isinstance(ar["duration_seconds"], int):
                raise ConfigError(f"[{name}] duration_seconds debe ser un número entero")
    for org in orgs:
        pp = org.get("probe_profile")
        if pp and pp not in {r["profile"] for r in catalog([org])}:
            raise ConfigError(f"[{org['name']}] probe_profile {pp!r} no existe entre los perfiles de esa org.")
    from collections import Counter
    dups = sorted(p for p, c in Counter(r["profile"] for r in catalog(orgs)).items() if c > 1)
    if dups:
        raise ConfigError(f"Perfiles duplicados: {', '.join(dups)}. Revisa cuentas o roles repetidos en orgs.toml.")
    return orgs


def role_pairs(org: dict, acc: dict) -> list[tuple[str, str]]:
    """Devuelve [(clave_corta, nombre_permission_set)] para una cuenta."""
    pairs = []
    reverse = {ps: alias for alias, ps in org["roles"].items()}
    for role in acc["roles"]:
        if role in org["roles"]:
            pairs.append((role, org["roles"][role]))
        elif role in reverse:  # nombre directo que coincide con un alias → usar el alias
            pairs.append((reverse[role], role))
        else:  # nombre directo del permission set
            key = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
            pairs.append((key, role))
    return pairs


def catalog(orgs: list[dict]) -> list[dict]:
    rows = []
    for org in orgs:
        for acc in org["accounts"]:
            for key, ps in role_pairs(org, acc):
                rows.append({
                    "profile": f"{org['name']}-{acc['name']}-{key}",
                    "org": org["name"], "account": acc["name"], "account_id": acc["id"],
                    "role": key, "permission_set": ps, "type": "sso",
                    "region": acc.get("region", org["region"]),
                })
        for ar in org["assume_roles"]:
            rows.append({
                "profile": f"{org['name']}-{ar['name']}", "org": org["name"],
                "account": ar.get("account", ""), "account_id": ar["role_arn"].split(":")[4],
                "role": ar["role_arn"].split("/")[-1], "permission_set": "",
                "type": f"assume-role (desde {org['name']}-{ar['source']})",
                "region": ar.get("region", org["region"]),
            })
    return rows


def probe_profile(org: dict) -> str | None:
    if org.get("probe_profile"):
        return org["probe_profile"]
    default = org.get("default_role")
    if default:
        for acc in org["accounts"]:
            if default in [k for k, _ in role_pairs(org, acc)]:
                return f"{org['name']}-{acc['name']}-{default}"
    for acc in org["accounts"]:
        pairs = role_pairs(org, acc)
        if pairs:
            return f"{org['name']}-{acc['name']}-{pairs[0][0]}"
    return None


# --------------------------------------------------------------------------- render
def render_block(org: dict) -> str:
    n = org["name"]
    out = [begin_marker(n), f"[sso-session {n}]",
           f"sso_start_url = {org['start_url']}",
           f"sso_region = {org['sso_region']}",
           "sso_registration_scopes = sso:account:access", ""]
    for acc in org["accounts"]:
        for key, ps in role_pairs(org, acc):
            out += [f"[profile {n}-{acc['name']}-{key}]",
                    f"sso_session = {n}",
                    f"sso_account_id = {acc['id']}",
                    f"sso_role_name = {ps}",
                    f"region = {acc.get('region', org['region'])}", ""]
    for ar in org["assume_roles"]:
        src = f"{n}-{ar['source']}"
        out += [f"[profile {n}-{ar['name']}]",
                f"role_arn = {ar['role_arn']}",
                f"source_profile = {src}",
                f"role_session_name = rolehop-{n}-{ar['name']}"]
        if ar.get("external_id"):
            out.append(f"external_id = {ar['external_id']}")
        if ar.get("duration_seconds"):
            out.append(f"duration_seconds = {int(ar['duration_seconds'])}")
        out += [f"region = {ar.get('region', org['region'])}", ""]
    if out[-1] == "":
        out.pop()
    out.append(end_marker(n))
    return "\n".join(out)


def split_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Separa el archivo en texto libre (con marcadores de posición) y bloques gestionados."""
    blocks: dict[str, str] = {}
    pattern = re.compile(
        rf"^# >>> {MARK_NAMES}:(?P<org>[a-z0-9_-]+) .*?>>>\n(?P<body>.*?)^# <<< {MARK_NAMES}:(?P=org) <<<\n?",
        re.S | re.M)

    def repl(m: re.Match) -> str:
        if m["org"] in blocks:
            raise ConfigError(f"El bloque rolehop:{m['org']} aparece dos veces en {AWS_CONFIG}. "
                              f"Deja uno solo (haz copia antes) y repite.")
        blocks[m["org"]] = m.group(0).rstrip("\n")
        return f"\0ROLEHOP:{m['org']}\0\n"

    if "\0" in text:
        raise ConfigError(f"{AWS_CONFIG} contiene bytes nulos: revísalo o restaura una copia .bak.")
    free = pattern.sub(repl, text)
    leftover = re.findall(rf"^# (?:>>>|<<<) {MARK_NAMES}:([a-z0-9_-]+)", free, re.M)
    if leftover:
        raise ConfigError(f"Marcadores de rolehop incompletos o manipulados para: {', '.join(sorted(set(leftover)))} "
                          f"en {AWS_CONFIG}. Corrige o borra ese bloque a mano (haz copia antes).")
    return free, blocks


SECTION_RE = re.compile(r"^\[(?:profile\s+)?(?P<name>[^\]]+?)\]\s*$", re.M)
SSO_SESSION_RE = re.compile(r"^\[sso-session\s+(?P<name>[^\]]+?)\]\s*$", re.M)


def conflicts(free_text: str, org: dict) -> list[str]:
    """Secciones fuera de bloques gestionados que chocarían con las generadas."""
    clean = re.sub(r"\0ROLEHOP:[^\0]+\0", "", free_text)
    existing_profiles = {m["name"].strip() for m in SECTION_RE.finditer(clean)
                         if not m.group(0).startswith("[sso-session")}
    existing_sessions = {m["name"].strip() for m in SSO_SESSION_RE.finditer(clean)}
    wanted = {r["profile"] for r in catalog([org])}
    found = sorted(wanted & existing_profiles)
    if org["name"] in existing_sessions:
        found.insert(0, f"sso-session {org['name']}")
    return found


# --------------------------------------------------------------------------- sync
def cmd_sync(args) -> int:
    orgs = load_orgs()
    current = AWS_CONFIG.read_text() if AWS_CONFIG.exists() else ""
    free, blocks = split_blocks(current)
    problems = {}
    for org in orgs:
        c = conflicts(free, org)
        if c:
            problems[org["name"]] = c
    if problems:
        print("✗ Hay secciones en tu config, fuera de los bloques de rolehop, que chocan con perfiles generados:",
              file=sys.stderr)
        for org, items in problems.items():
            for it in items:
                print(f"    [{org}] {it}", file=sys.stderr)
        print("  Renómbralas o bórralas a mano (haz copia antes) y vuelve a ejecutar `rolehop sync`.",
              file=sys.stderr)
        return 2

    wanted = {o["name"]: render_block(o) for o in orgs}
    stale = [n for n in blocks if n not in wanted]
    new_free = free
    for name, body in wanted.items():
        token = f"\0ROLEHOP:{name}\0\n"
        if token in new_free:
            new_free = new_free.replace(token, body + "\n", 1)
        else:
            if new_free and not new_free.endswith("\n"):
                new_free += "\n"
            if new_free and not new_free.endswith("\n\n"):
                new_free += "\n"
            new_free += body + "\n"
    for name in stale:
        token = f"\0ROLEHOP:{name}\0\n"
        new_free = new_free.replace(token, "" if args.prune else blocks[name] + "\n", 1)
    if stale and not args.prune:
        print(f"! Bloques de orgs que ya no están en orgs.toml (se conservan; usa --prune para quitarlos): "
              f"{', '.join(stale)}")

    new_free = re.sub(r"\n{3,}", "\n\n", new_free)
    total = sum(len(catalog([o])) for o in orgs)
    if new_free == current:
        print(f"✓ {AWS_CONFIG} ya está al día ({total} perfiles).")
        return 0
    if "\0" in new_free:
        print("✗ Error interno: el resultado contiene marcadores sin resolver. No se escribió nada.", file=sys.stderr)
        return 2
    import configparser
    try:
        configparser.RawConfigParser(strict=True, interpolation=None).read_string(new_free)
    except configparser.Error as exc:
        print(f"✗ El resultado no sería un ~/.aws/config válido; no se escribió nada:\n  {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        diff = difflib.unified_diff(current.splitlines(True), new_free.splitlines(True),
                                    fromfile=str(AWS_CONFIG), tofile=f"{AWS_CONFIG} (nuevo)")
        sys.stdout.writelines(diff)
    if args.dry_run:
        print("\n(dry-run: no se escribió nada)")
        return 0
    target = AWS_CONFIG.resolve() if AWS_CONFIG.is_symlink() else AWS_CONFIG
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
        print(f"\n✓ Copia de seguridad: {backup}")
    tmp = target.with_name(target.name + ".rolehop-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(new_free)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    print(f"✓ {AWS_CONFIG} actualizado: {total} perfiles en {len(orgs)} org(s).")
    return 0


# --------------------------------------------------------------------------- status / login
def state_file(org: str) -> Path:
    return STATE_DIR / f"{org}.json"


def read_state(org: str) -> dict:
    try:
        return json.loads(state_file(org).read_text())
    except (OSError, ValueError):
        return {}


def write_state(org: str, **values) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = read_state(org) | values
    state_file(org).write_text(json.dumps(data))


def check_org(org: dict) -> dict:
    prof = probe_profile(org)
    res = {"org": org["name"], "probe_profile": prof, "ok": False, "expired": False,
           "account": None, "arn": None, "error": None}
    if not prof:
        res["error"] = "sin perfiles definidos"
        return res
    try:
        p = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", prof, "--output", "json"],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        res["error"] = "no se encontró el comando aws"
        return res
    except subprocess.TimeoutExpired:
        res["error"] = "tiempo agotado consultando STS"
        return res
    if p.returncode == 0:
        try:
            info = json.loads(p.stdout)
            res.update(ok=True, account=info.get("Account"), arn=info.get("Arn"))
        except ValueError:
            res["error"] = "respuesta inesperada de aws sts (no es JSON)"
    else:
        err = (p.stderr or p.stdout).strip().splitlines()
        msg = next((line for line in err if line.strip()), "error desconocido")
        res["error"] = msg[:200]
        res["expired"] = bool(EXPIRED_RE.search(p.stderr or ""))
    st = read_state(org["name"])
    if st.get("last_login"):
        try:
            elapsed = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(st["last_login"])
            left = dt.timedelta(hours=float(org["portal_session_hours"])) - elapsed
            res["last_login"] = st["last_login"]
            res["estimated_left_min"] = max(int(left.total_seconds() // 60), 0)
        except (TypeError, ValueError):
            pass
    res["blocked"] = bool(st.get("blocked"))
    if res["blocked"]:  # el hook vio la sesión expirada: no confiar en credenciales de rol en caché
        res["ok"] = False
        res["expired"] = True
        res["error"] = res["error"] or "sesión expirada (detectada en un comando reciente)"
    return res


def select(orgs: list[dict], names: list[str]) -> list[dict]:
    if not names:
        return orgs
    by = {o["name"]: o for o in orgs}
    missing = [n for n in names if n not in by]
    if missing:
        raise ConfigError(f"Org desconocida: {', '.join(missing)}. Disponibles: {', '.join(by)}")
    return [by[n] for n in names]


def fmt_left(minutes: int | None) -> str:
    if minutes is None:
        return "sin dato de login"
    return f"~{minutes // 60}h{minutes % 60:02d}m estimado"


def cmd_status(args) -> int:
    orgs = select(load_orgs(), args.orgs)
    results = [check_org(o) for o in orgs]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["ok"]:
                print(f"✓ {r['org']:<12} sesión activa · cuenta {r['account']} · "
                      f"{fmt_left(r.get('estimated_left_min'))} · probado con {r['probe_profile']}")
            else:
                hint = f"ejecuta: awslogin {r['org']}" if r["expired"] else r["error"]
                print(f"✗ {r['org']:<12} sin sesión · {hint}")
        if any(r["ok"] for r in results):
            print("  (Credenciales del rol en caché pueden dar ✓ aunque la sesión del portal haya terminado.)")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_login(args) -> int:
    orgs = select(load_orgs(), args.orgs)
    rc = 0
    if not shutil.which("aws"):
        raise ConfigError("no se encontró el comando aws (instala AWS CLI v2).")
    for org in orgs:
        if not args.force and not read_state(org["name"]).get("blocked"):
            r = check_org(org)
            if r["ok"]:
                print(f"✓ {org['name']}: ya tiene sesión ({fmt_left(r.get('estimated_left_min'))}). "
                      f"Usa --force para renovarla.")
                continue
        device = args.device or org["login_flow"] == "device"
        cmd = ["aws", "sso", "login", "--sso-session", org["name"]]
        if device:
            cmd.append("--use-device-code")
        print(f"→ {org['name']}: {' '.join(cmd)}")
        p = subprocess.run(cmd)
        if p.returncode == 0:
            write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
            r = check_org(org)
            print(f"✓ {org['name']}: sesión iniciada" + (f" · cuenta {r['account']}" if r["ok"] else ""))
        else:
            print(f"✗ {org['name']}: el login falló (código {p.returncode}).", file=sys.stderr)
            rc = 1
    return rc


# --------------------------------------------------------------------------- list / envrc / discover
def cmd_list(args) -> int:
    orgs = load_orgs()
    if args.org:
        orgs = select(orgs, [args.org])
    rows = catalog(orgs)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No hay perfiles definidos.")
        return 0
    w = max(len(r["profile"]) for r in rows)
    wo = max([3] + [len(r["org"]) for r in rows])
    wa = max([6] + [len(r["account"]) for r in rows])
    print(f"{'PERFIL':<{w}}  {'ORG':<{wo}}  {'CUENTA':<{wa}}  {'ID':<12}  ROL")
    for r in rows:
        role = r["permission_set"] or r["role"]
        extra = "" if r["type"] == "sso" else f"  [{r['type']}]"
        print(f"{r['profile']:<{w}}  {r['org']:<{wo}}  {r['account']:<{wa}}  {r['account_id']:<12}  {role}{extra}")
    return 0


def cmd_envrc(args) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.state_var):
        print(f"✗ Nombre de variable inválido: {args.state_var}", file=sys.stderr)
        return 2
    names = {r["profile"] for r in catalog(load_orgs())}
    for p in [args.profile] + ([args.state] if args.state else []):
        if p not in names:
            print(f"✗ Perfil desconocido: {p}. Mira `rolehop list`.", file=sys.stderr)
            return 2
    print("# .envrc generado por rolehop: perfil AWS por defecto de este repo.")
    print("# La IA puede usar otro perfil anteponiendo AWS_PROFILE=<perfil> a un comando.")
    print(f"export AWS_PROFILE={args.profile}")
    if args.state:
        print(f"export {args.state_var}={args.state}")
    print("unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN")
    return 0


# --------------------------------------------------------------------------- portal SSO (stdlib)
SSO_CACHE = Path(_env("SSO_CACHE") or HOME / ".aws/sso/cache")


class PortalError(Exception):
    pass


def _cached_token(session_name: str) -> str:
    """Token de acceso SSO del caché del AWS CLI para un sso-session. Solo en memoria; nunca se imprime."""
    import hashlib
    path = SSO_CACHE / (hashlib.sha1(session_name.encode(), usedforsecurity=False).hexdigest() + ".json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise PortalError(f"no hay sesión iniciada para '{session_name}' (ejecuta awslogin {session_name})") from None
    token, expires = data.get("accessToken"), data.get("expiresAt", "")
    if not token:
        raise PortalError(f"caché SSO sin token para '{session_name}'")
    try:
        exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00").replace("UTC", "+00:00"))
        if exp <= dt.datetime.now(dt.UTC):
            raise PortalError(f"el token de '{session_name}' expiró (ejecuta awslogin {session_name})")
    except ValueError:
        pass
    return token


def _portal_get(region: str, path: str, token: str, params: dict) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request
    base = _env("PORTAL_ENDPOINT") or f"https://portal.sso.{region}.amazonaws.com"
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-amz-sso_bearer_token": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (URL fija de AWS)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PortalError("el portal rechazó el token (sesión expirada): ejecuta awslogin") from None
        raise PortalError(f"error HTTP {exc.code} del portal SSO") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PortalError(f"no se pudo contactar el portal SSO: {exc}") from None


def portal_inventory(session_name: str, region: str) -> list[dict]:
    """[{id, name, roles:[...]}] de todas las cuentas y roles visibles para el usuario."""
    token = _cached_token(session_name)
    accounts, nxt = [], None
    while True:
        params = {"max_result": 100} | ({"next_token": nxt} if nxt else {})
        page = _portal_get(region, "/assignment/accounts", token, params)
        accounts += page.get("accountList", [])
        nxt = page.get("nextToken")
        if not nxt:
            break
    out = []
    for acc in sorted(accounts, key=lambda a: a.get("accountName", "")):
        roles, nxt = [], None
        while True:
            params = {"account_id": acc["accountId"], "max_result": 100} | ({"next_token": nxt} if nxt else {})
            page = _portal_get(region, "/assignment/roles", token, params)
            roles += [r["roleName"] for r in page.get("roleList", [])]
            nxt = page.get("nextToken")
            if not nxt:
                break
        out.append({"id": acc["accountId"], "name": acc.get("accountName", acc["accountId"]), "roles": sorted(roles)})
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "cuenta"


def role_alias(ps_name: str) -> str:
    low = ps_name.lower()
    if low in ("administrator", "administratoraccess", "admin", "adminaccess"):
        return "admin"
    if low.startswith("poweruser"):
        return "power"
    if low in ("readonly", "readonlyaccess", "viewonly", "viewonlyaccess"):
        return "ro"
    return slug(ps_name)


def short_account_name(account_name: str, org: str) -> str:
    s = slug(account_name)
    for pre in (f"{org}-", f"{org}_"):
        if s.startswith(pre) and len(s) > len(pre):
            return s[len(pre):]
    return s


def cmd_discover(args) -> int:
    org = select(load_orgs(), [args.org])[0]
    try:
        inv = portal_inventory(org["name"], org["sso_region"])
    except PortalError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"# Cuentas y roles visibles en {org['name']} ({len(inv)}). Pégalo en orgs.toml y ajusta los nombres.")
    for acc in inv:
        print(f'[[org.accounts]]\nname = "{short_account_name(acc["name"], org["name"])}"   # {acc["name"]}\n'
              f'id = "{acc["id"]}"\nroles = {json.dumps(acc["roles"])}\n')
    return 0


# --------------------------------------------------------------------------- asistente (init)
def _ask(prompt: str, default: str | None = None, validate=None, error: str = "valor inválido") -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise ConfigError("entrada cerrada: ejecuta el asistente en una terminal interactiva") from None
        if not val and default is not None:
            val = default
        if val and (validate is None or validate(val)):
            return val
        print(f"    ✗ {error}")


def _yes(prompt: str, default: bool = True) -> bool:
    d = "S/n" if default else "s/N"
    try:
        val = input(f"  {prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    return default if not val else val.startswith(("s", "y"))


def toml_dump(orgs: list[dict]) -> str:
    q = json.dumps
    lines = ["# orgs.toml — organizaciones de AWS Identity Center para rolehop.",
             "# Generado/actualizado por `rolehop init`. Puedes editarlo a mano; después ejecuta `rolehop sync`.",
             "# Documentación: https://github.com/escobarjsa/rolehop", ""]
    for org in orgs:
        lines += ["[[org]]", f"name = {q(org['name'])}", f"start_url = {q(org['start_url'])}",
                  f"sso_region = {q(org['sso_region'])}", f"region = {q(org.get('region', org['sso_region']))}",
                  f"login_flow = {q(org.get('login_flow', 'device'))}",
                  f"portal_session_hours = {org.get('portal_session_hours', 8)}"]
        if org.get("default_role"):
            lines.append(f"default_role = {q(org['default_role'])}")
        if org.get("probe_profile"):
            lines.append(f"probe_profile = {q(org['probe_profile'])}")
        lines += ["", "[org.roles]"] + [f"{k} = {q(v)}" for k, v in org.get("roles", {}).items()] + [""]
        for acc in org.get("accounts", []):
            lines += ["[[org.accounts]]", f"name = {q(acc['name'])}", f"id = {q(str(acc['id']))}",
                      f"roles = {q(list(acc['roles']))}"]
            if acc.get("region"):
                lines.append(f"region = {q(acc['region'])}")
            lines.append("")
        for ar in org.get("assume_roles", []):
            lines.append("[[org.assume_roles]]")
            for k, v in ar.items():
                lines.append(f"{k} = {v if isinstance(v, int) else q(str(v))}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_orgs(orgs: list[dict]) -> None:
    text = toml_dump(orgs)
    tomllib.loads(text)  # validación: nunca escribir un TOML roto
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"  ✓ copia de seguridad: {backup}")
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    print(f"  ✓ guardado {CONFIG_PATH}")


def _login_for_discovery(org: dict) -> bool:
    """Login SSO usando un config temporal con solo el sso-session (no toca ~/.aws/config)."""
    import tempfile
    block = (f"[sso-session {org['name']}]\nsso_start_url = {org['start_url']}\n"
             f"sso_region = {org['sso_region']}\nsso_registration_scopes = sso:account:access\n")
    with tempfile.NamedTemporaryFile("w", suffix=".aws-config", delete=False) as fh:
        fh.write(block)
        tmp = fh.name
    try:
        cmd = ["aws", "sso", "login", "--sso-session", org["name"]]
        if org.get("login_flow", "device") == "device":
            cmd.append("--use-device-code")
        print(f"  → {' '.join(cmd)}  (aprueba en el navegador)")
        rc = subprocess.run(cmd, env=os.environ | {"AWS_CONFIG_FILE": tmp}).returncode
    finally:
        os.unlink(tmp)
    if rc == 0:
        write_state(org["name"], last_login=dt.datetime.now(dt.UTC).isoformat(), blocked=False)
    return rc == 0


def _accounts_from_inventory(org_name: str, inv: list[dict], roles_map: dict, ask_names: bool) -> list[dict]:
    accounts, used = [], set()
    for acc in inv:
        aliases = []
        for ps in acc["roles"]:
            alias = next((k for k, v in roles_map.items() if v == ps), None)
            if alias is None:
                alias = role_alias(ps)
                while alias in roles_map and roles_map[alias] != ps:
                    alias = slug(ps)
                    if alias in roles_map:
                        alias = f"{alias}-{len(roles_map)}"
                roles_map[alias] = ps
            aliases.append(alias)
        name = short_account_name(acc["name"], org_name)
        if ask_names:
            name = _ask(f"Nombre corto para '{acc['name']}' ({acc['id']})", name, NAME_RE.match,
                        "minúsculas, números, - o _")
        base, n = name, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        accounts.append({"name": name, "id": acc["id"], "roles": aliases})
    return accounts


def _manual_accounts(roles_map: dict) -> list[dict]:
    print("  Escribe tus cuentas (las ves en el portal: nombre, ID de 12 dígitos y roles debajo de cada cuenta).")
    accounts = []
    while True:
        name = _ask("Nombre corto de la cuenta (ej. devsecops)", None, NAME_RE.match, "minúsculas, números, - o _")
        acc_id = _ask("ID de la cuenta (12 dígitos)", None, lambda v: re.fullmatch(r"\d{12}", v), "deben ser 12 dígitos")
        roles_txt = _ask("Roles separados por coma, tal como salen en el portal (ej. Administrator, ReadOnly)")
        aliases = []
        for ps in [r.strip() for r in roles_txt.split(",") if r.strip()]:
            alias = next((k for k, v in roles_map.items() if v == ps), None) or role_alias(ps)
            roles_map.setdefault(alias, ps)
            aliases.append(alias)
        accounts.append({"name": name, "id": acc_id, "roles": aliases})
        if not _yes("¿Agregar otra cuenta?", False):
            return accounts


def _print_accounts(org_name: str, accounts: list[dict], roles_map: dict) -> None:
    for acc in accounts:
        roles = ", ".join(f"{a} ({roles_map.get(a, a)})" for a in acc["roles"])
        print(f"    {org_name}-{acc['name']:<20} {acc['id']}  {roles}")


def _wizard_new_org(existing: list[dict]) -> dict:
    taken = {o["name"] for o in existing}
    print("\n  Nueva organización. Datos en el portal de acceso de AWS (la URL de la barra del navegador).")
    name = _ask("Nombre corto de la organización (ej. empresa, personal)", None,
                lambda v: NAME_RE.match(v) and v not in taken, "minúsculas/números/-/_ y que no exista ya")
    url = _ask("URL del portal (https://xxxx.awsapps.com/start)", None,
               lambda v: re.match(r"^https://[^\s/]+(/start)?/?(#.*)?$", v), "debe ser https://<algo>.awsapps.com/start")
    url = re.sub(r"[/#].*$", "", url.split("://", 1)[1])
    url = f"https://{url}/start"
    sso_region = _ask("Región de Identity Center", "us-east-1", lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v),
                      "formato de región, p. ej. us-east-1")
    region = _ask("Región por defecto para los perfiles", sso_region,
                  lambda v: re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", v), "formato de región")
    hours = _ask("¿Cuántas horas dura la sesión del portal? (si no lo sabes, 8)", "8",
                 lambda v: re.fullmatch(r"\d{1,4}(\.\d+)?", v), "un número de horas")
    org = {"name": name, "start_url": url, "sso_region": sso_region, "region": region, "login_flow": "device",
           "portal_session_hours": float(hours) if "." in hours else int(hours), "roles": {}, "accounts": []}
    if _yes("¿Inicio sesión ahora y descubro tus cuentas y roles automáticamente? (recomendado)"):
        try:
            if not _login_for_discovery(org):
                raise PortalError("el login no se completó")
            inv = portal_inventory(name, sso_region)
            if not inv:
                raise PortalError("el portal no devolvió cuentas")
            org["accounts"] = _accounts_from_inventory(name, inv, org["roles"], ask_names=False)
            print(f"\n  Encontré {len(inv)} cuenta(s):")
            _print_accounts(name, org["accounts"], org["roles"])
            if not _yes("¿Uso estos nombres cortos para los perfiles?"):
                org["roles"] = {}
                org["accounts"] = _accounts_from_inventory(name, inv, org["roles"], ask_names=True)
        except PortalError as exc:
            print(f"  ! No pude descubrir automáticamente ({exc}). Vamos a escribirlas a mano.")
            org["accounts"] = _manual_accounts(org["roles"])
    else:
        org["accounts"] = _manual_accounts(org["roles"])
    aliases = sorted({a for acc in org["accounts"] for a in acc["roles"]})
    org["default_role"] = "admin" if "admin" in aliases else (aliases[0] if aliases else "")
    return org


def _wizard_refresh_org(org: dict) -> None:
    print(f"\n  Actualizar cuentas y roles de '{org['name']}' desde el portal.")
    if not _login_for_discovery(org):
        raise ConfigError("el login no se completó")
    try:
        inv = portal_inventory(org["name"], org["sso_region"])
    except PortalError as exc:
        raise ConfigError(str(exc)) from None
    roles_map = org.setdefault("roles", {})
    by_id = {str(a["id"]): a for a in org.setdefault("accounts", [])}
    added, changed = [], []
    new_inv = [a for a in inv if a["id"] not in by_id]
    for acc in inv:
        if acc["id"] in by_id:
            cur = by_id[acc["id"]]
            fresh = _accounts_from_inventory(org["name"], [acc], roles_map, ask_names=False)[0]["roles"]
            extra = [r for r in fresh if r not in cur["roles"]]
            if extra:
                cur["roles"] = list(cur["roles"]) + extra
                changed.append(f"{cur['name']}: +{', '.join(extra)}")
    if new_inv:
        names_used = {a["name"] for a in org["accounts"]}
        for acc in _accounts_from_inventory(org["name"], new_inv, roles_map, ask_names=False):
            base, n = acc["name"], 2
            while acc["name"] in names_used:
                acc["name"], n = f"{base}-{n}", n + 1
            names_used.add(acc["name"])
            org["accounts"].append(acc)
            added.append(f"{acc['name']} ({acc['id']})")
    if not added and not changed:
        print("  ✓ No hay cambios: tu orgs.toml ya tiene todas las cuentas y roles.")
    for a in added:
        print(f"    + cuenta {a}")
    for c in changed:
        print(f"    + roles {c}")


def cmd_init(args) -> int:
    print("\n=== Asistente de rolehop: organizaciones de AWS Identity Center ===")
    orgs: list[dict] = []
    if CONFIG_PATH.exists():
        try:
            orgs = load_orgs()
        except ConfigError as exc:
            print(f"  ✗ Tu {CONFIG_PATH} tiene un problema: {exc}\n  Corrígelo o muévelo y vuelve a ejecutar.")
            return 2
        print(f"  Ya tienes {CONFIG_PATH} con: {', '.join(o['name'] for o in orgs)}")
        print("  (Si lo editaste a mano, los comentarios se perderán al guardar; se hace copia de seguridad.)")
    changed = False
    while True:
        if orgs:
            print("\n  ¿Qué quieres hacer?\n    1) Agregar otra organización\n"
                  "    2) Actualizar cuentas/roles de una organización (desde el portal)\n    3) Terminar")
            choice = _ask("Opción", "3", lambda v: v in ("1", "2", "3"), "elige 1, 2 o 3")
        else:
            choice = "1"
        if choice == "3":
            break
        if choice == "2":
            name = _ask("¿Qué organización?", orgs[0]["name"], lambda v: v in {o["name"] for o in orgs},
                        f"elige una de: {', '.join(o['name'] for o in orgs)}")
            _wizard_refresh_org(next(o for o in orgs if o["name"] == name))
            changed = True
            continue
        org = _wizard_new_org(orgs)
        if not org["accounts"]:
            print("  ! Sin cuentas: no agrego esta organización.")
        else:
            orgs.append(org)
            changed = True
        if not _yes("¿Agregar otra organización?", False):
            break
    if not changed:
        print("  Sin cambios.")
        return 0
    _write_orgs(orgs)
    load_orgs()  # validación final con las mismas reglas que sync
    print("\n  Siguiente paso: `rolehop sync --dry-run` y luego `rolehop sync` (o el instalador lo hace por ti).")
    return 0


def cmd_mark(args) -> int:
    """Uso interno de los hooks: marca o limpia el estado 'sesión expirada' de una org."""
    if not NAME_RE.match(args.org):
        return 0
    try:
        names = {o["name"] for o in load_orgs()}
    except ConfigError:
        names = set()
    if names and args.org not in names:
        return 0
    write_state(args.org, blocked=(args.state == "blocked"),
                blocked_at=dt.datetime.now(dt.UTC).isoformat() if args.state == "blocked" else None)
    return 0


def org_of_profile(profile: str) -> str | None:
    """Devuelve la org a la que pertenece un nombre de perfil, según orgs.toml."""
    try:
        for row in catalog(load_orgs()):
            if row["profile"] == profile:
                return row["org"]
    except ConfigError:
        return None
    return None


def cmd_which_org(args) -> int:
    org = org_of_profile(args.profile)
    if org:
        print(org)
        return 0
    return 1


# --------------------------------------------------------------------------- run / hook / agent / setup
def cmd_run(args) -> int:
    from rolehop import runner
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return runner.run(args.org or [], args.max, args.login, args.yes, cmd)


def cmd_hook(args) -> int:
    from rolehop import hooks
    try:
        if args.kind == "post-tool":
            return hooks.post_tool(args.format)
        return hooks.cwd_direnv()
    except Exception:  # noqa: BLE001 — un hook nunca debe romper la sesión del agente
        return 0


def cmd_agent(args) -> int:
    from rolehop import agents
    home = Path.home()
    if args.action == "rules":
        print(agents.rules_text())
        return 0
    if args.action == "list":
        rows = [agents.status(a, home) for a in agents.AGENTS.values()]
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print(f"{'AGENTE':<10} {'DETECTADO':<10} {'INSTALADO':<10} {'HOOKS':<8} INSTRUCCIONES")
        for r in rows:
            print(f"{r['agent']:<10} {'sí' if r['detected'] else '-':<10} {'sí' if r['installed'] else '-':<10} "
                  f"{r['hooks']:<8} {r['rules']}")
        return 0
    try:
        targets = agents.resolve(args.names, args.all, home)
    except KeyError as exc:
        print(f"✗ {exc.args[0]}", file=sys.stderr)
        return 2
    if not targets:
        print("No detecté agentes de IA instalados. Indica cuáles: rolehop agent install claude codex …")
        return 1
    ch = agents.Changes(args.dry_run)
    for a in targets:
        if args.action == "install":
            agents.install(a, home, ch, with_hooks=not args.no_hooks)
        else:
            agents.uninstall(a, home, ch)
    for line in ch.lines:
        print(f"  {line}")
    if args.action == "install" and not args.dry_run:
        print("  Reinicia tus agentes para que carguen las instrucciones nuevas.")
    return 1 if any(line.startswith("✗") for line in ch.lines) else 0


def cmd_setup(args) -> int:
    from rolehop import shellrc
    print("==> 1. Shell (PATH y direnv)")
    for m in shellrc.ensure_shell_integration(dry_run=args.dry_run):
        print(f"  {m}")
    print("==> 2. Organizaciones")
    if not CONFIG_PATH.exists():
        if args.dry_run or not sys.stdin.isatty():
            print(f"  ! No existe {CONFIG_PATH}. Ejecuta `rolehop init` (asistente) y repite `rolehop setup`.")
            return 0 if args.dry_run else 1
        rc = cmd_init(args)
        if rc != 0 or not CONFIG_PATH.exists():
            return rc or 1
    else:
        print(f"  ✓ uso {CONFIG_PATH}")
    print("==> 3. Perfiles en ~/.aws/config")
    rc = cmd_sync(argparse.Namespace(dry_run=args.dry_run, prune=False, quiet=True))
    if rc != 0:
        return rc
    print("==> 4. Agentes de IA")
    if args.no_agents:
        print("  (omitido: --no-agents)")
    else:
        rc = cmd_agent(argparse.Namespace(action="install", names=args.agents or [], all=False,
                                          dry_run=args.dry_run, no_hooks=False, json=False))
        if rc == 1 and not args.agents:
            print("  (sin agentes detectados; instala luego con: rolehop agent install <agente>)")
    print("==> Listo")
    print("  Siguiente: awslogin  ·  rolehop status  ·  dile a tu agente «usa los perfiles y roles de AWS que necesites»")
    return 0


def awslogin_main() -> int:
    return main(["login", *sys.argv[1:]])


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rolehop", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"rolehop {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{setup,init,sync,login,status,list,run,agent,envrc,discover}")

    s = sub.add_parser("sync", help="escribe los perfiles en ~/.aws/config")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--prune", action="store_true", help="quita bloques de orgs que ya no existen")
    s.add_argument("--quiet", action="store_true", help="no muestra el diff")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("login", help="aws sso login solo donde haga falta")
    s.add_argument("orgs", nargs="*")
    s.add_argument("--device", action="store_true", help="forzar código de dispositivo")
    s.add_argument("--force", action="store_true", help="hacer login aunque haya sesión")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("status", help="estado de sesión por org")
    s.add_argument("orgs", nargs="*")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="catálogo de perfiles")
    s.add_argument("--json", action="store_true")
    s.add_argument("--org")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("envrc", help="imprime un .envrc para direnv")
    s.add_argument("profile")
    s.add_argument("--state", help="perfil para el estado remoto de Terraform/Terragrunt")
    s.add_argument("--state-var", default="AWS_STATE_PROFILE",
                   help="variable donde va el perfil del estado (por defecto AWS_STATE_PROFILE)")
    s.set_defaults(func=cmd_envrc)

    s = sub.add_parser("init", help="asistente: te pregunta tus organizaciones y crea orgs.toml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("discover", help="lista cuentas y roles reales de una org")
    s.add_argument("org")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("setup", help="todo en uno: PATH, asistente si falta config, perfiles y agentes de IA")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-agents", action="store_true", help="no instalar instrucciones/hooks en agentes")
    s.add_argument("--agent", dest="agents", action="append", help="agente concreto (repetible); por defecto, los detectados")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run", help="comprueba/renueva la sesión antes de dejar trabajando a un agente")
    s.add_argument("--org", action="append", help="org a comprobar (repetible); por defecto todas")
    s.add_argument("--max", default="4h", help="duración del trabajo: 90m, 3h, 1h30m (por defecto 4h)")
    s.add_argument("--login", action="store_true", help="renueva antes la sesión (ventana completa desde ahora)")
    s.add_argument("--yes", action="store_true", help="arranca aunque no se pueda estimar el tiempo restante")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- comando a ejecutar (opcional)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("agent", help="instrucciones y hooks para agentes de IA (claude, codex, kiro, copilot…)")
    s.add_argument("action", choices=["list", "install", "uninstall", "rules"])
    s.add_argument("names", nargs="*", help="agentes; por defecto los detectados")
    s.add_argument("--all", action="store_true", help="todos los agentes soportados")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-hooks", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("hook")
    s.add_argument("kind", choices=["post-tool", "cwd"])
    s.add_argument("--format", default="claude", choices=["claude", "codex", "copilot", "gemini", "qwen", "text"])
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("_mark")
    s.add_argument("org")
    s.add_argument("state", choices=["blocked", "clear"])
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("_which-org")
    s.add_argument("profile")
    s.set_defaults(func=cmd_which_org)

    args = ap.parse_args(argv)
    if args.cmd not in ("hook", "_mark", "_which-org") and not getattr(args, "dry_run", False):
        try:
            for msg in migrate_legacy():
                print(msg, file=sys.stderr)
        except OSError as exc:
            print(f"! No pude migrar la configuración de {LEGACY_NAME}: {exc}", file=sys.stderr)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
