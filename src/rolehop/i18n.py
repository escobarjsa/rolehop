"""Translations for user-facing messages (English, Spanish, Portuguese). Standard library only.

Messages are written in English in the code and wrapped with ``_()``; the English text is the message id.
Catalogs live in ``rolehop/locales/<lang>/*.json`` (one file per module, merged at load time) and map the
English text to the translation. Missing entries fall back to English, so a partial catalog never breaks
the tool. Placeholders are named (``{org}``) and filled with ``str.format`` after translation.

What is NOT translated, on purpose: machine-readable output (``--json``), the rules installed into AI agents
and the context hooks inject into them (agents get English and reply in the user's language), managed-block
markers, and ``--verbose`` debug lines.

Language resolution, first match wins:
  1. ``--lang`` on the command line
  2. ``ROLEHOP_LANG`` environment variable
  3. ``language`` in ``settings.toml`` (set with ``rolehop language <code>``)
  4. the system locale (``LC_ALL``, ``LC_MESSAGES``, ``LANG``)
  5. English
"""

from __future__ import annotations

import json
import os
import tomllib
from functools import cache
from importlib import resources

from rolehop import paths

SUPPORTED = ("en", "es", "pt")
NAMES = {"en": "English", "es": "Español", "pt": "Português"}

_explicit: str | None = None  # set by --lang


def normalize(code: str | None) -> str | None:
    """Map a locale or language code ("es_CO.UTF-8", "pt-BR", "EN") to a supported language, or None."""
    if not code:
        return None
    lang = code.strip().replace("-", "_").split(".")[0].split("_")[0].lower()
    return lang if lang in SUPPORTED else None


def read_settings() -> dict:
    try:
        with paths.SETTINGS_PATH.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def write_language(code: str | None) -> None:
    """Persist the language in settings.toml (None removes the setting, back to automatic)."""
    settings = {k: v for k, v in read_settings().items() if k != "language"}
    if code:
        settings["language"] = code
    lines = ["# rolehop user settings. Change the language with: rolehop language <en|es|pt|auto>"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in settings.items() if isinstance(v, (str, int, bool))]
    paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.SETTINGS_PATH.with_name(paths.SETTINGS_PATH.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(paths.SETTINGS_PATH)
    current.cache_clear()


def resolve() -> tuple[str, str]:
    """Return (language, source) where source is one of: flag, env, settings, system, default."""
    if lang := normalize(_explicit):
        return lang, "flag"
    if lang := normalize(os.environ.get("ROLEHOP_LANG")):
        return lang, "env"
    if lang := normalize(str(read_settings().get("language") or "")):
        return lang, "settings"
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:  # the first variable that is set decides, as in POSIX
            return (normalize(value) or "en"), "system"
    return "en", "default"


@cache
def current() -> str:
    return resolve()[0]


def set_language(code: str | None) -> None:
    """Force a language for this process (from --lang)."""
    global _explicit
    _explicit = code
    current.cache_clear()


@cache
def catalog(lang: str) -> dict[str, str]:
    """Merged catalog for a language (empty for English)."""
    if lang == "en":
        return {}
    merged: dict[str, str] = {}
    folder = resources.files("rolehop").joinpath("locales", lang)
    if not folder.is_dir():
        return merged
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            merged.update(json.loads(entry.read_text(encoding="utf-8")))
    return merged


def _(message: str, /, **values: object) -> str:
    """Translate an English message and fill its named placeholders."""
    text = catalog(current()).get(message) or message
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):  # a broken translation must never crash the tool
        return message.format(**values)


ARGPARSE = {  # argparse's own strings, so --help is fully translated
    "usage: ": {"es": "uso: ", "pt": "uso: "},
    "positional arguments": {"es": "argumentos posicionales", "pt": "argumentos posicionais"},
    "options": {"es": "opciones", "pt": "opções"},
    "show this help message and exit": {"es": "muestra esta ayuda y sale", "pt": "mostra esta ajuda e sai"},
    "show program's version number and exit": {"es": "muestra la versión y sale", "pt": "mostra a versão e sai"},
    "the following arguments are required: %s": {"es": "faltan argumentos obligatorios: %s",
                                                 "pt": "faltam argumentos obrigatórios: %s"},
    "invalid choice: %(value)r (choose from %(choices)s)": {"es": "opción no válida: %(value)r (elige entre %(choices)s)",
                                                            "pt": "opção inválida: %(value)r (escolha entre %(choices)s)"},
    "unrecognized arguments: %s": {"es": "argumentos no reconocidos: %s", "pt": "argumentos não reconhecidos: %s"},
    "%(prog)s: error: %(message)s\n": {"es": "%(prog)s: error: %(message)s\n", "pt": "%(prog)s: erro: %(message)s\n"},
}


def install_argparse_translations() -> None:
    """Route argparse's built-in strings through our catalog."""
    import argparse

    def translate(text: str) -> str:
        return ARGPARSE.get(text, {}).get(current(), text)

    argparse._ = translate  # type: ignore[attr-defined]
