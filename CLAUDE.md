# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Remotely is a terminal SSH connection manager that ships as **one binary with
zero runtime dependencies** — no Python, no uv, no tmux, no OpenSSH, no sshpass.
It carries its own SSH client (paramiko) and its own terminal emulator (pyte),
and PyInstaller bundles the interpreter.

**The zero-dependency property is the product.** Before adding anything that
shells out to an external binary, or a dependency that needs a compiler or a
system library at runtime, check it survives `make binary` and the hermetic
sandbox check in CI.

## Commands

```bash
make setup                 # uv sync
make run                   # run from source
make test                  # uv run pytest -q
make binary                # PyInstaller -> ./dist/remotely
make doctor

uv run pytest tests/test_transport.py::test_password_auth_connects   # single test
REMOTELY_HOME=/tmp/rh uv run remotely                                # throwaway config
```

`uv tool install --force .` can serve a **stale cached wheel**; use
`uv cache clean remotely && uv tool install --reinstall --force .`.

## Architecture

Layered, TUI at the top, no upward dependencies:

- **`models.py`** — `Host` and `Credential`. A `Host` never holds a secret; it
  references a vault credential by name. That is what keeps `hosts.json` safe to
  export and commit.
- **`store.py`** — `hosts.json` CRUD, atomic writes, grouping. Keyed by `name`,
  case-insensitively.
- **`vault.py`** — one encrypted file, one passcode. scrypt → AES-256-GCM, with
  the envelope header authenticated as AAD so weakening the KDF params on disk
  fails the tag check rather than downgrading security.
- **`themes.py`** — TOML themes from the bundled package plus
  `~/.remotely/themes/`, user files shadowing bundled ones by name.
- **`transport.py`** — `ParamikoTransport` (default, in-process) and
  `SystemSSHTransport` (opt-in per host, local PTY around the system `ssh`).
  Both satisfy the same `Transport` protocol: read/write/resize/close.
- **`terminal.py`** — pyte wrapper plus key-event → terminal-byte encoding. No
  Textual imports, so escape-sequence handling is testable standalone.
- **`sessions.py`** — `Session` (transport + emulator + status) and
  `SessionManager`. No Textual imports either.
- **`completion.py`** — command-bar parsing and fuzzy ranking. Also Textual-free.
- **`settings.py`** — user preferences (`~/.remotely/settings.json`), separate
  from `hosts.json` so a corrupt or unrecognised preference can never take the
  host list down with it — anything unreadable falls back to defaults. Toggled
  from `/settings`.
- **`tui/`** — the Textual app, the `TerminalPane` widget, modal screens.

### How a session works

Connecting is threaded end to end because none of it may block the UI:

1. `SessionManager.open()` returns immediately with status `connecting` and does
   the handshake on a background thread — DNS, TCP and auth can take seconds.
2. On success a **reader thread** polls `transport.read()` every 20 ms and
   appends bytes to a buffer under a lock.
3. `TerminalPane` drains that buffer on a **30 fps timer**, not on arrival. A
   chatty remote command would otherwise trigger one repaint per packet.
4. Rendering goes through `render_line`, not `render`, so Textual repaints only
   the rows that changed. Styles are cached — a screen is thousands of cells but
   only a handful of distinct styles.

pyte reports a cell the remote never explicitly coloured as `"default"`, which
`terminal.py:resolve_color` turns into `None`. Textual does **not** backfill a
`None` segment background from the widget's CSS for content lines drawn via
`render_line` — it is only filled in for padding/border rows and the strip's
own overflow. Leaving `bgcolor=None` therefore emits no colour code at all,
and whatever terminal Remotely happens to be running inside shows its own
default background through the gap instead of the theme's. `TerminalPane`
resolves `None` fg/bg to the theme's colours (via `session.theme.pane_styles()`)
in `_style_for` and every `Strip.blank`/`adjust_cell_length` call — don't
reintroduce a bare `Strip.blank(width)` or an un-defaulted `Style()` in that
file, or the background leak comes back.

The same bypass costs the **selection highlight**. Textual paints a drag
selection inside `Visual.to_strips`, which only runs for widgets rendering via
`render()`/Visual. Painting via `render_line` skips it, so `screen.selections`
and `get_selection()` are correct while not one cell on screen changes —
indistinguishable from selection being broken, and the reason it was reported
broken twice. `TerminalPane.render_line` applies the highlight itself from
`Selection.get_span(y)`. It deliberately takes only the *background* of
`screen--selection`: Textual's default resolves foreground and background to
the same colour (`#064273 on #064273`), so applying both paints an unreadable
block.

Copy happens on mouse-up (`events.TextSelected` → `copy_to_clipboard`), not via
Textual's `ctrl+c` binding, because `ctrl+c` has to reach the remote shell.

### Subclassing Textual's Tab

`Tab._on_click(self)` takes **no event argument**. Textual walks the MRO and
invokes the base handler itself, so `SessionTab._on_click` must never call
`super()._on_click(event)` — that raises `TypeError: takes 1 positional
argument but 2 were given` on every tab click. `event.prevent_default()` breaks
the MRO walk, which is how a hit on the ✕ suppresses "select this tab".

### Kitty keyboard protocol vs. composed input (known, accepted)

