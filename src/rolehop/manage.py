"""Mantenimiento: `rolehop refresh` (recargar permisos desde el portal), `remove` y `uninstall`.

Todo lo que modifica archivos lo comprueba antes de escribir nada, hace copia de seguridad y admite --dry-run.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from rolehop import cli

EDIT_NOTE = "  (orgs.toml se reescribe entero: los comentarios hechos a mano no se conservan; queda una copia de seguridad.)"


# --------------------------------------------------------------------------- utilidades
def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("✗ Hace falta confirmación: repite con --yes (o revisa antes con --dry-run).", file=sys.stderr)
        return False
    return input(f"{question} [s/N]: ").strip().lower().startswith(("s", "y"))


def _validate(orgs: list[dict]) -> None:
    """Valida un conjunto de orgs con las mismas reglas que `sync`, sin tocar orgs.toml."""
    text = cli.toml_dump(orgs)
    cli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".orgs-", suffix=".toml", dir=cli.CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        cli.load_orgs(Path(tmp))
    finally:
        os.unlink(tmp)


def _precheck_sync(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Calcula, antes de escribir nada, el ~/.aws/config que dejaría `sync` y comprueba que es aplicable y válido."""
    current = cli.AWS_CONFIG.read_text() if cli.AWS_CONFIG.exists() else ""
    _, _, conflicts = cli.plan_sync(orgs, current, prune_only=set(prune_only or []))  # ConfigError si no es válido
    problems = [f"[{org}] {c}" for org, items in conflicts.items() for c in items]
    if problems:
        raise cli.ConfigError(f"Hay secciones en {cli.AWS_CONFIG}, fuera de los bloques de rolehop, que chocan con "
                              f"perfiles nuevos: {', '.join(problems)}. Renómbralas o bórralas y repite.")


def _save(orgs: list[dict], prune_only: list[str] | None = None) -> None:
    """Valida y escribe orgs.toml solo si `sync` podrá aplicarlo después (nunca deja los dos archivos desalineados)."""
    _validate(orgs)
    _precheck_sync(orgs, prune_only)
    cli._write_orgs(orgs)


def _sync(prune_only: list[str] | None = None) -> int:
    return cli.cmd_sync(argparse.Namespace(dry_run=False, prune=False, quiet=True, prune_only=prune_only or []))


def _token_cache(session: str) -> Path:
    return cli.SSO_CACHE / (hashlib.sha1(session.encode(), usedforsecurity=False).hexdigest() + ".json")


def _forget_org_local(name: str, dry_run: bool) -> list[str]:
    """Borra el estado de rolehop y el token SSO en caché de una org (cierra su sesión en este equipo)."""
    msgs = []
    for path, label in ((cli.state_file(name), "estado"), (_token_cache(name), "token SSO en caché")):
        if path.exists():
            if not dry_run:
                path.unlink()
            msgs.append(f"{'[dry-run] borraría' if dry_run else '✓ borrado'} {label} de {name}: {path}")
    return msgs


def _drop_probe_if_missing(org: dict, changes: list[str]) -> None:
    pp = org.get("probe_profile")
    if pp and pp not in {r["profile"] for r in cli.catalog([org])}:
        del org["probe_profile"]
        changes.append(f"! probe_profile {pp} ya no existe: se quita (status usará default_role)")


def _profiles(org: dict) -> set[str]:
    return {r["profile"] for r in cli.catalog([org])}


