"""
Live MIDI monitor — shows all MIDI traffic flowing through the bridge.

Usage: dlive monitor
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from . import __version__
from .dlive_tcp import DLiveTCPConnection, DLIVE_MIXRACK_PORT
from .rtp_midi import RTPMIDIReceiver


# ── ANSI colors ──────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
MAGENTA = "\033[35m"
RED    = "\033[31m"


def _decode_midi(data: bytes) -> str:
    """Human-readable MIDI message description."""
    if len(data) < 1:
        return "empty"
    status = data[0]
    hex_str = data.hex(" ")

    if 0x80 <= status <= 0x8F:
        ch = (status & 0x0F) + 1
        note = data[1] if len(data) > 1 else 0
        vel = data[2] if len(data) > 2 else 0
        return f"Note Off      ch={ch:>2}  note={note:<3}  vel={vel:<3}  [{hex_str}]"
    elif 0x90 <= status <= 0x9F:
        ch = (status & 0x0F) + 1
        note = data[1] if len(data) > 1 else 0
        vel = data[2] if len(data) > 2 else 0
        label = "Note Off" if vel == 0 else "Note On "
        return f"{label}      ch={ch:>2}  note={note:<3}  vel={vel:<3}  [{hex_str}]"
    elif 0xA0 <= status <= 0xAF:
        ch = (status & 0x0F) + 1
        note = data[1] if len(data) > 1 else 0
        pressure = data[2] if len(data) > 2 else 0
        return f"Aftertouch    ch={ch:>2}  note={note:<3}  val={pressure:<3}  [{hex_str}]"
    elif 0xB0 <= status <= 0xBF:
        ch = (status & 0x0F) + 1
        cc = data[1] if len(data) > 1 else 0
        val = data[2] if len(data) > 2 else 0
        return f"CC            ch={ch:>2}  cc={cc:<3}    val={val:<3}  [{hex_str}]"
    elif 0xC0 <= status <= 0xCF:
        ch = (status & 0x0F) + 1
        prog = data[1] if len(data) > 1 else 0
        return f"Program Change ch={ch:>2}  prog={prog:<3}              [{hex_str}]"
    elif 0xD0 <= status <= 0xDF:
        ch = (status & 0x0F) + 1
        pressure = data[1] if len(data) > 1 else 0
        return f"Chan Pressure ch={ch:>2}  val={pressure:<3}              [{hex_str}]"
    elif 0xE0 <= status <= 0xEF:
        ch = (status & 0x0F) + 1
        lsb = data[1] if len(data) > 1 else 0
        msb = data[2] if len(data) > 2 else 0
        val = (msb << 7) | lsb
        return f"Pitch Bend    ch={ch:>2}  val={val:<5}              [{hex_str}]"
    elif status == 0xF0:
        return f"SysEx         [{hex_str}]"
    elif status == 0xFE:
        return None  # suppress Active Sense
    else:
        return f"System        [{hex_str}]"


def _load_config() -> Optional[dict]:
    paths = [
        Path.home() / ".config" / "dlive-midi-bridge" / "config.yaml",
        Path("config/config.yaml"),
    ]
    for p in paths:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return None


def _print_header():
    print()
    print(f"  {BOLD}dLive MIDI Bridge — Live Monitor  v{__version__}{RESET}")
    print(f"  {DIM}Press Ctrl+C to stop{RESET}")
    print()
    print(f"  {DIM}{'Source':<14}  {'Direction':<4}  {'Message'}{RESET}")
    print(f"  {DIM}{'─' * 70}{RESET}")


_msg_count = 0


def _print_midi(source: str, direction: str, data: bytes, color: str):
    global _msg_count
    desc = _decode_midi(data)
    if desc is None:
        return
    _msg_count += 1
    ts = time.strftime("%H:%M:%S")
    print(f"  {DIM}{ts}{RESET}  {color}{source:<12}{RESET}  {direction}  {desc}")
    sys.stdout.flush()


async def run_monitor():
    config = _load_config()
    if not config:
        print("  No config found. Run 'dlive setup' first.")
        return

    dlive_ip = config.get("dlive_ip")
    dlive_port = config.get("dlive_port", DLIVE_MIXRACK_PORT)
    session_name = config.get("session_name", "dLive-MIDI-Bridge")
    local_port = config.get("local_port", 5004)
    bind_ip = config.get("bind_ip")

    _print_header()

    rtp_rx_count = 0
    dlive_rx_count = 0

    def on_rtp_midi(data: bytes):
        nonlocal rtp_rx_count
        rtp_rx_count += 1
        _print_midi("RTP-MIDI", " →", data, CYAN)

    def on_dlive_midi(data: bytes):
        nonlocal dlive_rx_count
        dlive_rx_count += 1
        _print_midi("dLive", " ←", data, YELLOW)

    def on_dlive_connected():
        print(f"  {GREEN}● dLive connected ({dlive_ip}:{dlive_port}){RESET}")

    def on_dlive_disconnected():
        print(f"  {RED}● dLive disconnected{RESET}")

    # Connect to dLive
    print(f"  Connecting to dLive at {dlive_ip}:{dlive_port}...")
    dlive = DLiveTCPConnection(
        host=dlive_ip,
        port=dlive_port,
        on_connected=on_dlive_connected,
        on_disconnected=on_dlive_disconnected,
        midi_callback=on_dlive_midi,
    )
    await dlive.connect()

    # Start RTP-MIDI
    print(f"  Starting RTP-MIDI session '{session_name}' on port {local_port}...")
    receiver = RTPMIDIReceiver(
        midi_callback=on_rtp_midi,
        session_name=f"{session_name}-mon",
        local_port=local_port + 10,
        bind_ip=bind_ip,
    )
    await receiver.start()

    print()
    print(f"  {GREEN}Monitoring...{RESET}  (MIDI messages will appear below)")
    print()

    # Print peer status periodically
    async def peer_status():
        await asyncio.sleep(3)
        peers = sum(
            1 for p in receiver._session._peers.values() if p.connected
        ) if receiver._session else 0
        print(
            f"  {DIM}RTP peers: {peers} | "
            f"dLive: {'connected' if dlive.connected else 'disconnected'}{RESET}"
        )
        while True:
            await asyncio.sleep(30)
            peers = sum(
                1 for p in receiver._session._peers.values() if p.connected
            ) if receiver._session else 0
            print(
                f"  {DIM}[{time.strftime('%H:%M:%S')}] "
                f"RTP peers: {peers} | "
                f"RTP rx: {rtp_rx_count} | dLive rx: {dlive_rx_count}{RESET}"
            )

    asyncio.create_task(peer_status())

    # Run until interrupted
    stop = asyncio.Event()
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await receiver.stop()
        await dlive.disconnect()
