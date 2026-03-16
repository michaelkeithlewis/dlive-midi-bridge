"""
Send test MIDI messages directly to a dLive over TCP.

Bypasses RTP-MIDI entirely — connects straight to the dLive's TCP port
and sends program changes, CC messages, or note on/off for verification.

Usage:
    dlive-test-send                                      # interactive mode
    dlive-test-send --dlive-ip 192.168.1.70 --program 5  # scripted mode
"""

import argparse
import asyncio
import logging
import sys

from .dlive_tcp import DLiveTCPConnection, DLIVE_MIXRACK_PORT


logger = logging.getLogger(__name__)

TRUCK_PACKER_BANNER = r"""
                     s p o n s o r e d   b y

  _____ ____  _   _  ____ _  __  ____   _    ____ _  _______ ____
 |_   _|  _ \| | | |/ ___| |/ / |  _ \ / \  / ___| |/ / ____|  _ \
   | | | |_) | | | | |   | ' /  | |_) / _ \| |   | ' /|  _| | |_) |
   | | |  _ <| |_| | |___| . \  |  __/ ___ \ |___| . \| |___|  _ <
   |_| |_| \_\\___/ \____|_|\_\ |_| /_/   \_\____|_|\_\_____|_| \_\
"""


def build_program_change(channel: int, program: int) -> bytes:
    """Build a MIDI Program Change message. Channel 0-15, program 0-127."""
    return bytes([0xC0 | (channel & 0x0F), program & 0x7F])


def build_cc(channel: int, cc: int, value: int) -> bytes:
    """Build a MIDI Control Change message."""
    return bytes([0xB0 | (channel & 0x0F), cc & 0x7F, value & 0x7F])


def build_note_on(channel: int, note: int, velocity: int) -> bytes:
    return bytes([0x90 | (channel & 0x0F), note & 0x7F, velocity & 0x7F])


def build_note_off(channel: int, note: int) -> bytes:
    return bytes([0x80 | (channel & 0x0F), note & 0x7F, 0])


# ── Interactive mode ─────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Bye!")
        sys.exit(0)
    return value if value else default


def _ask_int(prompt: str, default: int, lo: int = 0, hi: int = 127) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            val = int(raw)
            if lo <= val <= hi:
                return val
            print(f"    Must be {lo}-{hi}.")
        except ValueError:
            print(f"    Enter a number ({lo}-{hi}).")


def _ask_choice(prompt: str, options: list[tuple[str, str]], default: int = 0) -> str:
    print(f"  {prompt}")
    for i, (_val, label) in enumerate(options):
        marker = " *" if i == default else "  "
        print(f"   {marker} [{i + 1}] {label}")
    raw = _ask("Choice", str(default + 1))
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except ValueError:
        pass
    return options[default][0]