# --------------------------------------------------------------------------- refresh
def plan_refresh(org: dict, inventory: list[dict], prune: bool) -> tuple[dict, list[str], set[str]]:
    """Compara una org con el inventario real del portal. Devuelve (org actualizada, cambios, perfiles quitados).

    No escribe nada. Lanza ConfigError si el resultado renombraría perfiles existentes, si --prune vaciaría la org
    o si el portal no devolvió cuentas con --prune.
    """
    name = org["name"]
    if prune and not inventory:
        raise cli.ConfigError(f"[{name}] el portal no devolvió ninguna cuenta: no aplico --prune "
                              f"(¿acceso caído temporalmente?). Sin --prune solo se agregan cuentas.")
    before = _profiles(org)
    org = copy.deepcopy(org)
    roles_map = org.setdefault("roles", {})
    # Roles escritos con el nombre exacto del permission set (sin alias): se siguen escribiendo igual,
    # porque crear un alias para ellos cambiaría el nombre de sus perfiles.
    direct = {r for a in org.get("accounts", []) for r in a["roles"] if r not in roles_map and r not in roles_map.values()}

    def ref(ps: str) -> str:
        return ps if ps in direct else cli._alias_for(ps, roles_map)

    live_by_id = {str(a["id"]): a for a in inventory}
    protected = {ar["source"] for ar in org.get("assume_roles", [])}  # "<cuenta>-<rol>" usados por assume_roles
    hint = "" if prune else " (usa --prune para quitarlo)"
    changes: list[str] = []
    removed: set[str] = set()
    kept: list[dict] = []
    for acc in org.get("accounts", []):
        pairs = cli.role_pairs(org, acc)
        live = live_by_id.get(str(acc["id"]))
        if live is None:
            locked = [(r, k) for r, (k, _) in zip(acc["roles"], pairs, strict=True) if f"{acc['name']}-{k}" in protected]
            if not prune:
                changes.append(f"! cuenta {acc['name']} ({acc['id']}): ya no aparece en el portal{hint}")
                kept.append(acc)
                continue
            gone = {f"{name}-{acc['name']}-{k}" for k, _ in pairs} - {f"{name}-{acc['name']}-{k}" for _, k in locked}
            removed |= gone
            if not locked:
                changes.append(f"- cuenta {acc['name']} ({acc['id']}): ya no tienes acceso")
                continue
            acc["roles"] = [r for r, _ in locked]
            keep = ", ".join(f"{acc['name']}-{k}" for _, k in locked)
            changes.append(f"! cuenta {acc['name']} ({acc['id']}): ya no aparece en el portal; "
                           f"se conserva solo {keep} (lo usa assume_roles)")
            kept.append(acc)
            continue
        live_ps = set(live["roles"])
        roles: list[str] = []
        for role, (key, ps) in zip(acc["roles"], pairs, strict=True):
            profile = f"{name}-{acc['name']}-{key}"
            if ps in live_ps:
                roles.append(role)
            elif prune and f"{acc['name']}-{key}" not in protected:
                removed.add(profile)
                changes.append(f"- rol {profile} ({ps}): revocado")
            else:
                roles.append(role)
                why = " (lo usa assume_roles)" if prune else hint
                changes.append(f"! rol {profile} ({ps}): revocado en el portal{why}")
        have = {ps for _, ps in pairs}
        for ps in live["roles"]:
            if ps not in have:
                r = ref(ps)
                roles.append(r)
                key = next(k for rr, (k, _) in zip(roles, cli.role_pairs(org, {"roles": roles}), strict=True) if rr == r)
                changes.append(f"+ rol {name}-{acc['name']}-{key} ({ps})")
        if not roles:
            changes.append(f"- cuenta {acc['name']} ({acc['id']}): sin roles")
            continue
        acc["roles"] = roles
        kept.append(acc)
    known = {str(a["id"]) for a in org.get("accounts", [])}
    used = {a["name"] for a in kept}
    for live in inventory:
        if str(live["id"]) in known or not live["roles"]:
            continue
        base = cli.short_account_name(live["name"], name)
        acc_name, n = base, 2
        while acc_name in used:
            acc_name, n = f"{base}-{n}", n + 1
        used.add(acc_name)
        acc = {"name": acc_name, "id": str(live["id"]), "roles": [ref(ps) for ps in live["roles"]]}
        kept.append(acc)
        keys = [k for k, _ in cli.role_pairs(org, acc)]
        changes.append(f"+ cuenta {acc_name} ({acc['id']}): {', '.join(keys)}")
    org["accounts"] = kept
    _drop_probe_if_missing(org, changes)
    lost = before - _profiles(org) - removed
    if lost:
        raise cli.ConfigError(f"[{name}] refresh cambiaría el nombre de perfiles existentes ({', '.join(sorted(lost))}); "
                              f"no se aplicó. Revisa [org.roles] en orgs.toml.")
    if not org["accounts"] and not org.get("assume_roles"):
        raise cli.ConfigError(f"[{name}] --prune dejaría la organización sin perfiles; no se aplicó. "
                              f"Si ya no la usas: rolehop remove {name}")
    return org, changes, removed


