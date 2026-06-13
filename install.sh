#!/usr/bin/env bash
set -euo pipefail

# Claude Code Watcher (TUI) — installer
#
# Remote (recommended):
#   curl -fsSL https://github.com/claude-watcher/tui/releases/latest/download/install.sh | bash
#   curl -fsSL https://github.com/claude-watcher/tui/releases/download/v1.5.1/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --version v1.5.1   # explicit pin
#
# From a local clone:
#   ./install.sh
#
# Env overrides (handy for piped / unattended installs):
#   CW_VERSION=v1.5.1   pin a version
#   CW_LANG=fr|en       set language without the interactive prompt

readonly REPO="claude-watcher/tui"
readonly SCRIPT_NAME="claude-watcher-tui.py"
# Replaced by the release workflow with the published tag. "__VERSION__" means
# the script is running outside a release (from source / raw checkout).
readonly DEFAULT_VERSION="__VERSION__"

readonly CYAN="\033[36m"; readonly GREEN="\033[32m"; readonly RESET="\033[0m"

# ── Local vs remote mode ──────────────────────────────────────────────────────
SELF="${BASH_SOURCE[0]:-}"
LOCAL_DIR=""
if [[ -n "$SELF" && -f "$SELF" ]]; then
    LOCAL_DIR="$(cd "$(dirname "$SELF")" && pwd)"
fi
local_mode() { [[ -n "$LOCAL_DIR" && -f "$LOCAL_DIR/$SCRIPT_NAME" ]]; }

# ── Resolve the version to install ────────────────────────────────────────────
VERSION="${CW_VERSION:-}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="${2:?--version needs a value}"; shift 2 ;;
        --version=*)  VERSION="${1#*=}"; shift ;;
        -h|--help)    sed -n '3,18p' "$0" 2>/dev/null; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

resolve_latest() {
    local tag
    tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
          | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    [[ -n "$tag" ]] || { echo "Could not resolve the latest release of ${REPO}." >&2; exit 1; }
    printf '%s' "$tag"
}

if ! local_mode; then
    if [[ -z "$VERSION" ]]; then
        if [[ "$DEFAULT_VERSION" != "__VERSION__" ]]; then
            VERSION="$DEFAULT_VERSION"
        else
            VERSION="$(resolve_latest)"
        fi
    fi
fi
readonly BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"

fetch() {
    local name="$1" dest="$2"
    if local_mode; then
        cp "$LOCAL_DIR/$name" "$dest"
    else
        curl -fsSL "$BASE_URL/$name" -o "$dest"
    fi
}

echo -e "${CYAN}"
echo "╔══════════════════════════════════════╗"
echo "║   Claude Code Watcher TUI — Install  ║"
echo "╚══════════════════════════════════════╝"
echo -e "${RESET}"
if local_mode; then
    echo "  source: local clone ($LOCAL_DIR)"
else
    echo "  source: release ${VERSION}"
fi

# ── Language — env, else interactive prompt (only with a real terminal) ───────
SYS_LANG="${LANG:-}"
[[ "$SYS_LANG" == fr* ]] && DEFAULT_LANG="fr" || DEFAULT_LANG="en"
CHOSEN_LANG="${CW_LANG:-}"

if [[ -z "$CHOSEN_LANG" && -e /dev/tty ]]; then
    if [[ "$DEFAULT_LANG" == "fr" ]]; then
        read -rp "Language / Langue [FR/en]: " LANG_INPUT < /dev/tty || LANG_INPUT=""
    else
        read -rp "Language / Langue [EN/fr]: " LANG_INPUT < /dev/tty || LANG_INPUT=""
    fi
    case "${LANG_INPUT,,}" in
        fr) CHOSEN_LANG="fr" ;;
        en) CHOSEN_LANG="en" ;;
        *)  CHOSEN_LANG="$DEFAULT_LANG" ;;
    esac
fi
CHOSEN_LANG="${CHOSEN_LANG:-$DEFAULT_LANG}"
echo ""

# ── 1. Dependencies ───────────────────────────────────────────────────────────
echo -e "${CYAN}[1/3] Dependencies...${RESET}"
command -v uv >/dev/null 2>&1 || {
    echo "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
}
echo "  uv: $(uv --version 2>/dev/null || echo 'ok')"
command -v wmctrl  >/dev/null 2>&1 || { echo "  Installing wmctrl..."; sudo apt install -y wmctrl; }
command -v xdotool >/dev/null 2>&1 || { echo "  Installing xdotool..."; sudo apt install -y xdotool; }
echo "  All dependencies ok"

# ── 2. Script ─────────────────────────────────────────────────────────────────
echo -e "${CYAN}[2/3] Installing...${RESET}"
mkdir -p "$HOME/bin"
fetch "$SCRIPT_NAME" "$HOME/bin/claude-watcher-tui"
chmod +x "$HOME/bin/claude-watcher-tui"
echo "  ~/bin/claude-watcher-tui"

# ── 3. Config ─────────────────────────────────────────────────────────────────
echo -e "${CYAN}[3/3] Config...${RESET}"
mkdir -p "$HOME/.config/claude-watcher"
if [[ ! -f "$HOME/.config/claude-watcher/config.ini" ]]; then
    cat > "$HOME/.config/claude-watcher/config.ini" << EOF
[general]
lang = ${CHOSEN_LANG}

[display]
# refresh_ms = 2000
EOF
    echo "  ~/.config/claude-watcher/config.ini (lang=${CHOSEN_LANG})"
else
    echo "  ~/.config/claude-watcher/config.ini (already exists, skipped)"
fi

echo ""
echo -e "${GREEN}Done! Run with:${RESET}"
echo "  uv run ~/bin/claude-watcher-tui"
