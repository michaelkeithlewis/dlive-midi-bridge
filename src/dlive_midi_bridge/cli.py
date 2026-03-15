"""
CLI entry point for dlive-midi-bridge.

Usage:
    dlive                     # auto-run (config) or auto-setup (no config)
    dlive setup               # interactive wizard
    dlive scan                # find dLive consoles on the network
    dlive test                # interactive MIDI test sender
    dlive start / stop        # control the background service
    dlive status              # check if bridge is running
"""

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from . import __version__
from .dlive_tcp import DLIVE_MIXRACK_PORT


# ── Config auto-discovery ────────────────────────────────────────────

CONFIG_SEARCH_PATHS = [
    Path.home() / ".config" / "dlive-midi-bridge" / "config.yaml",
    Path("/etc/dlive-midi-bridge/config.yaml"),
]


def _find_config() -> Optional[Path]:
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return None


def _load_config(path: Path) -> dict:
    with open(path) as f:
        config = yaml.safe_load(f)
    return config or {}


# ── Logging ──────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False, quiet: bool = False):
    level = logging.DEBUG if verbose else (logging.ERROR if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Service control ──────────────────────────────────────────────────

LAUNCHD_LABEL = "com.backlinelogic.dlive-midi-bridge"
SYSTEMD_UNIT = "dlive-midi-bridge"


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _service_installed() -> bool:
    if _is_mac():
        return _plist_path().exists()
    if _is_linux():
        return Path(f"/etc/systemd/system/{SYSTEMD_UNIT}.service").exists()
    return False


def _handle_start():
    if _is_mac():
        plist = _plist_path()
        if not plist.exists():
            print("Service not installed. Run 'dlive setup' first.")
            sys.exit(1)
        subprocess.run(["launchctl", "load", str(plist)], check=False)
        print("Bridge started.")
        print(f"Logs: tail -f ~/Library/Logs/dlive-midi-bridge/dlive-midi-bridge.log")
    elif _is_linux():
        subprocess.run(["sudo", "systemctl", "start", SYSTEMD_UNIT], check=False)
        print("Bridge started.")
        print("Logs: journalctl -u dlive-midi-bridge -f")
    else:
        print(f"Service control not supported on {platform.system()}.")


def _handle_stop():
    if _is_mac():
        plist = _plist_path()
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
        print("Bridge stopped.")
    elif _is_linux():
        subprocess.run(["sudo", "systemctl", "stop", SYSTEMD_UNIT], check=False)
        print("Bridge stopped.")
    else:
        print(f"Service control not supported on {platform.system()}.")


def _handle_restart():
    _handle_stop()
    _handle_start()


def _handle_status():
    config_path = _find_config()
    if config_path:
        config = _load_config(config_path)
        print(f"Config:    {config_path}")
        if config.get("bind_ip"):
            print(f"Interface: {config['bind_ip']}")
        print(f"dLive IP:  {config.get('dlive_ip', 'not set')}")
        print(f"Session:   {config.get('session_name', 'dLive-MIDI-Bridge')}")
    else:
        print("Config:    not found (run 'dlive setup')")

    installed = _service_installed()
    print(f"Service:   {'installed' if installed else 'not installed'}")

    if _is_mac() and installed:
        result = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True,
        )
        running = result.returncode == 0
        print(f"Running:   {'yes' if running else 'no'}")
        if running:
            print(f"Logs:      ~/Library/Logs/dlive-midi-bridge/dlive-midi-bridge.log")
    elif _is_linux() and installed:
        result = subprocess.run(
            ["systemctl", "is-active", SYSTEMD_UNIT],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        print(f"Running:   {state}")
        if state == "active":
            print("Logs:      journalctl -u dlive-midi-bridge -f")


# ── Uninstall ────────────────────────────────────────────────────────

INSTALL_DIR = Path.home() / ".local" / "share" / "dlive-midi-bridge"
BIN_DIR = Path.home() / ".local" / "bin"


def _handle_uninstall():
    print()
    print("  dLive MIDI Bridge — Uninstall")
    print("  ─────────────────────────────")
    print()

    try:
        confirm = input("  Are you sure? This removes everything. (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return
    if confirm not in ("y", "yes"):
        print("  Cancelled.")
        return

    print()
    removed = []

    # 1. Stop the service
    if _service_installed():
        print("  Stopping service...")
        if _is_mac():
            plist = _plist_path()
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
            removed.append(f"  - Service plist: {plist}")
        elif _is_linux():
            subprocess.run(["sudo", "systemctl", "stop", SYSTEMD_UNIT], capture_output=True)
            subprocess.run(["sudo", "systemctl", "disable", SYSTEMD_UNIT], capture_output=True)
            svc = Path(f"/etc/systemd/system/{SYSTEMD_UNIT}.service")
            if svc.exists():
                subprocess.run(["sudo", "rm", str(svc)], capture_output=True)
                subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
            removed.append(f"  - systemd service: {svc}")

    # 2. Remove config
    for config_path in CONFIG_SEARCH_PATHS:
        if config_path.exists():
            config_path.unlink()
            removed.append(f"  - Config: {config_path}")
            config_dir = config_path.parent
            if config_dir.exists() and not any(config_dir.iterdir()):
                config_dir.rmdir()

    # 3. Remove symlinks
    for name in ("dlive", "dlive-midi-bridge", "dlive-test-send"):
        link = BIN_DIR / name
        if link.exists() or link.is_symlink():
            link.unlink()
            removed.append(f"  - Symlink: {link}")

    # 4. Remove install directory
    if INSTALL_DIR.exists():
        import shutil
        shutil.rmtree(INSTALL_DIR)
        removed.append(f"  - Install dir: {INSTALL_DIR}")

    # 5. Remove log directory (macOS)
    log_dir = Path.home() / "Library" / "Logs" / "dlive-midi-bridge"
    if log_dir.exists():
        import shutil
        shutil.rmtree(log_dir)
        removed.append(f"  - Logs: {log_dir}")

    if removed:
        print()
        print("  Removed:")
        for item in removed:
            print(item)
    else:
        print("  Nothing to remove — already clean.")

    print()
    print("  Uninstall complete.")
    print()


# ── Help ─────────────────────────────────────────────────────────────

HELP_TEXT = """
  dLive MIDI Bridge — Commands
  ════════════════════════════════════════════════

  dlive              Run the bridge (auto-finds your config)
  dlive setup        Interactive setup wizard
  dlive scan         Find dLive consoles on the network
  dlive test         Send test MIDI messages (interactive)

  dlive start        Start the background service
  dlive stop         Stop the background service
  dlive restart      Restart the background service
  dlive status       Show config + whether bridge is running

  dlive help         Show this help
  dlive uninstall    Remove everything

  Advanced (power-user):
    dlive run --dlive-ip 192.168.1.70 --log-midi --verbose
    dlive run --list-midi-ports
    dlive --version
"""


def print_help():
    print(HELP_TEXT)


# ── Scan ─────────────────────────────────────────────────────────────

def _handle_scan():
    from .wizard import scan_for_dlive, _get_local_subnet

    subnet = _get_local_subnet()
    if not subnet:
        print("Could not determine your local network.", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {subnet}1-254 for dLive consoles...", end="", flush=True)

    def _progress(done, total):
        pct = int(done / total * 100)
        print(f"\rScanning {subnet}1-254 for dLive consoles... {pct}%", end="", flush=True)

    found = scan_for_dlive(progress_callback=_progress)
    print(f"\rScanning {subnet}1-254 for dLive consoles... done!   ")

    if found:
        print(f"\nFound {len(found)} dLive device(s):\n")
        for ip, port, dtype in found:
            print(f"  {dtype:10s}  {ip}:{port}")
        print()
    else:
        print("\nNo dLive consoles found.")
        print("Make sure the console is powered on and on the same network.\n")


# ── Run bridge ───────────────────────────────────────────────────────

def _handle_run(args):
    from .bridge import MIDIBridge

    if getattr(args, "list_midi_ports", False):
        from .local_midi import LocalMIDIInput
        listener = LocalMIDIInput(midi_callback=lambda _: None)
        ports = listener.list_ports()
        if ports:
            print("Available local MIDI input ports:")
            for i, name in enumerate(ports):
                print(f"  [{i}] {name}")
        else:
            print("No local MIDI input ports found.")
        sys.exit(0)

    config = {}
    config_path = None

    if getattr(args, "config", None):
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        config = _load_config(config_path)
    else:
        config_path = _find_config()
        if config_path:
            config = _load_config(config_path)

    dlive_ip = getattr(args, "dlive_ip", None) or config.get("dlive_ip")
    if not dlive_ip:
        print(
            "No config found and no --dlive-ip given.\n"
            "Run 'dlive setup' to configure, or pass --dlive-ip.",
            file=sys.stderr,
        )
        sys.exit(1)

    dlive_port_arg = getattr(args, "dlive_port", None)
    if dlive_port_arg:
        dlive_port = dlive_port_arg
    elif "dlive_port" in config:
        dlive_port = config["dlive_port"]
    else:
        dlive_port = DLIVE_MIXRACK_PORT

    local_port = getattr(args, "local_port", None) or config.get("local_port") or 5004
    session_name = getattr(args, "session_name", None) or config.get("session_name") or "dLive-MIDI-Bridge"
    filter_name = getattr(args, "filter_name", None) or config.get("filter_name")
    log_midi = getattr(args, "log_midi", False) or config.get("log_midi", False)
    verbose = getattr(args, "verbose", False) or config.get("verbose", False)
    quiet = getattr(args, "quiet", False) or config.get("quiet", False)

    midi_channel = getattr(args, "midi_channel", None) or config.get("midi_channel")
    if midi_channel is not None:
        midi_channel = midi_channel - 1

    enable_local_midi = getattr(args, "local_midi", False) or config.get("local_midi", False)
    local_midi_filter = getattr(args, "local_midi_filter", None) or config.get("local_midi_filter")
    bind_ip = config.get("bind_ip")

    _setup_logging(verbose=verbose, quiet=quiet)

    bridge = MIDIBridge(
        dlive_host=dlive_ip,
        dlive_port=dlive_port,
        local_port=local_port,
        session_name=session_name,
        filter_name=filter_name,
        midi_channel=midi_channel,
        log_midi=log_midi,
        enable_local_midi=enable_local_midi,
        local_midi_filter=local_midi_filter,
        bind_ip=bind_ip,
    )

    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass


# ── Parser & main ────────────────────────────────────────────────────

def _add_run_args(parser: argparse.ArgumentParser):
    conn = parser.add_argument_group("dLive connection")
    conn.add_argument("--dlive-ip", help="IP address of the dLive")
    conn.add_argument("--dlive-port", type=int, help="TCP port (default: 51325)")

    rtp = parser.add_argument_group("RTP-MIDI")
    rtp.add_argument("--local-port", type=int, default=None)
    rtp.add_argument("--session-name", default=None)
    rtp.add_argument("--filter", dest="filter_name")

    midi = parser.add_argument_group("MIDI")
    midi.add_argument("--midi-channel", type=int, choices=range(1, 17), metavar="1-16")
    midi.add_argument("--local-midi", action="store_true")
    midi.add_argument("--local-midi-filter", metavar="NAME")
    midi.add_argument("--list-midi-ports", action="store_true")

    gen = parser.add_argument_group("general")
    gen.add_argument("--config", help="Path to config file")
    gen.add_argument("--log-midi", action="store_true")
    gen.add_argument("-v", "--verbose", action="store_true")
    gen.add_argument("-q", "--quiet", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dlive",
        description="Allen & Heath dLive MIDI Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  dlive              Run the bridge (auto-finds config)\n"
            "  dlive setup        Interactive setup wizard\n"
            "  dlive scan         Find dLive consoles on the network\n"
            "  dlive test         Send test MIDI messages\n"
            "  dlive start        Start the background service\n"
            "  dlive stop         Stop the background service\n"
            "  dlive restart      Restart the background service\n"
            "  dlive status       Check if the bridge is running\n"
            "  dlive uninstall    Remove everything\n"
        ),
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("help", help="Show all available commands")
    subparsers.add_parser("setup", help="Interactive setup wizard")
    subparsers.add_parser("scan", help="Find dLive consoles on the network")
    subparsers.add_parser("test", help="Send test MIDI messages")
    subparsers.add_parser("start", help="Start the background service")
    subparsers.add_parser("stop", help="Stop the background service")
    subparsers.add_parser("restart", help="Restart the background service")
    subparsers.add_parser("status", help="Check if bridge is running")
    subparsers.add_parser("uninstall", help="Remove everything")

    run_parser = subparsers.add_parser("run", help="Run the bridge (foreground)")
    _add_run_args(run_parser)

    _add_run_args(parser)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    cmd = args.command

    if cmd == "help":
        print_help()
    elif cmd == "setup":
        from .wizard import run_wizard
        run_wizard()
    elif cmd == "scan":
        _handle_scan()
    elif cmd == "test":
        from .test_send import run_interactive
        try:
            asyncio.run(run_interactive())
        except KeyboardInterrupt:
            print("\n  Bye!")
    elif cmd == "start":
        _handle_start()
    elif cmd == "stop":
        _handle_stop()
    elif cmd == "restart":
        _handle_restart()
    elif cmd == "status":
        _handle_status()
    elif cmd == "uninstall":
        _handle_uninstall()
    elif cmd == "run":
        _handle_run(args)
    else:
        # No subcommand: if flags given, run. If config exists, run.
        # If nothing, launch setup.
        if args.dlive_ip or args.config or getattr(args, "list_midi_ports", False):
            _handle_run(args)
        elif _find_config():
            _handle_run(args)
        else:
            print("No config found. Let's set things up!\n")
            from .wizard import run_wizard
            run_wizard()


if __name__ == "__main__":
    main()
