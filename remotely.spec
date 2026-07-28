# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build.

Produces a single executable carrying Python, Textual, Paramiko and pyte, so a
user needs nothing installed - no Python, no uv, no tmux, no OpenSSH.

Build with:  pyinstaller remotely.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SRC = Path(SPECPATH) / "src"

datas = []
binaries = []
hiddenimports = []

# Textual and Rich ship .tcss and data files that are loaded at runtime; missing
# them produces a binary that starts and then fails to draw.
for package in ("textual", "rich"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Paramiko selects key and cipher backends dynamically, so the import graph
# does not reach them.
hiddenimports += collect_submodules("paramiko")
hiddenimports += collect_submodules("pyte")
hiddenimports += [
    "cryptography",
    "cryptography.hazmat.backends.openssl",
    "cryptography.hazmat.bindings._rust",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.kdf.scrypt",
    "nacl",
    "nacl.bindings",
    "nacl.signing",
    "_cffi_backend",
]

# Our own runtime data: the stylesheet and the bundled themes.
datas += [
    (str(SRC / "remotely" / "tui" / "app.tcss"), "remotely/tui"),
    (str(SRC / "remotely" / "themes_data"), "remotely/themes_data"),
]

a = Analysis(
    [str(SRC / "remotely" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing here is used, and each one is measurable weight.
        "tkinter",
        "matplotlib",
        "numpy",
        "PIL",
        "pytest",
        "setuptools",
        "pip",
        "wheel",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# A onedir build, deliberately, and it is the single biggest thing keeping
# startup fast.
#
# In onefile mode the bootloader unpacks the whole archive - 26MB and ~38
# shared libraries - into a fresh temp directory on *every* launch and deletes
# it on exit. On Linux that costs about half a second. On macOS it is far
# worse: each library lands at a new path every time, so Gatekeeper re-verifies
# all of them on every run and never gets to reuse a verdict, which took
# startup to roughly ten seconds.
#
# runtime_tmpdir does NOT fix this - it only moves where the throwaway
# directory is created; the bootloader still extracts and still deletes.
# Measured, onedir starts in ~110ms against ~500-800ms for onefile on Linux,
# and removes the repeated Gatekeeper work on macOS entirely.
#
# The cost is that the artefact is a directory rather than one file. install.sh
# unpacks it once into ~/.local/lib/remotely and links the entry point into
# ~/.local/bin, so that stays invisible to users.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="remotely",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles signatures on macOS and buys little here.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="remotely",
)
