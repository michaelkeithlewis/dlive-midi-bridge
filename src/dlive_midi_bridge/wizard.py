"""
Interactive setup wizard for dlive-midi-bridge.

Walks the user through configuration, tests the dLive connection,
and optionally installs as a system service.

Usage:
    dlive-midi-bridge setup
"""

import ipaddress
import os
import platform
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import yaml

from .dlive_tcp import DLIVE_MIXRACK_PORT, DLIVE_SURFACE_PORT


# ── Terminal helpers ─────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"

USE_COLOR = sys.stdout.isatty()

# When launched from a curl|bash pipe, stdin is the download stream.
# Reopen from /dev/tty so we can actually read keyboard input.
_tty_input = None


def _ensure_tty():
    """Ensure we're reading from the real terminal, not a pipe."""
    global _tty_input
    if _tty_input is not None:
        return
    if sys.stdin.isatty():
        _tty_input = sys.stdin
        return
    try:
        _tty_input = open("/dev/tty", "r")
        sys.stdin = _tty_input
    except OSError:
        _tty_input = sys.stdin


def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


TRUCK_PACKER_BANNER = r"""
  _____ ____  _   _  ____ _  __  ____   _    ____ _  _______ ____
 |_   _|  _ \| | | |/ ___| |/ / |  _ \ / \  / ___| |/ / ____|  _ \
   | | | |_) | | | | |   | ' /  | |_) / _ \| |   | ' /|  _| | |_) |
   | | |  _ <| |_| | |___| . \  |  __/ ___ \ |___| . \| |___|  _ <
   |_| |_| \_\\___/ \____|_|\_\ |_| /_/   \_\____|_|\_\_____|_| \_\

                     s p o n s o r e d   b y
"""


def banner():
    print(TRUCK_PACKER_BANNER)
    print(_c(BOLD, "  ╔══════════════════════════════════════════════════╗"))
    print(_c(BOLD, "  ║       dLive MIDI Bridge — Setup Wizard          ║"))
    print(_c(BOLD, "  ╚══════════════════════════════════════════════════╝"))
    print()
    os_name = platform.system()
    os_detail = platform.platform(terse=True)
    print(f"  Platform: {os_name} ({os_detail})")
    is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
    if is_root:
        print(f"  Running as: {_c(YELLOW, 'root')}")
    print()