def _read_portal(org: dict) -> list[dict]:
    """Inventario del portal con el token en caché; si falla, renueva el token con un comando AWS y reintenta."""
    try:
        return cli.portal_inventory(org["name"], org["sso_region"])
    except cli.PortalError:
        cli.check_org(org)  # best effort: el AWS CLI renueva el token SSO en caché si la sesión sigue viva
        return cli.portal_inventory(org["name"], org["sso_region"])


def cmd_refresh(args) -> int:
    orgs = cli.load_orgs()
    targets = {o["name"] for o in cli.select(orgs, args.orgs)}
    if args.login:
        rc = cli.cmd_login(argparse.Namespace(orgs=sorted(targets), force=False, device=False))
        if rc != 0:
            print("✗ El login falló.", file=sys.stderr)
            return 1
    updated, failed, removals, any_change = [], [], set(), False
    for org in orgs:
        if org["name"] not in targets:
            updated.append(org)
            continue
        try:
            inventory = _read_portal(org)
            new_org, changes, removed = plan_refresh(org, inventory, args.prune)
        except cli.PortalError as exc:
            print(f"✗ {org['name']}: no pude leer el portal ({exc}). Ejecuta: awslogin {org['name']}  "
                  f"(o usa --login)", file=sys.stderr)
            failed.append(org["name"])
            updated.append(org)
            continue
        except cli.ConfigError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            failed.append(org["name"])
            updated.append(org)
            continue
        if changes:
            print(f"{org['name']}: {len(inventory)} cuenta(s) en el portal")
            for c in changes:
                print(f"  {c}")
        else:
            print(f"✓ {org['name']}: sin cambios ({len(inventory)} cuentas en el portal)")
        any_change = any_change or new_org != org
        removals |= removed
        updated.append(new_org)
    rc_failed = 1 if failed else 0
    if args.dry_run:
        if any_change:
            print("\n(dry-run: no se escribió nada. Repite sin --dry-run para aplicar.)")
        return rc_failed
    if any_change:
        if removals and not _confirm(f"Se quitarán {len(removals)} perfil(es). ¿Continuar?", args.yes):
            return 1
        try:
            _save(updated)
        except cli.ConfigError as exc:
            print(f"✗ No se escribió nada: {exc}", file=sys.stderr)
            return 2
        print(EDIT_NOTE)
    # Siempre: deja ~/.aws/config alineado con orgs.toml aunque esta vez no haya cambios.
    return _sync() or rc_failed


# --------------------------------------------------------------------------- remove
def _resolve(orgs: list[dict], target: str) -> tuple[str, dict, Any]:
    """('org'|'profile'|'assume'|'account', org, elemento). Error si el nombre no existe o es ambiguo."""
    matches: list[tuple[str, dict, Any]] = []
    for org in orgs:
        if target == org["name"]:
            matches.append(("org", org, None))
        for acc in org["accounts"]:
            if target == f"{org['name']}-{acc['name']}":
                matches.append(("account", org, acc))
            for role, (key, _) in zip(acc["roles"], cli.role_pairs(org, acc), strict=True):
                if target == f"{org['name']}-{acc['name']}-{key}":
                    matches.append(("profile", org, (acc, role, key)))
        for ar in org["assume_roles"]:
            if target == f"{org['name']}-{ar['name']}":
                matches.append(("assume", org, ar))
    if not matches:
        raise cli.ConfigError(f"No encuentro '{target}' entre tus organizaciones, cuentas ni perfiles. Mira `rolehop list`.")
    if len(matches) > 1:
        labels = {"org": "organización", "account": "cuenta", "profile": "perfil", "assume": "rol encadenado"}
        found = ", ".join(f"{labels[k]} de {o['name']}" for k, o, _ in matches)
        raise cli.ConfigError(f"'{target}' es ambiguo ({found}). Edita orgs.toml a mano para quitar el que quieras.")
    return matches[0]


