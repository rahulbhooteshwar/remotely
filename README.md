# Remotely

[![CI](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml/badge.svg)](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rahulbhooteshwar/remotely?sort=semver)](https://github.com/rahulbhooteshwar/remotely/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Terminal based SSH hosts and connection manager. One binary, zero dependencies.**

`remotely` opens a full-screen terminal UI that lists your saved SSH hosts by
group, connects to them in tabs, and colours each session by theme so you always
know whether you are on production.

It needs **nothing installed**. No Python, no uv, no tmux, no OpenSSH, no
sshpass. It carries its own SSH client and its own terminal emulator.

```
remotely
```

---

## Why it exists

It succeeds [Connectify](https://github.com/rahulbhooteshwar/connectify-iterm2),
which did the same job but was welded to macOS.

| | Connectify | Remotely |
|---|---|---|
| Tabs | iTerm2 via AppleScript | built-in terminal emulator |
| SSH | system `ssh` + `sshpass` | built-in SSH client |
| Themes | iTerm2 profiles | own `.toml` theme files |
| Secrets | macOS Keychain | encrypted vault, one passcode |
| UI | web UI on a background server | terminal UI, no server |
| Install | PyInstaller bundle | single binary |
| Platform | macOS only | macOS and Linux |

## Requirements

None. Run `remotely --doctor` to confirm.

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
```

Downloads a prebuilt binary for your platform, verifies its checksum, drops it
in `~/.local/bin`, and clears macOS quarantine. Nothing else is installed.

Or grab a binary from the [releases page](https://github.com/rahulbhooteshwar/remotely/releases),
`chmod +x` it, and put it on your `PATH`.

<details>
<summary>Installing from source instead</summary>

```bash
uv tool install git+https://github.com/rahulbhooteshwar/remotely
```

</details>

### Uninstall

```bash
rm ~/.local/bin/remotely          # the binary
rm -rf ~/.remotely                # configuration and vault, if you want them gone
```

## End to end, first run

From nothing to a themed production session:

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
remotely
```

Everything else happens in the command bar:

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
enter                  -> connects
```

What happens on that last keystroke:

1. The host uses a credential, so the vault is unlocked (once per run) and
   `ldap` is decrypted in memory.
2. The built-in SSH client connects and authenticates. **The password is passed
   directly into the SSH handshake** - it is never written to disk, never put in
   an environment variable, never placed on a command line, and no helper
   process ever sees it.
3. A new tab opens running the session in the built-in terminal emulator,
   painted with the `prod` theme: red background, red border, `🔴` in the tab.

`ctrl+w` returns to the launcher, `ctrl+pageup`/`ctrl+pagedown` cycle tabs.
Everything else - including `ctrl+c` and `ctrl+d` - goes to the remote shell.

Point a second host at the same `ldap` credential and it just works. Rotate the
password once in `/vault` and every host that references it follows.

## Using it

### The command bar

The input at the top is the only control surface. It completes as you type.

| Type | What you get |
|---|---|
| anything | fuzzy match across host names, hostnames, users, groups, tags |
| `/` | the command list |
| `@` | filter by tag |
| `#` | filter by group |

### Keys

| Key | Action |
|---|---|
| `Enter` | connect to the selected host |
| `Tab` | accept completion |
| `Ctrl+W` | back to the launcher from a session |
| `Ctrl+PageUp` / `Ctrl+PageDown` | previous / next tab |
| `Ctrl+N` | new host |
| `Ctrl+E` / `Ctrl+D` | edit / delete selected host |
| `Ctrl+T` | themes |
| `Ctrl+K` | vault |
| `Ctrl+L` | open sessions |
| `F1` | help |
| `Ctrl+Q` | quit, closing all sessions |

### Commands

```
/connect <host>   connect to a host        /themes          browse and clone themes
/add              add a host               /vault           manage credentials
/edit <host>      edit a host              /lock            lock the vault
/delete <host>    delete a host            /export <path>   export hosts
/sessions         list open sessions       /import <path>   import hosts
/help  /quit
```

## Configuration

Everything lives in `~/.remotely`:

```
~/.remotely/
├── hosts.json      host definitions - never contains secrets
├── vault.enc       encrypted credentials
├── known_hosts     host keys Remotely has learned
└── themes/         your own themes, override bundled ones by name
```

Because `hosts.json` holds no secrets, it is safe to diff, sync, or commit.
Use `--config DIR` or `REMOTELY_HOME` to point somewhere else.

## Credentials

Secrets live in a single AES-256-GCM encrypted vault unlocked by one passcode.
The key is derived with scrypt (n=2¹⁵) and held only in memory. The envelope
header is authenticated, so tampering with the KDF parameters on disk fails the
tag check rather than weakening the vault.

Credentials are **named and shareable**. Create one called `ldap`, point twenty
hosts at it, rotate it once. A host can equally have its own private credential,
or use `agent` mode and rely on your ssh agent.

Password and key auth are both supported, including key passphrases.

### Host keys

Unknown hosts are learned and recorded in `~/.remotely/known_hosts` (mode
`0600`); your existing `~/.ssh/known_hosts` is read too. A host whose key has
**changed** is refused, which is what you want if someone is between you and the
server. Set `StrictHostKeyChecking=yes` in a host's ssh options to refuse
unknown hosts as well.

## Themes

A theme is a TOML file. Three ship with the app: `prod` (red), `non-prod`
(amber), `personal` (teal). Themes are discovered from the bundled set and from
`~/.remotely/themes/*.toml`, with user files shadowing bundled ones of the same
name. Adding a theme is just dropping in a file.

```toml
name = "staging"
label = "Staging"
description = "Blue-grey"

[colors]
accent     = "#5f87d7"
background = "#101828"
foreground = "#dbe4f5"

[terminal]            # optional, overrides [colors] for the session pane
background = "#0b1220"
border     = "#5f87d7"

[tab]
icon       = "🔵"
ascii_icon = "[S]"     # used when the terminal cannot render the icon
format     = "{icon} {host}"
```

`/themes` clones any theme into your own directory to start from, and `Ctrl+R`
reloads after editing.

## Import / export

```
/export ~/hosts.json          hosts only, no secrets
/export ~/all.json --secrets  hosts plus credentials, vault must be unlocked
/import ~/hosts.json          merge, skipping existing names
/import ~/hosts.json --replace
```

An export containing secrets is written `0600` and warns you on the way out.

## Advanced: using the system ssh

The built-in client covers password, key and agent authentication. For setups it
cannot express - `ProxyCommand`, GSSAPI/Kerberos, unusual certificate configs -
tick **"Use the system ssh binary"** on a host. That host then shells out to your
`ssh`, which must be installed, and cannot use a stored password.

Per-host `ssh_options` are honoured natively where they map (`ConnectTimeout`,
`ServerAliveInterval`, `StrictHostKeyChecking`); anything else applies only when
that host uses system ssh, and the UI tells you so rather than ignoring it
silently.

## What this deliberately does not do

- **No detach/reattach.** Sessions live in the app and close when you quit.
  That is the cost of not depending on tmux. If you need sessions that outlive
  the client, run tmux on the *remote* host.
- **Not a perfect xterm.** The terminal emulator handles colour, cursor
  addressing, alternate screen and scrollback, so shells, `vim`, `htop` and
  friends work - but exotic escape sequences may render imperfectly.

## Development

```bash
uv sync
make run           # run from source
make test          # full suite
make binary        # build the self-contained binary
make doctor
```

The suite stands up a **real SSH server** using paramiko and connects to it, so
authentication, channels, PTY sizing, resize and host-key changes are all
exercised for real rather than mocked. The TUI is driven headlessly through
Textual's pilot. Nothing needs to be installed to run any of it.

CI runs the suite on Linux and macOS across Python 3.11-3.13, builds the binary
for all three targets, and verifies each one starts in a sandbox with `python`,
`uv`, `tmux`, `ssh` and `sshpass` deliberately absent from `PATH`.

## Releasing

`__version__` in `src/remotely/__init__.py` is the single source of truth;
`pyproject.toml` reads it dynamically. The release workflow rewrites it from the
tag.

```bash
git tag v1.0.0
git push origin v1.0.0
```

That builds and verifies binaries for macOS arm64, macOS x86_64 and Linux
x86_64, publishes them with checksums, and attaches a wheel and sdist too.

## Licence

[MIT](LICENSE)
