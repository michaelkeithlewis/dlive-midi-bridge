"""
CLI entry point for dlive-midi-bridge.

Usage:
    dlive-midi-bridge --dlive-ip 192.168.1.80
    dlive-midi-bridge --config /etc/dlive-midi-bridge/config.yaml
    dlive-midi-bridge --dlive-ip 192.168.1.80 --log-midi --verbose
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import yaml

from . import __version__
from .bridge import MIDIBridge
from .dlive_tcp import DLIVE_MIXRACK_PORT, DLIVE_SURFACE_PORT


def load_config(path: str) -> dict:
    """Load configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    return config or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dlive-midi-bridge",
        description=(
            "RTP-MIDI to Allen & Heath dLive TCP bridge.\n"
            "Receives Network MIDI (Bonjour) and forwards to a dLive console."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --dlive-ip 192.168.1.80\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --target surface\n"
            "  %(prog)s --config /etc/dlive-midi-bridge/config.yaml\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --filter 'Tracks' --log-midi\n"
        ),
    )

    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Connection
    conn = parser.add_argument_group("dLive connection")
    conn.add_argument(
        "--dlive-ip",
        help="IP address of the dLive MixRack or Surface",
    )
    conn.add_argument(
        "--dlive-port",
        type=int,
        help=f"TCP port (default: {DLIVE_MIXRACK_PORT} for MixRack)",
    )
    conn.add_argument(
        "--target",
        choices=["mixrack", "surface"],
        default="mixrack",
        help="Connect to MixRack (51325) or Surface (51328). Default: mixrack",
    )

    # RTP-MIDI
    rtp = parser.add_argument_group("RTP-MIDI settings")
    rtp.add_argument(
        "--local-port",
        type=int,
        default=5004,
        help="Local UDP port for RTP-MIDI (default: 5004)",
    )
    rtp.add_argument(
        "--session-name",
        default="dLive-MIDI-Bridge",
        help="Name for the RTP-MIDI session (default: dLive-MIDI-Bridge)",
    )
    rtp.add_argument(
        "--filter",
        dest="filter_name",
        help="Only connect to RTP-MIDI peers whose name contains this string",
    )

    # MIDI
    midi = parser.add_argument_group("MIDI options")
    midi.add_argument(
        "--midi-channel",
        type=int,
        choices=range(1, 17),
        metavar="1-16",
        help="Only forward messages on this MIDI channel (default: all)",
    )

    # Logging & config
    gen = parser.add_argument_group("general")
    gen.add_argument(
        "--config",
        help="Path to YAML config file",
    )
    gen.add_argument(
        "--log-midi",
        action="store_true",
        help="Log every MIDI message (verbose, useful for debugging)",
    )
    gen.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    gen.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress all output except errors",
    )

    return parser


def setup_logging(verbose: bool = False, quiet: bool = False):
    level = logging.DEBUG if verbose else (logging.ERROR if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Load config file if specified
    config = {}
    if args.config:
        config = load_config(args.config)

    # CLI args override config file
    dlive_ip = args.dlive_ip or config.get("dlive_ip")
    if not dlive_ip:
        parser.error("--dlive-ip is required (or set dlive_ip in config file)")

    # Determine port
    if args.dlive_port:
        dlive_port = args.dlive_port
    elif "dlive_port" in config:
        dlive_port = config["dlive_port"]
    elif args.target == "surface":
        dlive_port = DLIVE_SURFACE_PORT
    else:
        dlive_port = DLIVE_MIXRACK_PORT

    local_port = args.local_port or config.get("local_port", 5004)
    session_name = args.session_name or config.get("session_name", "dLive-MIDI-Bridge")
    filter_name = args.filter_name or config.get("filter_name")
    log_midi = args.log_midi or config.get("log_midi", False)
    verbose = args.verbose or config.get("verbose", False)
    quiet = args.quiet or config.get("quiet", False)

    midi_channel = args.midi_channel or config.get("midi_channel")
    if midi_channel is not None:
        midi_channel = midi_channel - 1  # convert 1-16 → 0-15

    setup_logging(verbose=verbose, quiet=quiet)

    bridge = MIDIBridge(
        dlive_host=dlive_ip,
        dlive_port=dlive_port,
        local_port=local_port,
        session_name=session_name,
        filter_name=filter_name,
        midi_channel=midi_channel,
        log_midi=log_midi,
    )

    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
