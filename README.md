# Remotely

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

It is the successor to [Connectify](../README.md), which did the same job but
was welded to macOS: iTerm2 for tabs, AppleScript for launching, and the Apple
Keychain for passwords. Remotely keeps the ideas and drops the platform:

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

### As a uv tool (recommended)

```bash
uv tool install remotely
```

From a checkout:

```bash
uv tool install ./remotely
```

### Via the install script

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/connectify-iterm2/main/remotely/install.sh | sh
```

The script installs uv if it is missing, installs Remotely as a uv tool, checks
for `tmux` and `ssh`, and makes sure `~/.local/bin` is on your `PATH`.

### For development

```bash
cd remotely
uv sync
uv run remotely
```

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
cd remotely
uv sync
uv run remotely           # run it
uv run pytest             # test it
uv run pytest tests/test_vault.py::test_wrong_passcode_rejected
```

The test suite covers the vault, host store, theme loading, ssh command
construction, and the completion engine. It runs anywhere - no tmux or ssh
server needed, since the tmux backend is exercised through a fake.

## Licence

MIT
