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

from rolehop import __version__

REPO = "escobarjsa/rolehop"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = f"rolehop/{__version__}"


class VerificationError(Exception):
    """The downloaded release does not match its published checksum."""


def _parse(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version.split("+")[0])[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def _get(url: str, accept: str = "application/octet-stream") -> bytes:
    from rolehop import ui
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
        raise VerificationError("the release publishes no checksum for the wheel; refusing to install it")
    if len(set(expected.values())) > 1:
        raise VerificationError("the GitHub asset digest and SHA256SUMS disagree")
    data = _get(rel["wheel"])
    actual = hashlib.sha256(data).hexdigest()
    want = next(iter(expected.values()))
    if actual != want:
        raise VerificationError(f"checksum mismatch for {rel['wheel_name']}: expected {want}, got {actual}")
    path = dest / rel["wheel_name"]  # keep the original name: installers require a valid wheel file name
    path.write_bytes(data)
    print(f"✓ Verified {rel['wheel_name']} (sha256 {actual[:12]}…, {' + '.join(expected)})")
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


def cmd_upgrade(args) -> int:
    try:
        rel = latest_release()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"✗ {REPO} has no published releases on GitHub yet.", file=sys.stderr)
        else:
            print(f"✗ GitHub returned {exc.code} when fetching releases.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"✗ Could not fetch releases from GitHub: {exc}", file=sys.stderr)
        return 1
    if not rel["tag"]:
        print("✗ GitHub returned no release.", file=sys.stderr)
        return 1
    current, latest = _parse(__version__), _parse(rel["version"])
    if latest <= current and not args.force:
        print(f"✓ rolehop {__version__} is already the latest version ({rel['tag']}).")
        return 0
    print(f"→ New version available: {__version__} → {rel['version']}  {rel['url']}")
    tool = installer()
    git_source = f"git+https://github.com/{REPO}@{rel['tag']}"
    if args.check or tool is None:
        source = rel["wheel"] or git_source
        cmd = upgrade_command(source, tool)
        if cmd is None:
            print("  Could not determine how rolehop was installed. Upgrade manually with:\n"
                  f"    uv tool install --force {source}")
            return 0 if args.check else 1
        print(f"  To upgrade: rolehop upgrade   (installs {rel['wheel_name'] or rel['tag']} after verifying its checksum)")
        return 0
    with tempfile.TemporaryDirectory(prefix="rolehop-upgrade-") as tmp:
        if rel["wheel"]:
            try:
                source = str(download_verified(rel, Path(tmp)))
            except VerificationError as exc:
                print(f"✗ Not upgrading: {exc}.", file=sys.stderr)
                return 1
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"✗ Could not download the release: {exc}", file=sys.stderr)
                return 1
        else:
            source = git_source  # no wheel attached: let the installer fetch the tag from GitHub
        cmd = upgrade_command(source, tool)
        if cmd is None:
            print(f"✗ {tool} is not on PATH. Upgrade manually with: uv tool install --force {rel['wheel'] or git_source}",
                  file=sys.stderr)
            return 1
        print(f"→ {' '.join(cmd)}")
        rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"✓ Upgraded to {rel['version']}. If the agent integration changed, run: rolehop agent install")
    return rc


def register(sub) -> None:
    s = sub.add_parser("upgrade", help="upgrade rolehop to the latest GitHub release (checksum-verified)")
    s.add_argument("--check", action="store_true", help="only check whether a new version is available")
    s.add_argument("--force", action="store_true", help="reinstall even if already on the latest version")
    s.set_defaults(func=cmd_upgrade)
