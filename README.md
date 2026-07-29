# Remotely

[![CI](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml/badge.svg)](https://github.com/rahulbhooteshwar/remotely/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rahulbhooteshwar/remotely?sort=semver)](https://github.com/rahulbhooteshwar/remotely/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Terminal based SSH hosts and connection manager. Self-contained, zero dependencies.**

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

SSH connection managers usually make you pick a compromise. GUI ones tie you to
a particular terminal emulator and a particular OS. Shell-script wrappers hand
your password to `sshpass`, where it lands in a temp file or in `ps` output.
Anything written in a scripting language expects you to have that language, a
package manager, and a multiplexer already installed.

Remotely takes none of those trades:

| | How |
|---|---|
| **Nothing to install** | Ships its own SSH client, terminal emulator and interpreter |
| **Passwords stay in memory** | Fed straight into the SSH handshake - never on disk, in an env var, or on a command line |
| **Prod looks like prod** | Per-host themes colour the whole session, so a red pane is never staging |
| **One input for everything** | Search, commands, tags and groups all come from the same bar |
| **Credentials are shared, not copied** | One named credential backs many hosts; rotate it once |
| **Runs where you work** | macOS and Linux, x86_64 and arm64, in any terminal, over SSH, in a container |

## Requirements

None. Run `remotely --doctor` to confirm.

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
```

Downloads a prebuilt runtime for your platform, verifies its checksum, unpacks
it into `~/.local/lib/remotely`, links `remotely` into `~/.local/bin`, and
clears macOS quarantine. Nothing else is installed.

Or grab the tarball for your platform from the
[releases page](https://github.com/rahulbhooteshwar/remotely/releases), unpack
it somewhere, and put a link to `remotely/remotely` on your `PATH`.

<details>
<summary>Why a directory rather than one file</summary>

A single-file build has to unpack ~26 MB of shared libraries into a temporary
directory on **every** launch. On macOS each library lands at a fresh path each
time, so Gatekeeper re-verifies all of them on every run and never reuses a
verdict - that alone took startup to about ten seconds. Unpacking once at
install time instead brings it to roughly 100 ms. The libraries still ship with
the tool; nothing extra is required on your machine.

</details>

<details>
<summary>Installing from source instead</summary>

```bash
uv tool install git+https://github.com/rahulbhooteshwar/remotely
```

</details>

### Updating

```bash
remotely --update
```

Checks the latest release, downloads the runtime for your platform, verifies
its SHA-256 and swaps it in place. Restart Remotely afterwards. It says so
and changes nothing when you are already current, and refuses to install
anything whose checksum does not match.

Re-running the install script does the same thing, if you prefer:

```bash
curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
```

Installed from source instead? `uv tool install --force git+https://github.com/rahulbhooteshwar/remotely`

### Uninstall

```bash
rm ~/.local/bin/remotely          # the command
rm -rf ~/.local/lib/remotely      # the runtime
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
ctrl+n                 -> new host form
                          name      prod-web-01
                          hostname  10.0.0.1
                          username  deploy
                          group     Production
                          tags      web, critical
                          theme     prod
                          auth      password based (saved in vault)  <- default
                          password  ••••••••
prod                   -> fuzzy search finds it
enter                  -> connects
```

The password goes straight into the vault as a credential named
`cred-prod-web-01`, and the host is bound to it. You are asked for the vault
passcode on the way - once, the first time. If you would rather name the
credential yourself so other hosts can share it, fill in **Save credential as**.

What happens on that last keystroke:

1. The host uses a credential, so the vault is unlocked (once per run) and
   `ldap` is decrypted in memory.
2. The built-in SSH client connects and authenticates. **The password is passed
   directly into the SSH handshake** - it is never written to disk, never put in
   an environment variable, never placed on a command line, and no helper
   process ever sees it.
3. A new tab opens running the session in the built-in terminal emulator,
   painted with the `prod` theme: red background, red border, and a red tab.

`ctrl+w` returns to the launcher, `ctrl+pageup`/`ctrl+pagedown` cycle tabs.
Everything else - including `ctrl+c` and `ctrl+d` - goes to the remote shell.

Point a second host at the same `ldap` credential and it just works. Rotate the
password once in `/vault` and every host that references it follows.

## Using it

### The launcher

Hosts are grouped into bordered boxes, two rows per host: the name and where it
connects, then its tags. The stripe down the left edge is the host's theme, so
production is obvious without reading anything.

Clicking a tile connects. The icons at its right edge do the rest:

| Icon | Action |
|---|---|
| `⧉` | copy the host name |
| `❯` | copy `user@host` |
| `✎` | edit the host |
| `◉` | view full details |

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
| `Ctrl+Shift+W` | close the current tab (or click the `✕` on the tab) |
| `Shift+PageUp` / `Shift+PageDown` | scroll back through session output |
| `Shift+End` | jump back to the live edge |
| `R` | reconnect a disconnected session (or click **Retry** in the overlay) |
| mouse wheel | scroll the session |
| drag | select session text - copied to the clipboard when you release |
| paste | your terminal's paste shortcut sends the text to the remote |
| `Ctrl+PageUp` / `Ctrl+PageDown` | previous / next tab |
| wheel over the tab row | scroll the tabs (or hold the pointer at either edge) |
| `Ctrl+N` | new host |
| `Ctrl+E` / `Ctrl+D` | edit / delete a host - the one on the current session tab, or the highlighted one on the launcher |
| `Ctrl+T` | themes |
| `Ctrl+K` | vault |
| `Ctrl+L` | open sessions |
| `F1` | help |
| `Ctrl+Q` | quit, closing all sessions |

### Commands

```
/connect <host>   connect to a host        /themes          browse and clone themes
/add              add a host               /vault           manage credentials
/edit <host>      edit a host              /settings        change preferences
/delete <host>    delete a host            /lock            lock the vault
/sessions         switch or close tabs     /export <path>   export hosts
/close [host]     close a tab              /import <path>   import hosts
/help  /quit
```

`/sessions` (or `Ctrl+L`) lists the open tabs. Select one to switch to it, or
use **Close Selected Tab** / **Close All Tabs** to close them without leaving -
the list refreshes and stays open so you can close several in a row. **Ok**
leaves the dialog and touches nothing.

## Configuration

Everything lives in `~/.remotely`:

```
~/.remotely/
├── hosts.json      host definitions - never contains secrets
├── settings.json   preferences: confirmation prompts (/settings) and the
│                   app theme you picked from the command palette
├── vault.enc       encrypted credentials
├── known_hosts     host keys Remotely has learned
└── themes/         your own themes, override bundled ones by name
```

Because `hosts.json` holds no secrets, it is safe to diff, sync, or commit.
Use `--config DIR` or `REMOTELY_HOME` to point somewhere else.

`/settings` toggles things like "confirm before quitting" and "confirm before
closing a tab" - both on by default so a stray `Ctrl+Q` or tab close can't
drop a live session by accident.

## Credentials

Secrets live in a single AES-256-GCM encrypted vault unlocked by one passcode.
The key is derived with scrypt (n=2¹⁵) and held only in memory. The envelope
header is authenticated, so tampering with the KDF parameters on disk fails the
tag check rather than weakening the vault.

Credentials are **named and shareable**. Create one called `ldap`, point twenty
hosts at it, rotate it once. A host can equally have its own private credential,
or use `agent` mode and rely on your ssh agent.

Password and key auth are both supported, including key passphrases.

### Managing them

`/vault` (or `Ctrl+K`) lists every credential with the number of hosts using it.
Select one to edit, **New** to add one, or the `✕` at the row's right edge to
delete it.

- **Deleting is refused while a host still points at the credential.** The
  dialog names the hosts and offers to edit it instead - deleting would leave
  them unable to connect, and the breakage would not surface until you tried.
  An unused credential deletes after a confirmation.
- **Renaming repoints every host automatically**, and the status line says how
  many moved. `hosts.json` stores the credential's name rather than an id, so a
  rename without this would silently dangle each host that shared it.
- **Names must be unique**, case-insensitively, because the vault looks them up
  that way. Saving a name that is already taken is flagged on the form rather
  than quietly replacing the other credential's secret.

### Choosing authentication for a host

The **Authentication** dropdown on the add/edit host form offers, in order:

| Choice | What it does |
| --- | --- |
| `password based (saved in vault)` | Type a password here; it is stored as a new vault credential and the host is bound to it |
| `ssh key based (saved in vault)` | Same, for a key path and optional passphrase |
| `credential: <name>` | Reuse a credential already in the vault |
| `ssh agent / default keys` | Store nothing - authenticate with your running ssh agent (`SSH_AUTH_SOCK`) and the default `~/.ssh/id_*` keys |

The first two are shortcuts for "I do not want to visit `/vault` first". Saving
prompts for the vault passcode if the vault is locked, and if you cancel that
prompt neither the credential nor the host is written - so a host can never end
up pointing at a credential that does not exist.

A **new** host opens on `password based`, with the password field ready below
it. **Editing** never second-guesses a host: the dropdown opens on whatever that
host authenticates with today.

The optional **Save credential as** field names the new entry so other hosts can
share it. Leave it blank and the name is derived from the host: `cred-prod-web`,
then `cred-prod-web-1`, `cred-prod-web-2` if that is taken. Naming an entry that
already exists is refused rather than overwritten.

Editing a host works the same way: the dropdown opens on whatever credential the
host uses now, and picking one of the first two options rotates it onto a brand
new vault entry, leaving the old one alone.

`ssh agent / default keys` is the only option that depends on machine-level
setup rather than anything Remotely stores. Note that the built-in client does
not read `~/.ssh/config` - if you rely on `IdentityFile`, `ProxyJump` or host
aliases from there, turn on **use system ssh** for that host.

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
icon       = "🔵"       # shown in the launcher list; tabs use the accent colour
ascii_icon = "[S]"     # used when the terminal cannot render the icon
format     = "{icon} {host}"
```

`/themes` clones any theme into your own directory to start from, and `Ctrl+R`
reloads after editing.

## Import / export

```
/export ~/hosts.json            hosts only, no secrets
/export ~/hosts.json --secrets  include credentials (vault must be unlocked)
/import ~/hosts.json            merge in, skipping names you already have
/import ~/hosts.json --replace  replace everything
```

The path completes as you type, and does not have to exist yet. Run `/export`
with no path to be prompted with a sensible default instead.


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

### Known limitation: emoji in form fields

**Paste emoji rather than using the macOS emoji picker (⌃⌘Space).** Pasting
works everywhere; the picker inserts the wrong character.

Remotely uses the Kitty keyboard protocol, which is what lets it tell
`Ctrl+Shift+W` apart from `Ctrl+W`. Under that protocol the terminal reports a
key code plus the text it produced. The emoji picker inserts text without a
real keypress, so the terminal reports a bare key code with no associated
text - and the app has no choice but to take the key code at face value,
yielding a letter instead of the emoji. The same applies to input methods that
compose characters, such as CJK IMEs.

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

That builds and verifies binaries for macOS arm64, Linux x86_64 and Linux
arm64, publishes them with checksums, and attaches a wheel and sdist too.

Every target needs its own runner: PyInstaller freezes the host interpreter and
its native extensions, so it cannot cross-compile.

**Intel macOS has no prebuilt binary.** It can be built on Apple Silicon under
Rosetta as far as the interpreter, but `cryptography` - the library that
encrypts the vault - publishes arm64-only macOS wheels, so the x86_64 build
falls back to compiling it from Rust source. Shipping that from an ad-hoc
cross-build is not a trade worth making. Intel Mac users install from source:

```bash
xcode-select --install
uv tool install git+https://github.com/rahulbhooteshwar/remotely
```

## Licence

[MIT](LICENSE)
