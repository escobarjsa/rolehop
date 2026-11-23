"""Translation catalogs stay complete and consistent with the messages in the code."""

from __future__ import annotations

import ast
import json
import string
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "rolehop"
LOCALES = SRC / "locales"
LANGS = ("es", "pt")


def msgids() -> dict[str, str]:
    """{english message: "file:line"} for every _("...") call in the package."""
    found: dict[str, str] = {}
    for py in sorted(SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_" and node.args:
                arg = node.args[0]
                where = f"{py.name}:{node.lineno}"
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                    f"{where}: _() needs a literal string (use named placeholders, not an f-string)")
                found.setdefault(arg.value, where)
    return found


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def load(lang: str) -> tuple[dict[str, str], list[str]]:
    merged: dict[str, str] = {}
    dupes: list[str] = []
    for f in sorted((LOCALES / lang).glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in merged and merged[k] != v:
                dupes.append(f"{f.name}: {k!r}")
            merged[k] = v
    return merged, dupes


def test_there_are_messages_to_translate():
    assert len(msgids()) > 20


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_is_complete_and_consistent(lang):
    ids = msgids()
    cat, dupes = load(lang)
    missing = sorted(f"{where}: {m!r}" for m, where in ids.items() if m not in cat)
    assert not missing, f"{len(missing)} message(s) missing in locales/{lang}:\n" + "\n".join(missing[:40])
    assert not dupes, f"conflicting duplicate keys in locales/{lang}: {dupes}"
    bad = [m for m in ids if fields(cat[m]) != fields(m)]
    assert not bad, f"placeholders differ in locales/{lang}: {bad[:10]}"
    empty = [m for m in ids if not cat[m].strip()]
    assert not empty, f"empty translations in locales/{lang}: {empty[:10]}"


@pytest.mark.parametrize("lang", LANGS)
def test_no_stale_entries(lang):
    ids = msgids()
    cat, _ = load(lang)
    stale = sorted(k for k in cat if k not in ids)
    assert not stale, f"{len(stale)} entries in locales/{lang} no longer used:\n" + "\n".join(map(repr, stale[:20]))


def test_commands_keep_english_in_translations():
    """Commands, flags and file names must not be translated (users copy them)."""
    import re
    ids = msgids()
    for lang in LANGS:
        cat, _ = load(lang)
        for m in ids:
            for token in re.findall(r"`[^`]+`|rolehop [a-z]+|--[a-z-]+|ROLEHOP_[A-Z_]+|NO_COLOR", m):
                assert token in cat[m], f"locales/{lang}: {token!r} changed in translation of {m!r}"
