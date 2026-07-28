"""Self-update for the bundled binary.

A single self-contained binary has no package manager behind it, so it has to
know how to replace itself. This does what ``install.sh`` does - resolve the
latest release, download the artefact for this platform, verify its checksum -
except it targets the running executable.

Only the standard library is used: pulling in a HTTP client for one occasional
download would be weight in every binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

REPO = os.environ.get("REMOTELY_REPO", "rahulbhooteshwar/remotely")
API = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 30


class UpdateError(RuntimeError):
    """Anything that stops an update, phrased for a terminal."""


@dataclass(slots=True)
class Release:
    tag: str
    version: str
    assets: dict[str, str]  # name -> download url


def running_as_binary() -> bool:
    """Whether this is the PyInstaller bundle rather than a source checkout."""
    return bool(getattr(sys, "frozen", False))


def target_name() -> str:
    """The release artefact for this platform, matching install.sh."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        raise UpdateError(
            "Intel macOS has no prebuilt binary, because `cryptography` ships "
            "arm64-only macOS wheels. Update from source instead:\n"
            f"  uv tool install --force git+https://github.com/{REPO}"
        )
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-arm64"
        raise UpdateError(f"No prebuilt binary for Linux {machine}.")
    raise UpdateError(f"No prebuilt binary for {sys.platform}.")


def parse_version(text: str) -> tuple[int, ...] | None:
    """Numeric version tuple, or ``None`` for a development build."""
    cleaned = text.strip().lstrip("v")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` supersedes ``current``.

    A build with no parseable version (a source checkout, or a dev build) is
    always treated as older, so ``--update`` is never a silent no-op there.
    """
    new = parse_version(candidate)
    old = parse_version(current)
    if new is None:
        return False
    if old is None:
        return True
    return new > old


def fetch_latest() -> Release:
    request = urllib.request.Request(
        API, headers={"Accept": "application/vnd.github+json", "User-Agent": "remotely"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(f"No published releases for {REPO}.") from exc
        raise UpdateError(f"Could not reach GitHub ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc

    tag = str(payload.get("tag_name") or "")
    if not tag:
        raise UpdateError("The latest release has no tag.")
    assets = {
        a["name"]: a["browser_download_url"] for a in payload.get("assets", []) if a.get("name")
    }
    return Release(tag=tag, version=tag.lstrip("v"), assets=assets)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "remotely"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            with open(destination, "wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Download failed: {exc}") from exc


def _verify(archive: Path, expected_line: str) -> None:
    expected = expected_line.split()[0].strip().lower()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected:
        raise UpdateError(
            f"Checksum mismatch. Expected {expected}, got {digest}. Not installing."
        )


def apply_update(release: Release, *, log=print) -> Path:
    """Download, verify and swap in the new binary. Returns its path."""
    name = f"remotely-{target_name()}.tar.gz"
    url = release.assets.get(name)
    if url is None:
        # A release is created before its binaries finish uploading, so this is
        # usually a few-minute window rather than a missing platform.
        available = ", ".join(sorted(release.assets)) or "nothing yet"
        raise UpdateError(
            f"Release {release.tag} has no {name}.\n"
            f"  It may still be publishing - try again in a few minutes.\n"
            f"  Currently attached: {available}"
        )

    destination = Path(sys.executable).resolve()
    if not os.access(destination.parent, os.W_OK):
        raise UpdateError(
            f"No write permission for {destination.parent}. "
            "Re-run with the right permissions, or reinstall with install.sh."
        )

    with tempfile.TemporaryDirectory(prefix="remotely-update-") as tmp:
        work = Path(tmp)
        archive = work / name
        log(f"Downloading {name} ...")
        _download(url, archive)

        checksum_url = release.assets.get(f"{name}.sha256")
        if checksum_url:
            checksum = work / f"{name}.sha256"
            _download(checksum_url, checksum)
            _verify(archive, checksum.read_text(encoding="utf-8"))
            log("Checksum verified.")
        else:
            log("WARNING: release published no checksum; skipping verification.")

        try:
            with tarfile.open(archive) as tar:
                # Never let an archive write outside the work directory.
                for member in tar.getmembers():
                    if member.name != "remotely" or not member.isfile():
                        continue
                    # Explicit filter, not just to silence the Python 3.12+
                    # DeprecationWarning: "data" also strips setuid/setgid bits
                    # and rejects device files, which a single named regular
                    # member does not otherwise guard against.
                    tar.extract(member, work, filter="data")
                    break
                else:
                    raise UpdateError("Archive did not contain a 'remotely' binary.")
        except tarfile.TarError as exc:
            raise UpdateError(f"Could not unpack the archive: {exc}") from exc

        fresh = work / "remotely"
        fresh.chmod(fresh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Replacing a running executable is fine on Unix: rename unlinks the old
        # inode, which stays alive for this process until it exits. Staging in
        # the destination directory keeps the rename on one filesystem so it is
        # atomic - a half-copied binary would leave nothing runnable.
        staged = destination.with_name(destination.name + ".new")
        shutil.copy2(fresh, staged)
        staged.chmod(0o755)
        os.replace(staged, destination)

    return destination


def update(*, log=print) -> int:
    """``remotely --update``. Returns a process exit code."""
    if not running_as_binary():
        log(
            "This is a source checkout, not the packaged binary.\n"
            f"  uv tool install --force git+https://github.com/{REPO}\n"
            "or 'git pull' if you are working on it."
        )
        return 0

    log(f"Installed: {__version__}")
    release = fetch_latest()
    log(f"Latest:    {release.version}")

    if not is_newer(release.version, __version__):
        log("Already up to date.")
        return 0

    try:
        path = apply_update(release, log=log)
    except UpdateError as exc:
        log(f"Update failed: {exc}")
        return 1

    log(f"Updated to {release.version} at {path}.")
    log("Restart Remotely to use it.")
    return 0
