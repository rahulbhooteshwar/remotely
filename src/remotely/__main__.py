"""Entry point for ``python -m remotely`` and for the bundled binary.

The import is absolute on purpose: PyInstaller runs this file as a top-level
script with no package context, so a relative import fails in the bundle while
working fine from source.
"""

from remotely.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
