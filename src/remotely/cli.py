"""Entry point.

``remotely`` takes no subcommands by design - everything lives in the TUI's
command bar. The few flags here exist for diagnostics.

Nothing is re-executed and nothing is shelled out to: the binary carries its
own SSH client and terminal emulator, so starting up is just drawing the UI.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__, config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remotely",
        description="Remotely - terminal based SSH hosts and connection manager.",
        epilog=(
            "Run 'remotely' with no arguments. Everything else - adding hosts, "
            "themes, credentials, connecting - happens in the app's command bar."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--doctor", action="store_true", help="check the environment and exit")
    parser.add_argument(
        "--config",
        metavar="DIR",
        help=(
            "use a different configuration directory "
            f"(default: {config.DEFAULT_HOME}, or $REMOTELY_HOME)"
        ),
    )
    return parser


def _frozen() -> bool:
    """Whether we are running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def doctor() -> int:
    """Report on the environment. Very little is required, by design."""
    from .store import HostStore
    from .themes import ThemeRegistry
    from .transport import system_ssh_available
    from .vault import Vault

    config.ensure_layout()
    ok = True

    print(f"Remotely {__version__}")
    print(f"  build           {'self-contained binary' if _frozen() else 'source checkout'}")
    print(f"  python          {sys.version.split()[0]}{' (bundled)' if _frozen() else ''}")
    print(f"  config dir      {config.home()}")

    try:
        import paramiko

        print(f"  ssh client      built-in (paramiko {paramiko.__version__})")
    except ImportError:
        print("  ssh client      MISSING - paramiko is not available")
        ok = False

    try:
        import pyte

        print(f"  terminal        built-in (pyte {getattr(pyte, '__version__', 'ok')})")
    except ImportError:
        print("  terminal        MISSING - pyte is not available")
        ok = False

    system_ssh = shutil.which("ssh")
    print(f"  system ssh      {system_ssh or 'not installed'} (optional)")

    try:
        store = HostStore()
        print(f"  hosts           {len(store)} in {store.path}")
        needs_ssh = [h.name for h in store if h.use_system_ssh]
        if needs_ssh and not system_ssh_available():
            print(
                f"                  WARNING {len(needs_ssh)} host(s) need system ssh: "
                f"{', '.join(needs_ssh[:5])}"
            )
            ok = False
    except Exception as exc:
        print(f"  hosts           ERROR: {exc}")
        ok = False

    vault = Vault()
    print(f"  vault           {'present' if vault.exists() else 'not created yet'}")

    registry = ThemeRegistry()
    print(f"  themes          {len(registry)} ({', '.join(registry.names())})")
    for error in registry.errors:
        print(f"                  WARNING {error}")
        ok = False

    print()
    print("All good." if ok else "Some checks failed - see above.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.version:
        print(f"remotely {__version__}")
        return 0

    if args.config:
        os.environ[config.ENV_HOME] = args.config

    if args.doctor:
        return doctor()

    config.ensure_layout()

    from .tui.app import RemotelyApp

    RemotelyApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