def cmd_remove(args) -> int:
    orgs = cli.load_orgs()
    plan: list[str] = []
    removed_orgs: list[str] = []
    for target in args.targets:
        kind, org, item = _resolve(orgs, target)
        if kind == "org":
            orgs = [o for o in orgs if o["name"] != org["name"]]
            removed_orgs.append(org["name"])
            plan.append(f"- organización {org['name']} ({len(cli.catalog([org]))} perfil(es), su sso-session y su sesión local)")
            continue
        sources = {ar["source"] for ar in org["assume_roles"]}
        if kind == "assume":
            org["assume_roles"] = [a for a in org["assume_roles"] if a is not item]
            plan.append(f"- perfil {target} (rol encadenado)")
        elif kind == "account":
            own = {f"{item['name']}-{k}" for k, _ in cli.role_pairs(org, item)}
            used = sorted(own & sources)
            if used:
                raise cli.ConfigError(f"La cuenta {target} es origen de assume_roles ({', '.join(used)}): quítalos antes.")
            org["accounts"] = [a for a in org["accounts"] if a is not item]
            plan.append(f"- cuenta {target} ({item['id']}) y sus {len(item['roles'])} perfil(es)")
        else:
            acc, role, key = item
            if f"{acc['name']}-{key}" in sources:
                raise cli.ConfigError(f"El perfil {target} es origen de un assume_role: quita antes ese rol encadenado.")
            acc["roles"] = [r for r in acc["roles"] if r != role]
            plan.append(f"- perfil {target}")
            if not acc["roles"]:
                org["accounts"] = [a for a in org["accounts"] if a is not acc]
                plan.append(f"  (la cuenta {org['name']}-{acc['name']} se queda sin roles: se quita)")
        if not org["accounts"] and not org["assume_roles"]:
            raise cli.ConfigError(f"La organización {org['name']} quedaría vacía: usa `rolehop remove {org['name']}`.")
        notes: list[str] = []
        _drop_probe_if_missing(org, notes)
        plan += [f"  {n}" for n in notes]
    if not orgs:
        raise cli.ConfigError("No puedes quitar todas las organizaciones: para eso usa `rolehop uninstall`.")
    print("Se va a quitar:")
    for line in plan:
        print(f"  {line}")
    _validate(orgs)
    _precheck_sync(orgs, removed_orgs)
    if args.dry_run:
        for name in removed_orgs:
            for m in _forget_org_local(name, True):
                print(f"  {m}")
        print("\n(dry-run: no se escribió nada.)")
        return 0
    print(EDIT_NOTE)
    if not _confirm("¿Continuar?", args.yes):
        return 1
    _save(orgs, removed_orgs)
    rc = _sync(prune_only=removed_orgs)
    if rc == 0:
        for name in removed_orgs:
            for m in _forget_org_local(name, False):
                print(m)
    return rc


# --------------------------------------------------------------------------- uninstall
def _strip_managed_blocks(current: str) -> tuple[str, list[str]]:
    """~/.aws/config sin los bloques de rolehop (y awsorg). Solo toca los huecos que dejan los bloques."""
    free, blocks = cli.split_blocks(current)
    new = free
    for token in re.findall(r"\0ROLEHOP:[^\0]+\0\n", free):
        new = cli.drop_placeholder(new, token)
    configparser.RawConfigParser(strict=True, interpolation=None).read_string(new)
    return new, sorted(blocks)


