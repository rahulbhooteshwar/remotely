"""Self-update: version comparison, artefact naming, checksum enforcement."""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from remotely import updater
from remotely.updater import Release, UpdateError, is_newer, parse_version, target_name


# ------------------------------------------------------------------ versions


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.0.0", (1, 0, 0)),
        ("v1.0.0", (1, 0, 0)),
        ("2.11.3", (2, 11, 3)),
        ("1.0.0rc1", (1, 0, 0)),
        ("WIP-local-build", None),
        ("", None),
    ],
)
def test_parse_version(text: str, expected) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.9", True),
        ("2.0.0", "1.99.99", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "1.0.1", False),
        # 10 must beat 9, which a string compare would get wrong.
        ("1.10.0", "1.9.0", True),
        ("1.9.0", "1.10.0", False),
    ],
)
def test_is_newer(candidate: str, current: str, expected: bool) -> None:
    assert is_newer(candidate, current) is expected


def test_dev_build_always_offers_an_update() -> None:
    """A build with no parseable version must not silently no-op."""
    assert is_newer("1.0.0", "WIP-local-build") is True


def test_unparseable_release_is_never_newer() -> None:
    assert is_newer("nightly", "1.0.0") is False


# ------------------------------------------------------------------ platform


def test_target_name_matches_release_artefacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """These strings must match what the release workflow uploads."""
    cases = [
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "aarch64", "linux-arm64"),
        ("linux", "arm64", "linux-arm64"),
        ("darwin", "arm64", "macos-arm64"),
    ]
    for plat, machine, expected in cases:
        monkeypatch.setattr(updater.sys, "platform", plat)
        monkeypatch.setattr(updater.platform, "machine", lambda m=machine: m)
        assert target_name() == expected


def test_intel_mac_explains_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    with pytest.raises(UpdateError, match="arm64-only macOS wheels"):
        target_name()


def test_unknown_platform_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.sys, "platform", "sunos5")
    with pytest.raises(UpdateError):
        target_name()


# ------------------------------------------------------------------ applying


def _make_archive(directory: Path, payload: bytes = b"#!/bin/sh\necho new\n") -> Path:
    """A onedir release archive: a `remotely/` tree, not a lone binary."""
    tree = directory / "remotely"
    (tree / "_internal").mkdir(parents=True)
    binary = tree / "remotely"
    binary.write_bytes(payload)
    (tree / "_internal" / "libfoo.so").write_bytes(b"\x7fELF fake library\n")
    archive = directory / "remotely-linux-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tree, arcname="remotely")
    shutil.rmtree(tree)
    return archive


def _install_runtime(root: Path, payload: bytes = b"old binary") -> Path:
    """A directory laid out the way install.sh leaves one."""
    runtime = root / "lib" / "remotely"
    (runtime / "_internal").mkdir(parents=True)
    (runtime / "remotely").write_bytes(payload)
    (runtime / "_internal" / "libfoo.so").write_bytes(b"old library")
    return runtime


@pytest.fixture
def fake_release(tmp_path, monkeypatch):
    """A release served from local files, with a correct checksum."""
    source = tmp_path / "src"
    source.mkdir()
    archive = _make_archive(source)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = source / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n")

    def fake_download(url: str, destination: Path) -> None:
        Path(destination).write_bytes(Path(url).read_bytes())

    monkeypatch.setattr(updater, "_download", fake_download)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")

    return Release(
        tag="v9.9.9",
        version="9.9.9",
        assets={archive.name: str(archive), checksum.name: str(checksum)},
    ), source


def test_apply_replaces_the_running_runtime(tmp_path, monkeypatch, fake_release) -> None:
    release, _ = fake_release
    runtime = _install_runtime(tmp_path)
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(runtime / "remotely"))

    result = updater.apply_update(release, log=lambda *_: None)

    assert result == runtime / "remotely"
    assert (runtime / "remotely").read_bytes().startswith(b"#!/bin/sh")
    assert (runtime / "remotely").stat().st_mode & 0o111, "entry point is not executable"
    # The bundled libraries came across too, not just the entry point.
    assert (runtime / "_internal" / "libfoo.so").read_bytes().startswith(b"\x7fELF")
    # Nothing staged left behind.
    assert not runtime.with_name("remotely.new").exists()
    assert not runtime.with_name("remotely.old").exists()


def test_update_finds_the_runtime_through_the_path_symlink(tmp_path, monkeypatch) -> None:
    """`remotely` on PATH is a symlink; the runtime beside it is the target.

    Without resolving it, an update would write into ~/.local/bin instead of
    the runtime directory.
    """
    runtime = _install_runtime(tmp_path)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    link = link_dir / "remotely"
    link.symlink_to(runtime / "remotely")

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(link))
    assert updater.runtime_dir() == runtime.resolve()


def test_legacy_single_file_install_is_told_to_reinstall(tmp_path, monkeypatch, fake_release) -> None:
    """The old onefile layout cannot be swapped for a directory in place."""
    installed = tmp_path / "bin" / "remotely"
    installed.parent.mkdir()
    installed.write_bytes(b"old onefile binary")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(installed))

    assert updater.runtime_dir() is None
    with pytest.raises(UpdateError) as excinfo:
        updater.apply_update(release=fake_release[0], log=lambda *_: None)
    message = str(excinfo.value)
    assert "install.sh" in message, message
    # And it must not have damaged the existing install.
    assert installed.read_bytes() == b"old onefile binary"


def test_apply_refuses_a_bad_checksum(tmp_path, monkeypatch, fake_release) -> None:
    """A corrupted download must never reach the installed path."""
    release, source = fake_release
    checksum = Path(release.assets["remotely-linux-x86_64.tar.gz.sha256"])
    checksum.write_text(f"{'0' * 64}  remotely-linux-x86_64.tar.gz\n")

    runtime = _install_runtime(tmp_path)
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(runtime / "remotely"))

    with pytest.raises(UpdateError, match="Checksum mismatch"):
        updater.apply_update(release, log=lambda *_: None)

    assert (runtime / "remotely").read_bytes() == b"old binary", "binary was replaced despite a bad checksum"


def test_apply_reports_a_missing_artefact(tmp_path, monkeypatch, fake_release) -> None:
    release, _ = fake_release
    release.assets.pop("remotely-linux-x86_64.tar.gz")
    installed = tmp_path / "bin" / "remotely"
    installed.parent.mkdir()
    installed.write_bytes(b"old")
    monkeypatch.setattr(updater.sys, "executable", str(installed))

    with pytest.raises(UpdateError, match="still be publishing"):
        updater.apply_update(release, log=lambda *_: None)


def test_source_checkout_is_told_to_use_uv(monkeypatch, capsys) -> None:
    monkeypatch.setattr(updater, "running_as_binary", lambda: False)
    lines: list[str] = []
    assert updater.update(log=lines.append) == 0
    assert any("uv tool install" in line for line in lines)