def step_header(num: int, title: str):
    print()
    print(_c(CYAN, f"  [{num}/8] {title}"))
    print(_c(DIM, "  " + "─" * 48))


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        sys.exit(0)
    return value if value else (default or "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        sys.exit(0)
    if not value:
        return default
    return value in ("y", "yes")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: int = 0) -> str:
    """Present numbered choices. Returns the value of the selected option."""
    print(f"  {prompt}")
    for i, (value, label) in enumerate(options):
        marker = _c(BOLD, " *") if i == default else "  "
        print(f"    {marker} [{i + 1}] {label}")
    try:
        raw = input(f"  Choice [default: {default + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        sys.exit(0)
    if not raw:
        return options[default][0]
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except ValueError:
        pass
    print(_c(YELLOW, f"    Invalid choice, using default: {options[default][1]}"))
    return options[default][0]


def ok(msg: str):
    print(f"  {_c(GREEN, '✓')} {msg}")


def warn(msg: str):
    print(f"  {_c(YELLOW, '!')} {msg}")


def fail(msg: str):
    print(f"  {_c(RED, '✗')} {msg}")


# ── Validation helpers ───────────────────────────────────────────────

def validate_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def test_tcp_connection(host: str, port: int, timeout: float = 5.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((host, port))
        return result == 0
    except (socket.timeout, OSError):
        return False
    finally:
        sock.close()


def scan_midi_ports() -> list[str]:
    try:
        import rtmidi
        probe = rtmidi.MidiIn()
        ports = probe.get_ports()
        probe.close_port()
        del probe
        return ports
    except Exception:
        return []


# ── Wizard steps ─────────────────────────────────────────────────────

def step_dlive_ip() -> str:
    step_header(1, "dLive IP Address")
    print("  Enter the IP address of your dLive MixRack or Surface.")
    print()
    while True:
        ip = ask("dLive IP address", default="192.168.1.80")
        if validate_ip(ip):
            ok(f"IP address: {ip}")
            return ip
        fail(f"'{ip}' is not a valid IP address. Try again.")


def step_target() -> tuple[str, int]:
    step_header(2, "Connection Target")
    target = ask_choice(
        "What are you connecting to?",
        [
            ("mixrack", f"MixRack  (TCP port {DLIVE_MIXRACK_PORT})"),
            ("surface", f"Surface  (TCP port {DLIVE_SURFACE_PORT})"),
        ],
        default=0,
    )
    port = DLIVE_SURFACE_PORT if target == "surface" else DLIVE_MIXRACK_PORT
    ok(f"Target: {target} (port {port})")
    return target, port


def step_test_connection(host: str, port: int):
    step_header(3, "Connection Test")
    print(f"  Testing TCP connection to {host}:{port}...")
    print()
    if test_tcp_connection(host, port):
        ok(f"Connected to dLive at {host}:{port}")
    else:
        fail(f"Could not reach {host}:{port}")
        warn("The dLive might be powered off, or the IP might be wrong.")
        if not ask_yes_no("Continue anyway?", default=True):
            print("\n  Setup cancelled. Fix the connection and try again.")
            sys.exit(0)
        warn("Continuing without a verified connection.")


def step_rtp_midi() -> tuple[str, Optional[str]]:
    step_header(4, "RTP-MIDI Settings")
    session_name = ask("Session name (how this bridge appears on the network)",
                       default="dLive-MIDI-Bridge")
    print()
    if ask_yes_no("Filter RTP-MIDI peers by name?", default=False):
        filter_name = ask("Only connect to peers whose name contains")
        ok(f"Peer filter: '{filter_name}'")
    else:
        filter_name = None
        ok("Peer filter: none (accept all)")
    return session_name, filter_name


def step_local_midi() -> tuple[bool, Optional[str]]:
    step_header(5, "Local MIDI (USB / Hardware)")
    ports = scan_midi_ports()
    if ports:
        print(f"  Found {len(ports)} MIDI input port(s):")
        for i, name in enumerate(ports):
            print(f"    [{i + 1}] {name}")
        print()
    else:
        print("  No local MIDI ports detected right now.")
        print("  (You can plug in a USB controller later — it will be auto-detected.)")
        print()

    enable = ask_yes_no("Enable local MIDI input?", default=bool(ports))
    if not enable:
        ok("Local MIDI: disabled")
        return False, None

    midi_filter = None
    if ports and len(ports) > 1:
        if ask_yes_no("Filter to a specific device?", default=False):
            midi_filter = ask("Only open ports whose name contains")
    ok(f"Local MIDI: enabled" + (f" (filter: '{midi_filter}')" if midi_filter else ""))
    return True, midi_filter


def step_midi_options() -> tuple[Optional[int], bool]:
    step_header(6, "MIDI Options")
    if ask_yes_no("Filter to a specific MIDI channel?", default=False):
        while True:
            raw = ask("MIDI channel (1-16)")
            try:
                ch = int(raw)
                if 1 <= ch <= 16:
                    ok(f"MIDI channel filter: {ch}")
                    midi_channel = ch
                    break
                fail("Must be 1-16.")
            except ValueError:
                fail("Enter a number 1-16.")
    else:
        midi_channel = None
        ok("MIDI channel filter: none (pass all)")

    print()
    log_midi = ask_yes_no("Log every MIDI message? (useful for debugging)", default=False)
    ok(f"MIDI logging: {'on' if log_midi else 'off'}")
    return midi_channel, log_midi


def _default_config_path() -> Path:
    if platform.system() == "Linux" and os.geteuid() == 0:
        return Path("/etc/dlive-midi-bridge/config.yaml")
    return Path.home() / ".config" / "dlive-midi-bridge" / "config.yaml"


def step_write_config(config: dict) -> Path:
    step_header(7, "Write Configuration")
    default_path = _default_config_path()
    path_str = ask("Config file location", default=str(default_path))
    config_path = Path(path_str).expanduser()

    if config_path.exists():
        warn(f"Config already exists at {config_path}")
        if not ask_yes_no("Overwrite?", default=False):
            print("  Keeping existing config.")
            return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        "# dlive-midi-bridge configuration\n"
        "# Generated by: dlive-midi-bridge setup\n"
        "#\n"
        "# Re-run 'dlive-midi-bridge setup' to regenerate.\n"
        "# Or edit this file directly — all fields are documented in\n"
        "# config/config.example.yaml\n"
        "\n"
    )

    clean = {k: v for k, v in config.items() if v is not None}
    yaml_body = yaml.dump(clean, default_flow_style=False, sort_keys=False)

    config_path.write_text(header + yaml_body)
    ok(f"Config written to {config_path}")
    return config_path


# ── Service install ──────────────────────────────────────────────────

LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.backlinelogic.dlive-midi-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
        <string>run</string>
        <string>--config</string>
        <string>{config_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/dlive-midi-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/dlive-midi-bridge.error.log</string>
</dict>
</plist>
"""

LAUNCHD_LABEL = "com.backlinelogic.dlive-midi-bridge"


def _find_exe() -> Optional[str]:
    return shutil.which("dlive-midi-bridge")


def _install_launchd(config_path: Path):
    exe = _find_exe()
    if not exe:
        fail("Could not find dlive-midi-bridge on PATH.")
        warn("Make sure you've installed the package (pip install .)")
        return

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"

    log_dir = Path.home() / "Library" / "Logs" / "dlive-midi-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_content = LAUNCHD_PLIST_TEMPLATE.format(
        exe_path=exe,
        config_path=str(config_path),
        log_dir=str(log_dir),
    )

    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)],
                       capture_output=True)

    plist_path.write_text(plist_content)
    ok(f"Plist written to {plist_path}")

    if ask_yes_no("Start the bridge now?", default=True):
        subprocess.run(["launchctl", "load", str(plist_path)], check=False)
        ok("Service loaded. The bridge is running.")
        print()
        print(f"  Logs: {log_dir}/dlive-midi-bridge.log")
        print(f"  Stop: launchctl unload {plist_path}")
        print(f"  Start: launchctl load {plist_path}")
    else:
        ok("Plist installed but not started.")
        print(f"  Start later: launchctl load {plist_path}")


def _install_systemd(config_path: Path):
    is_root = os.geteuid() == 0

    if not is_root:
        warn("systemd service install requires root.")
        warn("Re-run with: sudo dlive-midi-bridge setup")
        return

    service_src = Path(__file__).parent.parent.parent / "systemd" / "dlive-midi-bridge.service"
    service_dest = Path("/etc/systemd/system/dlive-midi-bridge.service")

    if service_src.exists():
        shutil.copy2(service_src, service_dest)
    else:
        exe = _find_exe() or "/usr/local/bin/dlive-midi-bridge"
        service_dest.write_text(textwrap.dedent(f"""\
            [Unit]
            Description=dLive MIDI Bridge — RTP-MIDI to Allen & Heath dLive TCP
            After=network-online.target avahi-daemon.service
            Wants=network-online.target avahi-daemon.service

            [Service]
            Type=simple
            User=dlive-bridge
            Group=dlive-bridge
            ExecStart={exe} run --config {config_path}
            Restart=always
            RestartSec=5

            [Install]
            WantedBy=multi-user.target
        """))

    ok(f"Service file installed at {service_dest}")

    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "dlive-midi-bridge"], check=False)
    ok("Service enabled (starts on boot)")

    if ask_yes_no("Start the bridge now?", default=True):
        subprocess.run(["systemctl", "start", "dlive-midi-bridge"], check=False)
        ok("Service started.")
        print()
        print("  View logs: journalctl -u dlive-midi-bridge -f")
        print("  Stop:      sudo systemctl stop dlive-midi-bridge")
        print("  Restart:   sudo systemctl restart dlive-midi-bridge")
    else:
        ok("Service enabled but not started.")
        print("  Start later: sudo systemctl start dlive-midi-bridge")


def step_install_service(config_path: Path):
    step_header(8, "Install as System Service")
    system = platform.system()

    if system == "Darwin":
        print("  macOS detected — can install as a launchd agent.")
        print("  The bridge will start automatically on login.")
    elif system == "Linux":
        print("  Linux detected — can install as a systemd service.")
        print("  The bridge will start automatically on boot.")
    else:
        warn(f"Service install not supported on {system}.")
        return

    print()
    if not ask_yes_no("Install as a system service?", default=True):
        ok("Skipping service install.")
        return

    if system == "Darwin":
        _install_launchd(config_path)
    elif system == "Linux":
        _install_systemd(config_path)


# ── Summary ──────────────────────────────────────────────────────────

def print_summary(config: dict, config_path: Path):
    print()
    print(_c(BOLD, "  ╔══════════════════════════════════════════════════╗"))
    print(_c(BOLD, "  ║              Setup Complete                      ║"))
    print(_c(BOLD, "  ╚══════════════════════════════════════════════════╝"))
    print()
    print(f"  Config file: {config_path}")
    print(f"  dLive:       {config['dlive_ip']}:{config.get('dlive_port', DLIVE_MIXRACK_PORT)}")
    if config.get("local_midi"):
        filt = config.get("local_midi_filter", "all devices")
        print(f"  Local MIDI:  enabled ({filt})")
    print()
    print("  Run manually:")
    print(f"    dlive-midi-bridge run --config {config_path}")
    print()
    print("  Re-run this wizard anytime:")
    print("    dlive-midi-bridge setup")
    print()


# ── Main wizard entry point ──────────────────────────────────────────

def run_wizard():
    _ensure_tty()
    banner()

    dlive_ip = step_dlive_ip()
    target, dlive_port = step_target()
    step_test_connection(dlive_ip, dlive_port)
    session_name, filter_name = step_rtp_midi()
    enable_local_midi, local_midi_filter = step_local_midi()
    midi_channel, log_midi = step_midi_options()

    config = {
        "dlive_ip": dlive_ip,
        "dlive_port": dlive_port,
        "session_name": session_name,
        "filter_name": filter_name,
        "local_midi": enable_local_midi,
        "local_midi_filter": local_midi_filter,
        "midi_channel": midi_channel,
        "log_midi": log_midi,
    }

    config_path = step_write_config(config)
    step_install_service(config_path)
    print_summary(config, config_path)
