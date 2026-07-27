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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="remotely",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles signatures on macOS and buys little here.
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
