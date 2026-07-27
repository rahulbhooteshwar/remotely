# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Remotely is a terminal SSH connection manager: a Textual TUI that launches SSH
sessions as tmux windows. It is a from-scratch successor to
[Connectify](https://github.com/rahulbhooteshwar/connectify-iterm2) and shares
no code with it. Unlike Connectify it is **not** macOS-specific and has no web
server — everything happens in one terminal.

A uv project: `pyproject.toml`, `uv.lock`, sources under `src/remotely/`.

## Commands

```bash
make setup                 # uv sync
make run                   # uv run remotely
make doctor                # environment diagnostics - run this first when debugging
make test                  # uv run pytest -q
make test-fast             # skip the tmux integration tests
make build                 # uv build (wheel + sdist)
make install               # uv tool install --force .

uv run pytest tests/test_vault.py::test_wrong_passcode_rejected   # single test
REMOTELY_HOME=/tmp/rh uv run remotely                             # throwaway config
```

`uv tool install --force .` alone can serve a **stale cached wheel**; use
`uv cache clean remotely && uv tool install --reinstall --force .` after
changing source if the installed binary seems not to update.

## Architecture

Layered, with the TUI at the top and no upward dependencies:

- **`models.py`** — `Host` and `Credential`. A `Host` never holds a secret; it
  references a vault credential by name. This is what keeps `hosts.json` safe to
  export and commit.
- **`store.py`** — `hosts.json` CRUD, atomic writes, grouping. Hosts are keyed
  by `name`, case-insensitively.
- **`vault.py`** — one encrypted file, one passcode. scrypt → AES-256-GCM. The
  envelope header is authenticated as AAD, so weakening the KDF params on disk
  fails the tag check rather than downgrading security.
- **`themes.py`** — TOML themes from the bundled package plus
  `~/.remotely/themes/`, user files shadowing bundled ones by name.
- **`ssh.py`** — builds argv and stages secrets. Never puts a password on the
  command line.
- **`sessions.py`** — libtmux wrapper. Tabs are tmux windows.
- **`completion.py`** — command-bar parsing and fuzzy ranking. Deliberately free
  of Textual imports so it is testable standalone.
- **`tui/`** — Textual app and modal screens.

`cli.py` is thin: it fixes the locale, then either draws the UI or re-execs into
tmux.

### The tmux launch model

Running `remotely` outside tmux **re-executes itself** as `tmux attach` against a
session whose window 0 runs `remotely --ui`. That indirection is what makes SSH
sessions real tabs. Consequences:

- `--ui` means "the tmux window already exists, just draw". It is internal.
- If already inside tmux, Remotely adopts the *current* session rather than
  creating its own, so it does not hijack a user's existing setup.
- Windows Remotely created are tagged with the tmux user option
  `@remotely_host`. That tag is the only way `list_tabs` identifies them, so a
  window the user opened by hand is never touched.

### Two non-obvious constraints

**A UTF-8 locale is mandatory.** Without one, tmux emits format output libtmux
cannot parse and session creation fails with an opaque
`zip() argument 2 is shorter than argument 1`. `ensure_utf8_locale()` sets
`LC_CTYPE` (and only `LC_CTYPE`) when the environment lacks one. Do not remove
this. Note that CPython's PEP 538 coercion already handles the plain `C` case,
so the guard matters for `LC_ALL` set to a non-UTF-8 value.

Theme icons are separate and purely cosmetic: tmux stores tab names verbatim,
but a client under a non-UTF-8 locale *renders* them as `__`, which is why
themes carry an `ascii_icon`.

**Commands are wrapped by `hold_on_failure()`.** A window whose command dies
instantly takes the window with it before libtmux can read the window id back,
turning "ssh not found" into an internal error. The tmux option for this
(`remain-on-exit`) is a *window* option, so it cannot be set on a session and
inherited, and setting it globally would alter the user's whole server. The
wrapper blocks on `read` after a non-zero exit instead. A clean exit still
closes the tab.

### Password delivery

Secrets reach ssh through `SSH_ASKPASS`, never argv or a persistent env var:

1. `stage_secret()` writes the secret to `~/.remotely/run/askpass-<ts>-<token>`,
   mode `0600` inside a `0700` directory, created with the right mode from the
   start.
2. `SSH_ASKPASS` points at the `remotely-askpass` console script and
   `SSH_ASKPASS_REQUIRE=force` makes ssh consult it despite having a TTY
   (OpenSSH 8.4+; older versions fall back to prompting).
3. The helper prints the secret once and unlinks it. A replay returns exit 1
   rather than an empty string, so ssh prompts instead of trying to auth with "".
4. `sweep_stale_secrets()` clears debris older than 5 minutes on each launch.

When running from a source checkout there is no `remotely-askpass` on PATH, so
`find_askpass_helper()` generates a shim in `~/.remotely/run/`.

### ssh_options semantics

`None` means "not configured" and takes `DEFAULT_SSH_OPTIONS`; an empty list
explicitly means "no extra options". These are distinct and both are persisted.
Auth-derived options are merged *underneath* the host's, so a host option always
wins on the same key. In the host form, blank means defaults and `-` means none.

## Testing

`tests/` uses an autouse fixture that repoints `REMOTELY_HOME` at a temp dir, so
tests can never touch real user data. The tmux tests run against a **real tmux
server** on a private socket rather than a mock, and skip when tmux is absent.
TUI tests drive the app headlessly via Textual's `run_test()` pilot.

Run with `-W error::DeprecationWarning`; libtmux has deprecated aliases
(`set_window_option`, `show_window_option`) that the code deliberately only
falls back to when the modern name is absent.

## Versioning and release

`__version__` in `src/remotely/__init__.py` is the single source of truth;
`pyproject.toml` reads it via `[tool.hatch.version]`. **Do not add a static
`version =` back to `pyproject.toml`** — the release workflow rewrites only the
`__init__.py` line, and a second copy would silently drift.

Pushing a `v*` tag runs `.github/workflows/release.yml`: validate tag → inject
version → test → `uv build` → assert the wheel contains the bundled themes and
`app.tcss` → smoke test the wheel → publish a GitHub release. The wheel-contents
check exists because a wheel missing those data files installs fine and only
fails at runtime.

CI (`.github/workflows/ci.yml`) runs the suite on Linux and macOS across Python
3.11–3.13, installing tmux on the runner first so the tmux tests actually
execute rather than skipping.
