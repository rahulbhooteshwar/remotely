#!/bin/sh
# Remotely uninstaller.
#
#   ./uninstall.sh                    remove the tool, keep ~/.remotely
#   ./uninstall.sh --remove-config    remove the tool and all configuration

set -eu

REMOVE_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --remove-config) REMOVE_CONFIG=1 ;;
        -h|--help)
            printf 'Usage: %s [--remove-config]\n' "$0"
            exit 0
            ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 1 ;;
    esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    GREEN=$(printf '\033[0;32m'); YELLOW=$(printf '\033[1;33m'); NC=$(printf '\033[0m')
else
    GREEN=''; YELLOW=''; NC=''
fi
ok()   { printf '%s ok%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '%s  !%s %s\n' "$YELLOW" "$NC" "$1"; }

printf '\nUninstalling Remotely\n\n'

REMOVED=0

# Binary install (the default).
for dir in "$HOME/.local/bin" /usr/local/bin; do
    if [ -f "$dir/remotely" ]; then
        rm -f "$dir/remotely"
        ok "Removed $dir/remotely"
        REMOVED=1
    fi
done

# Installed from source as a uv tool instead.
if command -v uv >/dev/null 2>&1; then
    if uv tool uninstall remotely >/dev/null 2>&1; then
        ok "Removed the remotely uv tool"
        REMOVED=1
    fi
fi

[ "$REMOVED" -eq 1 ] || warn "No installed copy of remotely was found"

CONFIG_DIR="$HOME/.remotely"
if [ "$REMOVE_CONFIG" -eq 1 ]; then
    if [ -d "$CONFIG_DIR" ]; then
        rm -rf "$CONFIG_DIR"
        ok "Removed $CONFIG_DIR"
    fi
else
    if [ -d "$CONFIG_DIR" ]; then
        printf '\n'
        warn "Configuration kept at $CONFIG_DIR"
        printf '     It contains your hosts and your encrypted vault.\n'
        printf '     Remove it with: rm -rf %s\n' "$CONFIG_DIR"
    fi
fi

printf '\nDone.\n\n'
