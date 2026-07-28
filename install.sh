#!/bin/sh
# Remotely installer - terminal based SSH hosts and connection manager.
#
#   curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/remotely/main/install.sh | sh
#
# Downloads a prebuilt, self-contained binary. Nothing else is installed and
# nothing else is required: no Python, no uv, no tmux, no OpenSSH.
#
# Environment overrides:
#   REMOTELY_REPO      owner/name to download from  (default rahulbhooteshwar/remotely)
#   REMOTELY_VERSION   tag to install, e.g. v1.0.0  (default: latest release)
#   REMOTELY_BIN_DIR   where the command goes       (default ~/.local/bin)
#   REMOTELY_LIB_DIR   where the runtime goes       (default ~/.local/lib/remotely)

set -eu

REPO="${REMOTELY_REPO:-rahulbhooteshwar/remotely}"
BIN_DIR="${REMOTELY_BIN_DIR:-$HOME/.local/bin}"
LIB_DIR="${REMOTELY_LIB_DIR:-$HOME/.local/lib/remotely}"
VERSION="${REMOTELY_VERSION:-}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED=$(printf '\033[0;31m'); GREEN=$(printf '\033[0;32m')
    YELLOW=$(printf '\033[1;33m'); BLUE=$(printf '\033[0;34m')
    BOLD=$(printf '\033[1m'); NC=$(printf '\033[0m')
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi

info() { printf '%s->%s %s\n' "$BLUE" "$NC" "$1"; }
ok()   { printf '%s ok%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '%s  !%s %s\n' "$YELLOW" "$NC" "$1"; }
err()  { printf '%s  x%s %s\n' "$RED" "$NC" "$1" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
fail() { err "$1"; exit 1; }

printf '\n%sRemotely%s - terminal based SSH hosts and connection manager\n\n' "$BOLD" "$NC"

# ---------------------------------------------------------------- platform

OS=$(uname -s)
ARCH=$(uname -m)

case "$OS" in
    Darwin)
        case "$ARCH" in
            arm64|aarch64) TARGET="macos-arm64" ;;
            x86_64)
                # No prebuilt Intel macOS binary: `cryptography`, which encrypts
                # the vault, publishes arm64-only macOS wheels, so an x86_64
                # build would have to compile it from source anyway.
                fail "No prebuilt binary for Intel macOS.

     Install from source instead (needs Xcode command line tools):
       xcode-select --install
       curl -LsSf https://astral.sh/uv/install.sh | sh
       uv tool install git+https://github.com/$REPO

     Apple Silicon Macs get a prebuilt binary from this script."
                ;;
            *) fail "Unsupported macOS architecture: $ARCH" ;;
        esac
        ;;
    Linux)
        case "$ARCH" in
            x86_64|amd64)  TARGET="linux-x86_64" ;;
            aarch64|arm64) TARGET="linux-arm64" ;;
            *) fail "Unsupported Linux architecture: $ARCH. Install from source:
     uv tool install git+https://github.com/$REPO" ;;
        esac
        ;;
    *)
        fail "Unsupported platform: $OS. Install from source:
     uv tool install git+https://github.com/$REPO"
        ;;
esac
ok "Platform: $OS $ARCH -> $TARGET"

have curl || have wget || fail "curl or wget is required."

download() {
    # $1 = url, $2 = destination
    if have curl; then
        curl -fsSL "$1" -o "$2"
    else
        wget -qO "$2" "$1"
    fi
}

fetch() {
    if have curl; then
        curl -fsSL "$1"
    else
        wget -qO- "$1"
    fi
}

# ----------------------------------------------------------------- version

if [ -z "$VERSION" ]; then
    info "Looking up the latest release..."
    VERSION=$(fetch "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
    [ -n "$VERSION" ] || fail "Could not determine the latest release. Set REMOTELY_VERSION=vX.Y.Z."
fi
ok "Version: $VERSION"

ARCHIVE="remotely-${TARGET}.tar.gz"
URL="https://github.com/$REPO/releases/download/$VERSION/$ARCHIVE"

# ---------------------------------------------------------------- download

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t remotely)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

info "Downloading $ARCHIVE..."
download "$URL" "$TMP/$ARCHIVE" || fail "Download failed: $URL"