def cmd_uninstall(args) -> int:
    from rolehop import agents, shellrc

    try:
        org_names = [o["name"] for o in cli.load_orgs()]
    except cli.ConfigError:
        org_names = []
    current = cli.AWS_CONFIG.read_text() if cli.AWS_CONFIG.exists() else ""
    aws_error = None
    try:
        new_aws, blocks = _strip_managed_blocks(current)
    except (cli.ConfigError, configparser.Error) as exc:
        new_aws, blocks, aws_error = current, [], str(exc)
    sessions = sorted(set(org_names) | set(blocks))
    home = Path.home()
    aws_backups = sorted(cli.AWS_CONFIG.parent.glob(f"{cli.AWS_CONFIG.name}.bak-*")) if cli.AWS_CONFIG.parent.exists() else []

    print("rolehop uninstall va a quitar:")
    if aws_error:
        print(f"  ! {cli.AWS_CONFIG}: no lo puedo limpiar ({aws_error}). Se omite, junto con tus sesiones SSO.")
    else:
        print(f"  - {len(blocks)} bloque(s) de perfiles en {cli.AWS_CONFIG}: {', '.join(blocks) or '(ninguno)'}")
    print("  - reglas y hooks de todos los agentes de IA")
    print("  - el bloque de rolehop en tu shell (PATH y direnv)")
    print(f"  - el estado de rolehop ({cli.STATE_DIR})")
    if not aws_error:
        print(f"  - los tokens SSO en caché de: {', '.join(sessions) or '(ninguna)'}")
    if args.purge:
        print(f"  - TAMBIÉN {cli.CONFIG_PATH}, sus copias de seguridad y la configuración antigua de awsorg")
    else:
        print(f"  (se conserva {cli.CONFIG_PATH}; usa --purge para borrarla)")
    if aws_backups:
        print(f"  (quedan {len(aws_backups)} copia(s) {cli.AWS_CONFIG.name}.bak-*: contienen tus perfiles; bórralas si quieres)")
    if args.dry_run:
        print("\n(dry-run: no se cambió nada.)")
        return 0
    if not _confirm("¿Desinstalar?", args.yes):
        return 1

    if not aws_error and blocks:
        cli.write_aws_config(new_aws)
        print(f"✓ {cli.AWS_CONFIG}: perfiles de rolehop quitados")
    ch = agents.Changes(False)
    for a in agents.AGENTS.values():
        agents.uninstall(a, home, ch)
    for line in ch.lines:
        print(line)
    for m in shellrc.remove_shell_integration():
        print(m)
    if not aws_error:
        for name in sessions:
            for m in _forget_org_local(name, False):
                print(m)
    if cli.STATE_DIR.is_dir():
        shutil.rmtree(cli.STATE_DIR)
        print(f"✓ borrado {cli.STATE_DIR}")
    if args.purge:
        for path in [cli.CONFIG_PATH, *sorted(cli.CONFIG_PATH.parent.glob(f"{cli.CONFIG_PATH.name}.bak-*"))]:
            if path.exists():
                path.unlink()
                print(f"✓ borrado {path}")
        for legacy in (cli.LEGACY_CONFIG_PATH.parent, cli.LEGACY_STATE_DIR):
            if legacy.is_dir():
                shutil.rmtree(legacy)
                print(f"✓ borrado {legacy} (awsorg)")
        try:
            cli.CONFIG_PATH.parent.rmdir()  # solo si quedó vacía
        except OSError:
            pass
    tool = "pipx uninstall rolehop" if "pipx" in sys.prefix else "uv tool uninstall rolehop"
    if aws_error:
        print(f"\n✗ Desinstalación incompleta: limpia a mano los bloques de rolehop de {cli.AWS_CONFIG} y repite.")
        return 1
    print(f"\nListo. Para quitar el comando: {tool}")
    return 0


def register(sub) -> None:
    s = sub.add_parser("refresh", help="recarga cuentas y roles desde el portal (si te cambiaron los permisos)")
    s.add_argument("orgs", nargs="*", help="organizaciones; por defecto todas")
    s.add_argument("--prune", action="store_true", help="quita también las cuentas y roles revocados (pide confirmación)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--login", action="store_true", help="inicia sesión antes si hace falta")
    s.add_argument("--yes", action="store_true", help="no pedir confirmación al quitar perfiles")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("remove", help="quita una organización, una cuenta (<org>-<cuenta>) o un perfil del equipo")
    s.add_argument("targets", nargs="+", metavar="org|perfil")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true", help="no pedir confirmación")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("uninstall", help="quita todo lo de rolehop del equipo (perfiles, agentes, shell, estado)")
    s.add_argument("--purge", action="store_true", help="borra también orgs.toml y la configuración antigua de awsorg")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true", help="no pedir confirmación")
    s.set_defaults(func=cmd_uninstall)
