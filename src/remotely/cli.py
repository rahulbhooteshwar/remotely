"""Entry point.

``remotely`` takes no subcommands by design - everything lives in the TUI's
command bar. The few flags here exist for bootstrapping and diagnostics.

Launching does one non-obvious thing: unless it is already inside tmux, the
process re-executes itself as ``tmux attach`` against a session whose window 0
runs the TUI. That is what makes SSH sessions open as real tabs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__, config
from .sessions import DEFAULT_SESSION, TmuxBackend, ensure_utf8_locale, inside_tmux


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
    parser.add_argument(
        "--no-tmux",
        action="store_true",
        help="run the UI in this terminal without a tmux session (tabs unavailable)",
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help=f"tmux session name to use (default: {DEFAULT_SESSION})",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: we are already inside the tmux window
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check the environment and exit",
    )
    return parser


def _self_command() -> str:
    """The command that re-launches this program inside tmux."""
    executable = shutil.which("remotely")
    if executable and getattr(sys, "frozen", False) is False:
        # Prefer the installed console script; it survives PATH changes better
        # than an absolute interpreter path baked into the tmux session.
        return f"{executable} --ui"
    return f"{sys.executable} -m remotely --ui"


def doctor() -> int:
    """Report on everything the app depends on."""
    from .ssh import askpass_force_supported, find_askpass_helper, openssh_version
    from .store import HostStore
    from .themes import ThemeRegistry
    from .vault import Vault

    config.ensure_layout()
    ok = True

    print(f"Remotely {__version__}")
    print(f"  config dir      {config.home()}")

    tmux_path = shutil.which("tmux")
    print(f"  tmux            {tmux_path or 'NOT FOUND'}")
    if not tmux_path:
        ok = False

    ssh_path = shutil.which("ssh")
    version = openssh_version()
    version_text = f" (OpenSSH {version[0]}.{version[1]})" if version else ""
    print(f"  ssh             {ssh_path or 'NOT FOUND'}{version_text}")
    if not ssh_path:
        ok = False

    if askpass_force_supported():
        print("  askpass         SSH_ASKPASS_REQUIRE supported")
    else:
        print("  askpass         OpenSSH < 8.4 - stored passwords cannot be auto-supplied")
        ok = False

    helper = find_askpass_helper()
    print(f"  askpass helper  {helper or 'NOT FOUND'}")
    if helper is None:
        ok = False

    try:
        store = HostStore()
        print(f"  hosts           {len(store)} in {store.path}")
    except Exception as exc:
        print(f"  hosts           ERROR: {exc}")
        ok = False

    vault = Vault()
    print(f"  vault           {'present' if vault.exists() else 'not created yet'}")

    registry = ThemeRegistry()
    print(f"  themes          {len(registry)} ({', '.join(registry.names())})")
    for error in registry.errors:
        print(f"                  WARNING {error}")

    from .sessions import locale_supports_unicode

    if locale_supports_unicode():
        print(f"  locale          UTF-8 ({os.environ.get('LC_CTYPE') or os.environ.get('LANG')})")
    else:
        # tmux emits output libtmux cannot parse without one, so this is fatal
        # rather than cosmetic.
        print("  locale          no UTF-8 locale available - tmux support will fail")
        ok = False

    print()
    print("All good." if ok else "Some checks failed - see above.")
    return 0 if ok else 1


def _launch_in_tmux(session_name: str) -> int:
    """Create the session if needed, then hand this terminal over to tmux."""
    backend = TmuxBackend(session_name=session_name)
    if not backend.is_available():
        print(
            "tmux was not found on PATH.\n"
            "Install it (brew install tmux / apt install tmux) or run "
            "'remotely --no-tmux' for a single-pane UI without tabs.",
            file=sys.stderr,
        )
        return 1

    try:
        backend.ensure_session(_self_command(), start_directory=os.getcwd())
    except Exception as exc:
        print(f"Could not prepare the tmux session: {exc}", file=sys.stderr)
        return 1

    argv = backend.attach_command()
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:  # pragma: no cover - exec rarely fails after which()
        print(f"Could not attach to tmux: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.version:
        print(f"remotely {__version__}")
        return 0

    # Do this before anything talks to tmux or draws a glyph.
    ensure_utf8_locale()

    if args.doctor:
        return doctor()

    config.ensure_layout()

    # --ui means "the tmux window already exists, just draw"; --no-tmux means
    # the user opted out of tabs entirely.
    if args.ui or args.no_tmux or inside_tmux():
        from .tui.app import RemotelyApp

        RemotelyApp().run()
        return 0

    return _launch_in_tmux(args.session)


if __name__ == "__main__":
    raise SystemExit(main())
