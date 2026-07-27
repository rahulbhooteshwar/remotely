# Remotely

[![CI](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml/badge.svg)](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rahulbhooteshwar/remotely?sort=semver)](https://github.com/rahulbhooteshwar/remotely/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Terminal based SSH hosts and connection manager.**

One command, one terminal. `remotely` opens a full-screen TUI that lists your
saved SSH hosts by group, launches them into real tabs, and colours each session
according to a theme so you always know whether you are on production.

No web server. No always-on daemon. No platform lock-in.

```
remotely
```

---

## Why it exists

It is the successor to
[Connectify](https://github.com/rahulbhooteshwar/connectify-iterm2), which did
the same job but was welded to macOS: iTerm2 for tabs, AppleScript for
launching, and the Apple Keychain for passwords. Remotely keeps the ideas and
drops the platform:

| | Connectify | Remotely |
|---|---|---|
| Tabs | iTerm2 via AppleScript | tmux via `libtmux` |
| Themes | iTerm2 profiles | own `.toml` theme files |
| Secrets | macOS Keychain | encrypted vault, one passcode |
| UI | web UI on a background server | Textual TUI, no server |
| Commands | `connectify --add`, `--list`, ... | autocompleting palette inside the app |
| Platform | macOS only | macOS, Linux, BSD, WSL |

## Requirements

- Python 3.11+
- `tmux` 3.0+
- `ssh` (OpenSSH 8.4+ recommended, see [Password delivery](#password-delivery))

## Install

### Via the install script (recommended)

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
```

The script installs uv if it is missing, installs Remotely as a uv tool, checks
for `tmux` and `ssh`, and makes sure `~/.local/bin` is on your `PATH`.

### As a uv tool

```bash
uv tool install git+https://github.com/rahulbhooteshwar/remotely
```

Pin a release:

```bash
uv tool install git+https://github.com/rahulbhooteshwar/remotely@v0.1.0
```

Or install a wheel from the [releases page](https://github.com/rahulbhooteshwar/remotely/releases):

```bash
uv tool install ./remotely-0.1.0-py3-none-any.whl
```

### For development

```bash
git clone https://github.com/rahulbhooteshwar/remotely.git
cd remotely
uv sync
uv run remotely
```

### Upgrading and uninstalling

```bash
uv tool upgrade remotely
uv tool uninstall remotely          # keeps ~/.remotely
./uninstall.sh --remove-config      # removes configuration too
```

## End to end, first run

From nothing to a themed production session:

```bash
# 1. install
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh

# 2. check the environment (tmux, ssh, locale, askpass helper)
remotely --doctor

# 3. start it
remotely
```

Remotely re-executes itself inside a tmux session and draws the UI in window 0.
From there, everything happens in the command bar:

```
/vault                 -> create the vault, set the one passcode
                          add a credential named "ldap" with your password
ctrl+n                 -> new host form
                          name      prod-web-01
                          hostname  10.0.0.1
                          username  deploy
                          group     Production
                          tags      web, critical
                          theme     prod
                          auth      credential: ldap
prod                   -> fuzzy search finds it
enter                  -> launches
```

What happens on that last keystroke:

1. The host says `auth_mode = credential`, so the vault is unlocked (once per
   session) and the `ldap` credential is read.
2. `build_plan()` assembles the ssh argv. The password is **not** in it.
3. The password is written to a one-shot `0600` file in `~/.remotely/run/`.
4. A new tmux window opens running ssh, with `SSH_ASKPASS` pointing at the
   `remotely-askpass` helper and `SSH_ASKPASS_REQUIRE=force`.
5. ssh asks the helper for the password. The helper prints it once and unlinks
   the file immediately.
6. The window gets the `prod` theme: red background, red borders, `🔴` in the
   tab title, and a tag identifying it as ours.

You are now in a red-tinted production shell. `ctrl+b 0` returns to Remotely,
`ctrl+b n` cycles tabs, `ctrl+b d` detaches and leaves every session running.

Add a second host pointing at the same `ldap` credential and it just works -
that is what "shared credentials" means here. Rotate the password once in
`/vault` and every host that references it follows.

## Using it

Run `remotely`. The app re-executes itself inside a dedicated tmux session so
that every SSH connection becomes a real tab you can switch between.

### The command bar

The input at the top is the only control surface you need. It completes as you
type, the way Claude Code's input does.

| Type | What you get |
|---|---|
| anything | fuzzy match across host names, hostnames, users, groups, tags |
| `/` | the command list - `/add`, `/edit`, `/delete`, `/themes`, `/vault`, ... |
| `@` | filter by tag |
| `#` | filter by group |

Enter launches the highlighted host in a new themed tab. `Tab` accepts the
current completion.

### Keys

| Key | Action |
|---|---|
| `Enter` | launch selected host |
| `Tab` | accept completion |
| `Up` / `Down` | move through results |
| `Ctrl+N` | new host form |
| `Ctrl+E` | edit selected host |
| `Ctrl+T` | theme browser |
| `Ctrl+K` | vault manager |
| `Ctrl+L` | list open session tabs |
| `F1` | help |
| `Ctrl+Q` | quit Remotely (open sessions keep running) |

### Commands

Every command is reachable from the bar by typing `/`:

```
/add            add a host
/edit <host>    edit a host
/delete <host>  delete a host
/connect <host> launch a host
/themes         browse and clone themes
/vault          manage credentials
/sessions       list and switch to open tabs
/export <path>  export hosts
/import <path>  import hosts
/lock           lock the vault
/help           help
/quit           exit
```

## Configuration

Everything lives in `~/.remotely`:

```
~/.remotely/
├── hosts.json      host definitions - never contains secrets
├── vault.enc       encrypted credentials
├── settings.json   UI preferences
├── themes/         your own themes, override bundled ones by name
└── run/            transient askpass helpers (mode 0700)
```

Because `hosts.json` holds no secrets, it is safe to diff, sync, or commit.

## Credentials

Secrets live in a single AES-256-GCM encrypted vault unlocked by one passcode.
The key is derived with scrypt (n=2^15, r=8, p=1) and held only in memory.

Credentials are **named and shareable**. Create one called `ldap` and point
twenty hosts at it; rotate it once and all twenty follow. A host can equally
have its own private credential, or use `agent` mode and rely on your ssh agent.

Both password and key-based auth are supported. Key credentials can carry a
passphrase, which is delivered the same way a password is.

### Password delivery

Passwords are never placed on a command line or in an environment variable that
survives the launch. Remotely stages the secret in a one-shot file (mode `0600`,
inside a `0700` directory) and points `SSH_ASKPASS` at a helper that reads it
once and immediately unlinks it.

This needs `SSH_ASKPASS_REQUIRE=force`, which is OpenSSH 8.4+ (Sept 2020). On
older ssh the app tells you, and falls back to letting ssh prompt normally.

## Themes

A theme is a TOML file. Three ship with the app:

| Theme | Look |
|---|---|
| `prod` | red, `[PROD]` banner |
| `non-prod` | amber |
| `personal` | teal |

Themes are discovered from the bundled set and from `~/.remotely/themes/*.toml`,
with user files shadowing bundled ones of the same name. Adding a theme is just
dropping in a file - there is no registry to edit. `/themes` will clone any
theme into your own directory to start from.

```toml
name = "staging"
label = "Staging"
description = "Blue-grey"

[colors]
accent = "#5f87d7"

[tmux]
window_style = "bg=#101828,fg=#dbe4f5"
pane_active_border_style = "fg=#5f87d7,bold"
window_status_current_style = "fg=#101828,bg=#5f87d7,bold"

[tab]
icon = "🔵"
format = "{icon} {host}"

[remote]
prompt_color = "1;34"
banner = "STAGING"
```

The `[tmux]` keys map onto tmux window options; unknown keys pass straight
through, so a theme can reach any tmux window option without a code change.

`[remote]` prompt colouring is **opt-in and best-effort** - set
`inject_prompt = true` to have Remotely push a `PS1` into the session after
connect. It is off by default because a remote `.bashrc` will usually overwrite
`PS1` anyway. The tmux pane background and status colours are the reliable
signal, and those always apply.

## A note on locales

tmux needs a UTF-8 locale. Without one it emits format output that `libtmux`
cannot parse, and session creation fails outright - so Remotely sets a UTF-8
`LC_CTYPE` for itself at startup if your environment does not already have one.
Only `LC_CTYPE` is touched; your language, dates and currency are left alone.

Theme icons are a separate, cosmetic matter. tmux stores tab names verbatim, but
a terminal running under a non-UTF-8 locale renders `🔴` as `__`. Themes
therefore carry an `ascii_icon` (`[!]`, `[~]`, `[+]`) used automatically when the
locale cannot display the real one.

`remotely --doctor` reports what it finds.

## Import / export

```
/export ~/hosts.json          hosts only, no secrets
/export ~/all.json --secrets  hosts plus credentials, vault must be unlocked
/import ~/hosts.json          merge, skipping existing names
/import ~/hosts.json --replace
```

An export containing secrets is written `0600` and warns you on the way out.

## Development

```bash
uv sync
make run                  # or: uv run remotely
make test                 # or: uv run pytest -q
make doctor               # environment diagnostics
uv run pytest tests/test_vault.py::test_wrong_passcode_rejected
```

The suite covers the vault, host store, theme loading, ssh command
construction, import/export and the completion engine. The tmux backend is
exercised against a **real tmux server** on a private socket rather than a mock,
and the TUI is driven headlessly through Textual's pilot. tmux tests skip
automatically when tmux is not installed.

CI runs the whole suite on Linux and macOS across Python 3.11, 3.12 and 3.13,
then builds the distribution and smoke tests the resulting wheel.

## Releasing

Versions live in one place: `__version__` in `src/remotely/__init__.py`, which
`pyproject.toml` reads through hatch's dynamic version. The release workflow
rewrites it from the tag, so there is nothing to bump by hand.

```bash
git tag v1.0.0
git push origin v1.0.0
```

That runs [`.github/workflows/release.yml`](.github/workflows/release.yml),
which validates the tag, injects the version, runs the tests, builds the wheel
and sdist, verifies the bundled themes and stylesheet actually made it into the
wheel, smoke tests the built artefact, and publishes a GitHub release.

Publishing to PyPI is wired up but disabled. To enable it, configure trusted
publishing for the repository and remove the `if: false` from the `pypi` job.

## Licence

[MIT](LICENSE)