# Verify the checksum when the release publishes one.
if download "$URL.sha256" "$TMP/$ARCHIVE.sha256" 2>/dev/null; then
    EXPECTED=$(awk '{print $1}' "$TMP/$ARCHIVE.sha256")
    if have shasum; then
        ACTUAL=$(shasum -a 256 "$TMP/$ARCHIVE" | awk '{print $1}')
    elif have sha256sum; then
        ACTUAL=$(sha256sum "$TMP/$ARCHIVE" | awk '{print $1}')
    else
        ACTUAL=""
    fi
    if [ -n "$ACTUAL" ]; then
        [ "$EXPECTED" = "$ACTUAL" ] || fail "Checksum mismatch. Expected $EXPECTED, got $ACTUAL."
        ok "Checksum verified"
    else
        warn "No sha256 tool available, skipping verification"
    fi
else
    warn "No published checksum for this release, skipping verification"
fi

tar -xzf "$TMP/$ARCHIVE" -C "$TMP" || fail "Could not extract $ARCHIVE"
[ -x "$TMP/remotely/remotely" ] || fail "Archive did not contain a 'remotely' runtime"

# ----------------------------------------------------------------- install

# The runtime is a directory, not a single file. Unpacking it once here is the
# whole point: the alternative (a onefile binary) re-extracts ~26MB of shared
# libraries on every launch, which on macOS makes Gatekeeper re-verify all of
# them every time and costs about ten seconds of startup.
mkdir -p "$BIN_DIR" "$(dirname "$LIB_DIR")"

# Swap the tree in rather than writing over it in place: a running copy keeps
# the old inodes and carries on working, and a failed download never leaves a
# half-replaced install behind.
rm -rf "$LIB_DIR.old"
if [ -e "$LIB_DIR" ]; then
    mv "$LIB_DIR" "$LIB_DIR.old" || fail "Could not move the previous install aside"
fi
if mv "$TMP/remotely" "$LIB_DIR"; then
    rm -rf "$LIB_DIR.old"
else
    # Put it back rather than leaving the user with nothing.
    [ -e "$LIB_DIR.old" ] && mv "$LIB_DIR.old" "$LIB_DIR"
    fail "Could not install into $LIB_DIR"
fi
chmod 755 "$LIB_DIR/remotely"

# A symlink, so `remotely` on PATH always points at the current runtime and an
# update never has to touch BIN_DIR.
ln -sf "$LIB_DIR/remotely" "$BIN_DIR/remotely"
ok "Installed to $LIB_DIR (linked from $BIN_DIR/remotely)"

# macOS quarantines anything downloaded; clear it so the binary just runs.
# The whole tree, not just the entry point - every bundled library is checked.
if [ "$OS" = "Darwin" ] && have xattr; then
    xattr -dr com.apple.quarantine "$LIB_DIR" 2>/dev/null || true
fi

# -------------------------------------------------------------------- PATH

if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
    warn "$BIN_DIR is not on your PATH."
    case "${SHELL:-}" in
        */zsh)  PROFILE="$HOME/.zshrc" ;;
        */bash) if [ "$OS" = "Darwin" ]; then PROFILE="$HOME/.bash_profile"; else PROFILE="$HOME/.bashrc"; fi ;;
        */fish) PROFILE="$HOME/.config/fish/config.fish" ;;
        *)      PROFILE="" ;;
    esac
    if [ -n "$PROFILE" ]; then
        info "Add it with:"
        if [ "${PROFILE##*/}" = "config.fish" ]; then
            printf "     echo 'fish_add_path %s' >> %s\n" "$BIN_DIR" "$PROFILE"
        else
            printf "     echo 'export PATH=\"%s:\$PATH\"' >> %s\n" "$BIN_DIR" "$PROFILE"
        fi
        printf '     exec %s\n' "${SHELL:-sh}"
    fi
else
    ok "$BIN_DIR is on your PATH"
fi

# ------------------------------------------------------------------ verify

printf '\n'
if "$BIN_DIR/remotely" --version >/dev/null 2>&1; then
    ok "$("$BIN_DIR/remotely" --version)"
    printf '\n%sRun it:%s   remotely\n' "$BOLD" "$NC"
    printf '%sCheck it:%s remotely --doctor\n' "$BOLD" "$NC"
else
    fail "The binary was installed but will not run. Please open an issue."
fi

printf '\nConfig lives in ~/.remotely\n'
printf 'Uninstall with: rm %s/remotely && rm -rf %s\n\n' "$BIN_DIR" "$LIB_DIR"
