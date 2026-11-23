"""`rolehop upgrade`: install the latest GitHub release with the same installer, after verifying its checksum.

The wheel is downloaded to a temporary directory and its SHA-256 is checked against the digest GitHub reports
for the release asset and, when present, against the release's ``SHA256SUMS`` file. Only the verified local file
is handed to ``uv``/``pipx``. Releases also carry a signed build provenance attestation that can be checked with
``gh attestation verify``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from rolehop import __version__, ui
from rolehop.i18n import _

REPO = "escobarjsa/rolehop"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = f"rolehop/{__version__}"


class VerificationError(Exception):
    """The downloaded release does not match its published checksum."""


def _parse(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version.split("+")[0])[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def _get(url: str, accept: str = "application/octet-stream") -> bytes:
    ui.debug(f"GET {url}")
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def latest_release() -> dict:
    """Latest release metadata: tag, version, page URL, wheel URL, wheel digest and SHA256SUMS URL."""
    url = os.environ.get("ROLEHOP_RELEASES_URL", API)
    data = json.loads(_get(url, accept="application/vnd.github+json"))
    tag = data.get("tag_name") or ""
    assets = {str(a.get("name", "")): a for a in data.get("assets", [])}
    wheel = next((a for name, a in assets.items() if name.endswith(".whl")), None)
    sums = assets.get("SHA256SUMS")
    digest = str((wheel or {}).get("digest") or "")
    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "url": data.get("html_url", ""),
        "wheel": wheel["browser_download_url"] if wheel else None,
        "wheel_name": wheel["name"] if wheel else None,
        "wheel_sha256": digest.removeprefix("sha256:") if digest.startswith("sha256:") else None,
        "sums": sums["browser_download_url"] if sums else None,
    }


def download_verified(rel: dict, dest: Path) -> Path:
    """Download the release wheel into ``dest`` and verify it. Raises VerificationError on any mismatch."""
    expected: dict[str, str] = {}
    if rel.get("wheel_sha256"):
        expected["GitHub asset digest"] = rel["wheel_sha256"].lower()
    if rel.get("sums"):
        for line in _get(rel["sums"]).decode().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == rel["wheel_name"]:
                expected["SHA256SUMS"] = parts[0].lower()
    if not expected:
        raise VerificationError(_("the release publishes no checksum for the wheel; refusing to install it"))
    if len(set(expected.values())) > 1:
        raise VerificationError(_("the GitHub asset digest and SHA256SUMS disagree"))
    data = _get(rel["wheel"])
    actual = hashlib.sha256(data).hexdigest()
    want = next(iter(expected.values()))
    if actual != want:
        raise VerificationError(_("checksum mismatch for {name}: expected {expected}, got {actual}",
                                  name=rel["wheel_name"], expected=want, actual=actual))
    path = dest / rel["wheel_name"]  # keep the original name: installers require a valid wheel file name
    path.write_bytes(data)
    labels = {"GitHub asset digest": _("GitHub asset digest"), "SHA256SUMS": "SHA256SUMS"}
    ui.echo("✓ " + _("Verified {name} (sha256 {digest}…, {sources})", name=rel["wheel_name"], digest=actual[:12],
                     sources=" + ".join(labels[k] for k in expected)))
    return path


def installer() -> str | None:
    """Return 'uv' or 'pipx' depending on where rolehop is installed; None if unknown (e.g. a development venv)."""
    prefix = sys.prefix.replace("\\", "/")
    uv_dir = (os.environ.get("UV_TOOL_DIR") or "").replace("\\", "/").rstrip("/")
    if "/uv/tools/" in prefix or (uv_dir and prefix.startswith(uv_dir + "/")):
        return "uv"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    return None


def upgrade_command(source: str, tool: str | None) -> list[str] | None:
    if tool == "uv" and shutil.which("uv"):
        return ["uv", "tool", "install", "--force", source]
    if tool == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", source]
    return None


def _retry_hint() -> None:
    ui.echo("→ " + _("try again later, or download it from {url}", url=RELEASES_PAGE), err=True)


def cmd_upgrade(args) -> int:
    try:
        rel = latest_release()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ui.echo("✗ " + _("{repo} has no published releases on GitHub yet.", repo=REPO), err=True)
            return 1
        ui.echo("✗ " + _("GitHub returned HTTP {code} when fetching releases.", code=exc.code), err=True)
        _retry_hint()
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        ui.echo("✗ " + _("Could not fetch releases from GitHub: {error}", error=exc), err=True)
        _retry_hint()
        return 1
    if not rel["tag"]:
        ui.echo("✗ " + _("GitHub returned no release."), err=True)
        _retry_hint()
        return 1
    current, latest = _parse(__version__), _parse(rel["version"])
    if latest <= current and not args.force:
        ui.echo("✓ " + _("rolehop {version} is already the latest version ({tag}).", version=__version__, tag=rel["tag"]))
        return 0
    ui.echo(_("New version available: {latest} (installed: {current}). Release notes: {url}",
              latest=rel["version"], current=__version__, url=rel["url"]))
    tool = installer()
    git_source = f"git+https://github.com/{REPO}@{rel['tag']}"
    if args.check or tool is None:
        source = rel["wheel"] or git_source
        cmd = upgrade_command(source, tool)
        if cmd is None:
            ui.echo("! " + _("Could not detect the installer (uv or pipx), so the upgrade cannot run automatically."))
            ui.echo("→ " + _("run: {cmd}", cmd=f"uv tool install --force {source}"))
            return 0 if args.check else 1
        ui.echo("→ " + _("run: {cmd}", cmd="rolehop upgrade"))
        ui.hint("  " + _("It installs {name} after verifying its checksum.", name=rel["wheel_name"] or rel["tag"]))
        return 0
    with tempfile.TemporaryDirectory(prefix="rolehop-upgrade-") as tmp:
        if rel["wheel"]:
            try:
                source = str(download_verified(rel, Path(tmp)))
            except VerificationError as exc:
                ui.echo("✗ " + _("Not upgrading: {reason}.", reason=exc), err=True)
                ui.echo("→ " + _("do not install this release by hand; report it at {url}",
                                 url=f"https://github.com/{REPO}/issues"), err=True)
                return 1
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                ui.echo("✗ " + _("Could not download the release: {error}", error=exc), err=True)
                _retry_hint()
                return 1
        else:
            source = git_source  # no wheel attached: let the installer fetch the tag from GitHub
        cmd = upgrade_command(source, tool)
        if cmd is None:
            ui.echo("✗ " + _("{tool} is not on PATH, so the upgrade cannot run automatically.", tool=tool), err=True)
            ui.echo("→ " + _("run: {cmd}", cmd=f"uv tool install --force {rel['wheel'] or git_source}"), err=True)
            return 1
        ui.echo(_("Running: {cmd}", cmd=" ".join(cmd)))
        rc = subprocess.run(cmd).returncode
    if rc == 0:
        ui.echo("✓ " + _("Upgraded to {version}.", version=rel["version"]))
        ui.hint(_("If the agent integration changed, run: {cmd}", cmd="rolehop agent install"))
    return rc


def register(sub) -> None:
    s = sub.add_parser("upgrade", help=_("upgrade to the latest GitHub release (checksum-verified)"))
    s.add_argument("--check", action="store_true", help=_("only check whether a new version is available"))
    s.add_argument("--force", action="store_true", help=_("reinstall even if already on the latest version"))
    s.set_defaults(func=cmd_upgrade)
