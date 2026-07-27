#!/bin/sh
# Remotely installer - terminal based SSH hosts and connection manager.
#
#   curl -LsSf https://raw.githubusercontent.com/rahulbhooteshwar/connectify-iterm2/main/remotely/install.sh | sh
#
# Installs Remotely as a uv tool. POSIX sh on purpose so it runs under
# sh, bash, dash and zsh without modification.

set -eu

REPO="${REMOTELY_REPO:-rahulbhooteshwar/connectify-iterm2}"
BRANCH="${REMOTELY_BRANCH:-main}"
SUBDIR="remotely"
SPEC="${REMOTELY_SPEC:-git+https://github.com/${REPO}@${BRANCH}#subdirectory=${SUBDIR}}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED=$(printf '\033[0;31m'); GREEN=$(printf '\033[0;32m')
    YELLOW=$(printf '\033[1;33m'); BLUE=$(printf '\033[0;34m')
    BOLD=$(printf '\033[1m'); NC=$(printf '\033[0m')
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; NC=''
fi

info()    { printf '%s->%s %s\n' "$BLUE" "$NC" "$1"; }
ok()      { printf '%s ok%s %s\n' "$GREEN" "$NC" "$1"; }
warn()    { printf '%s  !%s %s\n' "$YELLOW" "$NC" "$1"; }
err()     { printf '%s  x%s %s\n' "$RED" "$NC" "$1" >&2; }
have()    { command -v "$1" >/dev/null 2>&1; }

fail() { err "$1"; exit 1; }

printf '\n%sRemotely%s - terminal based SSH hosts and connection manager\n\n' "$BOLD" "$NC"

# ---------------------------------------------------------------- platform

OS=$(uname -s)
case "$OS" in
    Darwin) PLATFORM="macOS" ;;
    Linux)  PLATFORM="Linux" ;;
    *)      PLATFORM="$OS"; warn "Untested platform: $OS" ;;
esac
ok "Platform: $PLATFORM $(uname -m)"

# ------------------------------------------------------------ dependencies

install_hint() {
    # $1 = package name
    if [ "$OS" = "Darwin" ]; then
        printf '     brew install %s\n' "$1"
    elif have apt-get; then
        printf '     sudo apt-get install -y %s\n' "$1"
    elif have dnf; then
        printf '     sudo dnf install -y %s\n' "$1"
    elif have pacman; then
        printf '     sudo pacman -S %s\n' "$1"
    else
        printf '     install %s with your package manager\n' "$1"
    fi
}

MISSING=0

if have tmux; then
    ok "tmux: $(tmux -V 2>/dev/null || echo present)"
else
    err "tmux not found - Remotely uses it for session tabs."
    install_hint tmux
    MISSING=1
fi

if have ssh; then
    SSH_VERSION=$(ssh -V 2>&1 | cut -d, -f1)
    ok "ssh: $SSH_VERSION"
    # SSH_ASKPASS_REQUIRE, needed to hand stored passwords to ssh, is 8.4+.
    SSH_MAJOR=$(printf '%s' "$SSH_VERSION" | sed -n 's/^OpenSSH_\([0-9]*\).*/\1/p')
    SSH_MINOR=$(printf '%s' "$SSH_VERSION" | sed -n 's/^OpenSSH_[0-9]*\.\([0-9]*\).*/\1/p')
    if [ -n "$SSH_MAJOR" ] && [ -n "$SSH_MINOR" ]; then
        if [ "$SSH_MAJOR" -lt 8 ] || { [ "$SSH_MAJOR" -eq 8 ] && [ "$SSH_MINOR" -lt 4 ]; }; then
            warn "OpenSSH < 8.4: stored passwords cannot be auto-supplied; ssh will prompt."
        fi
    fi
else
    err "ssh not found."
    install_hint openssh-client
    MISSING=1
fi

[ "$MISSING" -eq 0 ] || fail "Install the missing dependencies above, then re-run this script."

# --------------------------------------------------------------------- uv

if have uv; then
    ok "uv: $(uv --version)"
else
    info "uv not found, installing it..."
    if ! have curl; then fail "curl is required to install uv."; fi
    curl -LsSf https://astral.sh/uv/install.sh | sh || fail "Could not install uv."
    # uv lands in one of these depending on version.
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [ -d "$d" ] && PATH="$d:$PATH"
    done
    export PATH
    have uv || fail "uv installed but not on PATH. Open a new shell and re-run."
    ok "uv installed"
fi

# ----------------------------------------------------------------- install

# Running from a checkout? Install that instead of fetching from GitHub.
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    SPEC="$SCRIPT_DIR"
    info "Installing from local checkout: $SPEC"
else
    info "Installing from $SPEC"
fi

if uv tool install --force "$SPEC"; then
    ok "Remotely installed"
else
    fail "Installation failed."
fi

# -------------------------------------------------------------------- PATH

BIN_DIR="$HOME/.local/bin"
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

# ------------------------------------------------------------------- verify

if have remotely; then
    ok "$(remotely --version)"
    printf '\n%sRun it:%s  remotely\n' "$BOLD" "$NC"
    printf '%sCheck it:%s remotely --doctor\n\n' "$BOLD" "$NC"
else
    printf '\n'
    warn "Installed, but 'remotely' is not on PATH yet."
    printf '     Try: %s/remotely\n\n' "$BIN_DIR"
fi

printf 'Config will live in ~/.remotely\n'
printf 'Uninstall with: uv tool uninstall remotely\n\n'
