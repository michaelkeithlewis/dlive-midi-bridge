#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# dlive-midi-bridge — Raspberry Pi installer
#
# Run on a fresh Raspberry Pi OS (Lite recommended):
#   curl -sSL <raw-url>/install-pi.sh | bash
# Or locally:
#   chmod +x install-pi.sh && sudo ./install-pi.sh
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

INSTALL_DIR="/opt/dlive-midi-bridge"
CONFIG_DIR="/etc/dlive-midi-bridge"
SERVICE_USER="dlive-bridge"

echo "══════════════════════════════════════════════════════"
echo "  dLive MIDI Bridge — Raspberry Pi Installer"
echo "══════════════════════════════════════════════════════"

# Check root
if [[ $EUID -ne 0 ]]; then
   echo "Please run as root (sudo)"
   exit 1
fi

# ── 1. System dependencies ───────────────────────────────────────────
echo "[1/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv avahi-daemon avahi-utils

# Ensure avahi (Bonjour) is running
systemctl enable avahi-daemon
systemctl start avahi-daemon

# ── 2. Create service user ──────────────────────────────────────────
echo "[2/6] Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── 3. Install the package ──────────────────────────────────────────
echo "[3/6] Installing dlive-midi-bridge..."
mkdir -p "$INSTALL_DIR"

# Create venv
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

# If we're running from the project directory, install locally
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    pip install --quiet "$SCRIPT_DIR"
else
    pip install --quiet dlive-midi-bridge
fi

deactivate

# Symlink the CLI into PATH
ln -sf "$INSTALL_DIR/venv/bin/dlive-midi-bridge" /usr/local/bin/dlive-midi-bridge

# ── 4. Configuration ────────────────────────────────────────────────
echo "[4/6] Setting up configuration..."
mkdir -p "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    if [[ -f "$SCRIPT_DIR/config/config.example.yaml" ]]; then
        cp "$SCRIPT_DIR/config/config.example.yaml" "$CONFIG_DIR/config.yaml"
    else
        cat > "$CONFIG_DIR/config.yaml" <<'YAML'
# dlive-midi-bridge configuration
# EDIT THIS: Set your dLive MixRack IP address
dlive_ip: "192.168.1.80"
session_name: "dLive-MIDI-Bridge"
log_midi: false
YAML
    fi
    echo ""
    echo "  ╔══════════════════════════════════════════════════╗"
    echo "  ║  IMPORTANT: Edit the config with your dLive IP  ║"
    echo "  ║  sudo nano /etc/dlive-midi-bridge/config.yaml   ║"
    echo "  ╚══════════════════════════════════════════════════╝"
    echo ""
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$CONFIG_DIR"

# ── 5. Install systemd service ──────────────────────────────────────
echo "[5/6] Installing systemd service..."
if [[ -f "$SCRIPT_DIR/systemd/dlive-midi-bridge.service" ]]; then
    cp "$SCRIPT_DIR/systemd/dlive-midi-bridge.service" /etc/systemd/system/
else
    cat > /etc/systemd/system/dlive-midi-bridge.service <<'UNIT'
[Unit]
Description=dLive MIDI Bridge
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service

[Service]
Type=simple
User=dlive-bridge
Group=dlive-bridge
ExecStart=/usr/local/bin/dlive-midi-bridge --config /etc/dlive-midi-bridge/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
fi

systemctl daemon-reload
systemctl enable dlive-midi-bridge

# ── 6. Done ─────────────────────────────────────────────────────────
echo "[6/6] Installation complete!"
echo ""
echo "  Quick start:"
echo "    1. Edit config:   sudo nano /etc/dlive-midi-bridge/config.yaml"
echo "    2. Start service: sudo systemctl start dlive-midi-bridge"
echo "    3. View logs:     journalctl -u dlive-midi-bridge -f"
echo ""
echo "  Manual test run:"
echo "    dlive-midi-bridge --dlive-ip YOUR_DLIVE_IP --log-midi --verbose"
echo ""