Textual enables Kitty's `DISAMBIGUATE_ESCAPE_CODES | REPORT_ALL_KEYS |
REPORT_ASSOCIATED_TEXT`. Where a terminal sends the associated-text field
(`CSI <keycode>;<mods>;<text> u`) the character is correct. Where it sends a
bare `CSI <keycode> u`, Textual falls back to `chr(keycode)` — correct for a
real keypress, wrong for text inserted by an input method. The macOS emoji
picker hits exactly this: it reports keycode 97 with no text, so an emoji
arrives as `a`. Verified against `textual._xterm_parser`, not just inferred
from the symptom.

Turning the protocol off (`TEXTUAL_DISABLE_KITTY_KEY=1`) fixes it but makes
`ctrl+shift+w` indistinguishable from `ctrl+w` — both parse as `ctrl+w` — which
would silently kill the close-tab binding. Deliberate call: keep the protocol,
document paste as the route for emoji. Don't "fix" this by disabling Kitty
without also rebinding close-tab.

### Tab titles and shortcut context

A tab is named after the **host record**, never the remote's OSC title. Shells
set that to whatever they like ("user@host", the running command), so honouring
it made the tab row mean different things per server. `TerminalPane` therefore
does not emit a title message at all — don't reintroduce one.

`_context_host()` is what `ctrl+e` / `ctrl+d` act on: the host behind the
**current session tab** when one is showing, else the launcher's highlighted
row. On a session tab the launcher list is off screen, so acting on its
highlight edits something invisible to the user.

### Chrome that belongs to the launcher only

`#banner` (version / host count / vault / session count) is hidden whenever a
session tab is active, via `_sync_banner_visibility()` — called from both
`_switch_to()` and the `Tabs.TabActivated` handler, which are the only two ways
the visible tab changes. A successful connect deliberately writes **no** status
line; only `session.notes` (warnings) surface there.

### Theme icons vs. tab colour

The theme's emoji marks hosts in the **launcher list only**. Tabs convey the
theme through their own colour (accent background, dark text) — a glyph there
is at the mercy of the terminal's emoji support, a colour is not.

### Password handling

Passwords go **straight into the SSH handshake in-process**. They are never
written to disk, never put in an environment variable, never placed on a command
line, and no helper process ever sees them. There is deliberately no askpass
file, no temp file and no sshpass.

This is why `SystemSSHTransport` refuses a stored password rather than trying to
smuggle it across a process boundary — reintroducing that path would undo the
main security gain of the rewrite.

### Keys the terminal must not swallow

`tui/terminal.py:RESERVED_KEYS` is the escape hatch: `ctrl+w`, `ctrl+q`, `f1`
and the tab-switch keys never reach the remote, so the user can always get back
to the launcher. **Everything else must pass through**, including `ctrl+c` and
`ctrl+d` — intercepting those would break the shell.

### ssh_options semantics

`None` means "not configured" and takes defaults; an empty list explicitly means
"no extra options". Both are persisted and distinct. `build_spec()` natively
honours `ConnectTimeout`, `ServerAliveInterval` and `StrictHostKeyChecking`;
anything else is recorded in `spec.notes` and surfaced in the UI as
system-ssh-only rather than being silently dropped.

### Host keys

`~/.ssh/known_hosts` is loaded read-only; `~/.remotely/known_hosts` (mode 0600)
is the writable store. Policy is `accept-new`: unknown hosts are learned, but a
**changed** key raises. `StrictHostKeyChecking=yes` on a host switches to
`RejectPolicy`. There is a test for the changed-key case — keep it.

## Testing

`tests/conftest.py` repoints `REMOTELY_HOME` at a temp dir for every test, so
tests can never touch real user data.

`tests/sshserver.py` is a **real SSH server** built on paramiko's server side.
Transport, session and TUI tests connect to it over a real socket with a real
key exchange and a real channel. Do not replace it with mocks — a bundled SSH
client is only trustworthy if it is tested against an actual handshake. It also
caught a genuine bug: paramiko 5.0 removed `DSSKey`, which would have broken all
key auth.

Key generation is slow, so `host_key` is a module-scoped fixture.

Run with `-W error::DeprecationWarning`.

## Packaging

`remotely.spec` bundles the interpreter and dependencies. Two things there are
load-bearing:

- **`hiddenimports`** — paramiko selects key and cipher backends dynamically, so
  the import graph does not reach them; `collect_submodules("paramiko")` plus the
  explicit `cryptography`/`nacl` entries are required. Textual and Rich need
  `collect_all` for their runtime data files.
- **`src/remotely/__main__.py` uses an absolute import.** PyInstaller runs it as
  a top-level script with no package context, so a relative import fails in the
  bundle while working fine from source. Do not "tidy" it back to relative.

## Self-update

`updater.py` implements `remotely --update`: resolve the latest release,
download this platform's artefact, verify its SHA-256, and `os.replace` over
`sys.executable`. Replacing a running binary is safe on Unix because rename
unlinks the old inode, which stays alive until the process exits; staging in
the destination directory keeps the rename on one filesystem so it is atomic.

Two invariants have tests and should keep them: a checksum mismatch must leave
the installed binary untouched, and version comparison must be numeric (a
string compare puts 1.9.0 above 1.10.0).

It uses only the standard library — a HTTP client for one occasional download
would be weight in every binary.

## Versioning and release

`__version__` in `src/remotely/__init__.py` is the single source of truth;
`pyproject.toml` reads it via `[tool.hatch.version]`. **Do not add a static
`version =` back to `pyproject.toml`** — the release workflow rewrites only the
`__init__.py` line, and a second copy would drift.

Pushing a `v*` tag runs `.github/workflows/release.yml`: validate tag → test →
build binaries for macOS arm64, Linux x86_64 and Linux arm64 → verify each one
runs in a sandbox with `python`, `uv`, `tmux`, `ssh` and `sshpass` absent from
`PATH` → publish with checksums, plus a wheel and sdist.

That sandbox check is the one that matters. A binary that only works because the
build machine happens to have something installed is the exact failure this
design exists to prevent.