async def _connect(ip: str, port: int) -> DLiveTCPConnection:
    connected_event = asyncio.Event()

    def on_connected():
        connected_event.set()

    conn = DLiveTCPConnection(host=ip, port=port, on_connected=on_connected)
    print(f"\n  Connecting to {ip}:{port}...")
    await conn.connect()

    try:
        await asyncio.wait_for(connected_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        print("  ERROR: Could not connect within 10 seconds.", file=sys.stderr)
        print("  Check the IP and make sure the dLive is on.", file=sys.stderr)
        await conn.disconnect()
        sys.exit(1)

    print("  Connected!\n")
    return conn


def _load_saved_config() -> dict:
    """Try to load existing config for sensible defaults."""
    from pathlib import Path
    try:
        import yaml
        for p in [
            Path.home() / ".config" / "dlive-midi-bridge" / "config.yaml",
            Path("/etc/dlive-midi-bridge/config.yaml"),
        ]:
            if p.exists():
                with open(p) as f:
                    return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _check_service_running() -> bool:
    """Check if the bridge service is currently running."""
    import platform
    import subprocess
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["launchctl", "list", "com.backlinelogic.dlive-midi-bridge"],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    elif platform.system() == "Linux":
        result = subprocess.run(
            ["systemctl", "is-active", "dlive-midi-bridge"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() == "active"
    return False


async def run_interactive():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(TRUCK_PACKER_BANNER)
    print("  ── dLive Test Sender ─────────────────────────────\n")

    # Warn if the bridge service is already running
    if _check_service_running():
        print("  NOTE: The bridge service is currently running.")
        print("  Test messages will go to the dLive AND get echoed")
        print("  back to all connected MIDI peers on the network.\n")

    # Load saved config for defaults
    saved = _load_saved_config()
    default_ip = saved.get("dlive_ip", "192.168.1.70")
    default_ch = saved.get("midi_channel", 1)

    # Offer network scan or use saved IP
    ip = None
    if default_ip:
        use_saved = _ask(f"dLive IP address", default_ip)
        if use_saved:
            ip = use_saved

    if not ip:
        try:
            from .wizard import scan_for_dlive
            scan = _ask("Scan all networks for dLive? (Y/n)", "Y").lower()
            if scan in ("y", "yes", ""):
                print(f"  Scanning all interfaces ...", end="", flush=True)
                found = scan_for_dlive(
                    progress_callback=lambda d, t: print(
                        f"\r  Scanning all interfaces ... {int(d/t*100)}%",
                        end="", flush=True,
                    )
                )
                print(f"\r  Scanning all interfaces ... done!   \n")
                if found:
                    if len(found) == 1:
                        ip = found[0][0]
                        print(f"  Found: {found[0][2]} at {ip}:{found[0][1]}\n")
                    else:
                        options = [
                            (fip, f"{fip}  ({ftype}, port {fport})")
                            for fip, fport, ftype in found
                        ]
                        ip = _ask_choice("Which dLive?", options)
                else:
                    print("  No dLive consoles found. Enter the IP manually.\n")
        except ImportError:
            pass

    if not ip:
        ip = _ask("dLive IP address", default_ip)
    port = DLIVE_MIXRACK_PORT
    channel = _ask_int("MIDI channel", default_ch, 1, 16)
    ch_zero = channel - 1

    conn = await _connect(ip, port)

    while True:
        msg_type = _ask_choice("What do you want to send?", [
            ("pc",    "Program Change  (recall a scene or preset)"),
            ("cc",    "Control Change  (move a fader, toggle a mute)"),
            ("note",  "Note On / Off   (trigger a cue)"),
            ("sweep", "Sweep Programs  (cycle through scenes one per second)"),
        ])

        if msg_type == "pc":
            prog = _ask_int("Program number", 0, 0, 127)
            msg = build_program_change(ch_zero, prog)
            conn.send_midi(msg)
            await conn.flush()
            print(f"\n  >> Program Change -> {prog} on ch {channel}  [{msg.hex(' ')}]\n")

        elif msg_type == "cc":
            cc_num = _ask_int("Controller number (e.g. 7=volume, 10=pan)", 7, 0, 127)
            cc_val = _ask_int("Value", 127, 0, 127)
            msg = build_cc(ch_zero, cc_num, cc_val)
            conn.send_midi(msg)
            await conn.flush()
            print(f"\n  >> CC {cc_num} = {cc_val} on ch {channel}  [{msg.hex(' ')}]\n")

        elif msg_type == "note":
            note = _ask_int("Note number (60=middle C)", 60, 0, 127)
            vel = _ask_int("Velocity", 100, 0, 127)
            msg_on = build_note_on(ch_zero, note, vel)
            conn.send_midi(msg_on)
            await conn.flush()
            print(f"\n  >> Note On {note} vel={vel} on ch {channel}  [{msg_on.hex(' ')}]")
            await asyncio.sleep(1.0)
            msg_off = build_note_off(ch_zero, note)
            conn.send_midi(msg_off)
            await conn.flush()
            print(f"  >> Note Off {note} on ch {channel}  [{msg_off.hex(' ')}]\n")

        elif msg_type == "sweep":
            sweep_max = _ask_int("Sweep up to program number", 9, 0, 127)
            print(f"\n  Sweeping Program Changes 0-{sweep_max}...")
            for prog in range(sweep_max + 1):
                msg = build_program_change(ch_zero, prog)
                conn.send_midi(msg)
                await conn.flush()
                print(f"    PC -> {prog}  [{msg.hex(' ')}]")
                await asyncio.sleep(1.0)
            print()

        again = _ask("Send another?", "Y").strip().lower()
        if again in ("n", "no"):
            break
        print()

    print("\n  Disconnecting.")
    await conn.disconnect()


# ── Scripted (CLI flags) mode ────────────────────────────────────────

async def run_test(args):
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    port = args.dlive_port or DLIVE_MIXRACK_PORT
    channel = args.channel - 1  # 1-16 → 0-15

    conn = await _connect(args.dlive_ip, port)

    print(f"  Sending on MIDI channel {args.channel}...\n")

    if args.sweep:
        print(f"  Sweeping Program Changes 0-{args.sweep_max} (one per second)...")
        for prog in range(args.sweep_max + 1):
            msg = build_program_change(channel, prog)
            conn.send_midi(msg)
            await conn.flush()
            print(f"    PC -> {prog}  [{msg.hex(' ')}]")
            await asyncio.sleep(1.0)

    elif args.cc is not None:
        msg = build_cc(channel, args.cc, args.cc_value)
        conn.send_midi(msg)
        await conn.flush()
        print(f"  CC {args.cc} = {args.cc_value}  [{msg.hex(' ')}]")

    elif args.note is not None:
        msg_on = build_note_on(channel, args.note, args.velocity)
        conn.send_midi(msg_on)
        await conn.flush()
        print(f"  Note On  {args.note} vel={args.velocity}  [{msg_on.hex(' ')}]")
        await asyncio.sleep(args.duration)
        msg_off = build_note_off(channel, args.note)
        conn.send_midi(msg_off)
        await conn.flush()
        print(f"  Note Off {args.note}  [{msg_off.hex(' ')}]")

    else:
        prog = args.program
        msg = build_program_change(channel, prog)
        conn.send_midi(msg)
        await conn.flush()
        print(f"  Program Change -> {prog}  [{msg.hex(' ')}]")
    await asyncio.sleep(0.5)

    print("\n  Done. Disconnecting.")
    await conn.disconnect()


def main():
    parser = argparse.ArgumentParser(
        prog="dlive-test-send",
        description="Send test MIDI messages directly to a dLive console over TCP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run with no arguments for interactive guided mode.\n\n"
            "Examples (scripted):\n"
            "  %(prog)s --dlive-ip 192.168.1.70 --channel 8 --program 5\n"
            "  %(prog)s --dlive-ip 192.168.1.70 --sweep\n"
            "  %(prog)s --dlive-ip 192.168.1.70 --cc 7 --cc-value 100\n"
            "  %(prog)s --dlive-ip 192.168.1.70 --note 60\n"
        ),
    )

    parser.add_argument("--dlive-ip", help="IP address of the dLive")
    parser.add_argument("--dlive-port", type=int, help="TCP port (default: 51325)")
    parser.add_argument(
        "--channel", type=int, default=1, choices=range(1, 17), metavar="1-16",
        help="MIDI channel (default: 1)",
    )

    group = parser.add_argument_group("message type (pick one)")
    group.add_argument("--program", type=int, default=0, metavar="0-127", help="Program number (default: 0)")
    group.add_argument("--sweep", action="store_true", help="Send program changes 0->N, one per second")
    group.add_argument("--sweep-max", type=int, default=9, metavar="N", help="Last program number for sweep (default: 9)")
    group.add_argument("--cc", type=int, metavar="0-127", help="Send a CC message (controller number)")
    group.add_argument("--cc-value", type=int, default=127, metavar="0-127", help="CC value (default: 127)")
    group.add_argument("--note", type=int, metavar="0-127", help="Send a Note On/Off")
    group.add_argument("--velocity", type=int, default=100, metavar="0-127", help="Note velocity (default: 100)")
    group.add_argument("--duration", type=float, default=1.0, help="Note duration in seconds (default: 1.0)")

    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    try:
        if args.dlive_ip:
            asyncio.run(run_test(args))
        else:
            asyncio.run(run_interactive())
    except KeyboardInterrupt:
        print("\n  Bye!")


if __name__ == "__main__":
    main()
