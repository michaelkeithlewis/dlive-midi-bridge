#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# dLive MIDI Bridge — Universal Installer
#
# One-liner:
#   curl -sSL https://raw.githubusercontent.com/michaelkeithlewis/dlive-midi-bridge/main/install.sh | bash
#
# Works on macOS and Linux (including Raspberry Pi).
# Clones the repo, installs into a venv, and launches the setup wizard.
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/michaelkeithlewis/dlive-midi-bridge.git"
INSTALL_DIR="$HOME/.local/share/dlive-midi-bridge"
BIN_DIR="$HOME/.local/bin"

echo ""
echo "  ══════════════════════════════════════════════════════"
echo "    dLive MIDI Bridge — Installer"
echo "  ══════════════════════════════════════════════════════"
echo ""

# ── Platform detection ───────────────────────────────────────────────

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="mac" ;;
    Linux)  PLATFORM="linux" ;;
    *)      echo "Unsupported OS: $OS"; exit 1 ;;
esac

echo "  Platform: $PLATFORM ($(uname -m))"

# ── Check for Python 3.9+ ───────────────────────────────────────────

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 9 ]]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo ""
    echo "  ERROR: Python 3.9+ is required but not found."
    if [[ "$PLATFORM" == "mac" ]]; then
        echo "  Install it with: brew install python3"
    else
        echo "  Install it with: sudo apt install python3 python3-venv python3-pip"
    fi
    exit 1
fi

echo "  Python:   $($PYTHON --version)"

# ── Linux: install system dependencies if needed ─────────────────────

if [[ "$PLATFORM" == "linux" ]]; then
    missing=()
    command -v git &>/dev/null || missing+=(git)
    $PYTHON -c "import venv" 2>/dev/null || missing+=(python3-venv)

    if ! dpkg -s libasound2-dev &>/dev/null 2>&1 && \
       ! dpkg -s libasound-dev &>/dev/null 2>&1; then
        if apt-cache show libasound2-dev &>/dev/null 2>&1; then
            missing+=(libasound2-dev)
        else
            missing+=(libasound-dev)
        fi
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo ""
        echo "  Installing system packages: ${missing[*]}"
        if [[ $EUID -eq 0 ]]; then
            apt-get update -qq
            apt-get install -y -qq "${missing[@]}"
        else
            sudo apt-get update -qq
            sudo apt-get install -y -qq "${missing[@]}"
        fi
    fi
fi

# ── Clone or update the repo ────────────────────────────────────────

echo ""
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "  Updating existing install..."
    git -C "$INSTALL_DIR" pull --quiet
else
    echo "  Cloning repository..."
    rm -rf "$INSTALL_DIR"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi

# ── Create venv and install ──────────────────────────────────────────

echo "  Creating virtual environment..."
$PYTHON -m venv "$INSTALL_DIR/.venv"

echo "  Installing..."
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet "$INSTALL_DIR"

# ── Add to PATH ──────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/.venv/bin/dlive" "$BIN_DIR/dlive"
ln -sf "$INSTALL_DIR/.venv/bin/dlive-midi-bridge" "$BIN_DIR/dlive-midi-bridge"
ln -sf "$INSTALL_DIR/.venv/bin/dlive-test-send" "$BIN_DIR/dlive-test-send"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "  Adding $BIN_DIR to your PATH..."
    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  RC_FILE="$HOME/.zshrc" ;;
        bash) RC_FILE="$HOME/.bashrc" ;;
        *)    RC_FILE="$HOME/.profile" ;;
    esac

    if ! grep -q "$BIN_DIR" "$RC_FILE" 2>/dev/null; then
        echo "" >> "$RC_FILE"
        echo "# dlive-midi-bridge" >> "$RC_FILE"
        echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$RC_FILE"
    fi

    export PATH="$BIN_DIR:$PATH"
    echo "  Added to $RC_FILE (restart your shell or run: source $RC_FILE)"
fi

# ── Done — launch wizard ────────────────────────────────────────────

echo ""
echo "  ══════════════════════════════════════════════════════"
echo "    Installation complete!"
echo "  ══════════════════════════════════════════════════════"
echo ""

if [[ -t 0 ]]; then
    echo "  Launching setup wizard..."
    echo ""
    "$BIN_DIR/dlive" setup
else
    echo "  Launching setup wizard..."
    echo ""
    "$BIN_DIR/dlive" setup < /dev/tty
fi
