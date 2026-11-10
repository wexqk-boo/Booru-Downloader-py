#!/usr/bin/env bash
# Install bdl: put the console entry point on PATH.
#
#   ./install.sh          — symlink the venv entry point into ~/.local/bin
#   ./install.sh --copy   — copy it instead of symlinking
#   ./install.sh --remove — remove what was installed (the project itself stays)
#
# `bdl` is a plain executable now. On a terminal it opens the wizard; without a
# terminal it prints usage and exits 2, so scripts never block on a prompt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="$ROOT/.venv/bin/bdl"
BIN_DIR="$HOME/.local/bin"
DST_BIN="$BIN_DIR/bdl"
FISH_FN="${XDG_CONFIG_HOME:-$HOME/.config}/fish/functions/bdl.fish"

c_ok=$'\033[32m'; c_err=$'\033[31m'; c_dim=$'\033[2m'; c_reset=$'\033[0m'

die() { echo "${c_err}✗ $*${c_reset}" >&2; exit 1; }
ok()  { echo "${c_ok}✓${c_reset} $*"; }

# Resolve the entry point. The venv is authoritative; a package installed
# system-wide is the fallback.
engine=""
if [ -x "$VENV_BIN" ]; then
    engine="$VENV_BIN"
elif command -v bdl >/dev/null 2>&1; then
    engine="$(readlink -f "$(command -v bdl)")"
fi

if [ "${1:-}" = "--remove" ]; then
    # Drop the PATH entry only when it is our own symlink into the project; a
    # foreign command with the same name is left alone.
    for dst in "$DST_BIN"; do
        if [ -L "$dst" ]; then
            case "$(readlink -f "$dst")" in
                "$ROOT"/*) rm -f "$dst"; ok "removed $dst" ;;
                *) echo "${c_dim}— $dst points outside the project, keeping it${c_reset}" ;;
            esac
        elif [ -f "$dst" ]; then
            rm -f "$dst"
            ok "removed $dst"
        else
            echo "${c_dim}— no $dst${c_reset}"
        fi
    done
    # Older installs dropped a helper next to the binary; clean it up too.
    legacy="$BIN_DIR/bdl-tags-input.py"
    if [ -L "$legacy" ] || [ -f "$legacy" ]; then
        rm -f "$legacy"
        ok "removed legacy $legacy"
    fi
    if [ -e "$FISH_FN" ]; then
        echo "${c_dim}— a fish function is still installed at $FISH_FN${c_reset}"
        echo "${c_dim}  it shadows the binary in fish; remove it to use the engine${c_reset}"
    fi
    echo "Done. The project folder itself was not touched."
    exit 0
fi

[ -n "$engine" ] || die "no bdl entry point found — install the package first: pip install -e ."

MODE="link"
[ "${1:-}" = "--copy" ] && MODE="copy"

mkdir -p "$BIN_DIR"

# Already the right symlink — do nothing (avoids rm+ln on every run).
if [ "$MODE" = "link" ] && [ -L "$DST_BIN" ] \
   && [ "$(readlink -f "$DST_BIN")" = "$(readlink -f "$engine")" ]; then
    ok "already installed: $DST_BIN"
else
    if [ -e "$DST_BIN" ] && [ ! -L "$DST_BIN" ]; then
        backup="$DST_BIN.backup-$(date +%Y%m%d-%H%M%S)"
        mv "$DST_BIN" "$backup"
        echo "${c_dim}  kept the old file as: $backup${c_reset}"
    fi
    rm -f "$DST_BIN"
    if [ "$MODE" = "copy" ]; then
        cp "$engine" "$DST_BIN"
        ok "copied: $DST_BIN"
    else
        ln -s "$engine" "$DST_BIN"
        ok "symlink: $DST_BIN → $engine"
    fi
fi

echo
echo "${c_dim}Check:${c_reset}"
command -v bdl >/dev/null 2>&1 \
    && ok "bdl is on PATH: $(command -v bdl)" \
    || echo "${c_err}⚠ $BIN_DIR is not on PATH — add it to your shell config${c_reset}"

# An old install dropped a fish function with the same name. It wins over the
# binary in fish, so the wizard would never start.
if [ -e "$FISH_FN" ]; then
    echo "${c_err}⚠ $FISH_FN shadows bdl in fish${c_reset}"
    echo "${c_dim}  remove it:  rm '$FISH_FN'  (then run: functions -e bdl)${c_reset}"
fi

echo
echo "Done. Run ${c_ok}bdl${c_reset} to open the interactive wizard."
