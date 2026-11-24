"""Filesystem locations and environment overrides, plus migration from the former `awsorg` name."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME = Path.home()
LEGACY_NAME = "awsorg"  # the tool's former name (up to 1.2.0): its files and markers are still recognized


def env(name: str) -> str | None:
    """Return ROLEHOP_<name>, falling back to the legacy AWSORG_<name>."""
    return os.environ.get(f"ROLEHOP_{name}") or os.environ.get(f"{LEGACY_NAME.upper()}_{name}") or None


CONFIG_PATH = Path(env("CONFIG") or HOME / ".config/rolehop/orgs.toml")
# User preferences (language…), kept apart from orgs.toml so the wizard can rewrite orgs.toml freely.
SETTINGS_PATH = Path(env("SETTINGS") or Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "rolehop/settings.toml")
LEGACY_CONFIG_PATH = HOME / f".config/{LEGACY_NAME}/orgs.toml"
AWS_CONFIG = Path(os.environ.get("AWS_CONFIG_FILE") or HOME / ".aws/config")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "rolehop"
LEGACY_STATE_DIR = STATE_DIR.with_name(LEGACY_NAME)


def migrate_legacy() -> list[str]:
    """Copy awsorg's orgs.toml and state to the rolehop paths if they don't exist yet. Never deletes anything."""
    from rolehop import ui
    from rolehop.i18n import _  # imported here: rolehop.i18n imports this module
    msgs = []
    if not env("CONFIG") and not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.is_file():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
        msgs.append("✓ " + _("Migrated {old} to {new} (original kept)", old=ui.short(LEGACY_CONFIG_PATH),
                             new=ui.short(CONFIG_PATH)))
    if not STATE_DIR.exists() and LEGACY_STATE_DIR.is_dir():
        shutil.copytree(LEGACY_STATE_DIR, STATE_DIR)
        msgs.append("✓ " + _("Migrated the state from {old} to {new}", old=ui.short(LEGACY_STATE_DIR),
                             new=ui.short(STATE_DIR)))
    return msgs


SSO_CACHE = Path(env("SSO_CACHE") or HOME / ".aws/sso/cache")
# Where the AWS CLI caches role credentials obtained through SSO (one file per account/role/sso-session).
CLI_CACHE = Path(env("CLI_CACHE") or HOME / ".aws/cli/cache")
