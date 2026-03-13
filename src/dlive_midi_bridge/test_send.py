"""
Send test MIDI messages directly to a dLive over TCP.

Bypasses RTP-MIDI entirely — connects straight to the dLive's TCP port
and sends program changes, CC messages, or note on/off for verification.

Usage:
    dlive-test-send --dlive-ip 192.168.1.80
    dlive-test-send --dlive-ip 192.168.1.80 --channel 1 --program 5
    dlive-test-send --dlive-ip 192.168.1.80 --sweep
"""

import argparse
import asyncio
import logging
import sys
import time

from .dlive_tcp import DLiveTCPConnection, DLIVE_MIXRACK_PORT, DLIVE_SURFACE_PORT


logger = logging.getLogger(__name__)


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


async def run_test(args):
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    port = args.dlive_port or (
        DLIVE_SURFACE_PORT if args.target == "surface" else DLIVE_MIXRACK_PORT
    )
    channel = args.channel - 1  # 1-16 → 0-15

    connected_event = asyncio.Event()

    def on_connected():
        connected_event.set()

    conn = DLiveTCPConnection(
        host=args.dlive_ip,
        port=port,
        on_connected=on_connected,
    )

    print(f"Connecting to dLive at {args.dlive_ip}:{port}...")
    await conn.connect()

    try:
        await asyncio.wait_for(connected_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        print("ERROR: Could not connect to dLive within 10 seconds.", file=sys.stderr)
        print("Check the IP address and make sure the console is reachable.", file=sys.stderr)
        await conn.disconnect()
        sys.exit(1)

    print(f"Connected! Sending test messages on MIDI channel {args.channel}...\n")

    if args.sweep:
        print(f"Sweeping Program Changes 0-{args.sweep_max} (one per second)...")
        for prog in range(args.sweep_max + 1):
            msg = build_program_change(channel, prog)
            conn.send_midi(msg)
            print(f"  Program Change → {prog}  [{msg.hex(' ')}]")
            await asyncio.sleep(1.0)

    elif args.cc is not None:
        msg = build_cc(channel, args.cc, args.cc_value)
        conn.send_midi(msg)
        print(f"  CC {args.cc} = {args.cc_value}  [{msg.hex(' ')}]")

    elif args.note is not None:
        msg_on = build_note_on(channel, args.note, args.velocity)
        conn.send_midi(msg_on)
        print(f"  Note On  {args.note} vel={args.velocity}  [{msg_on.hex(' ')}]")
        await asyncio.sleep(args.duration)
        msg_off = build_note_off(channel, args.note)
        conn.send_midi(msg_off)
        print(f"  Note Off {args.note}  [{msg_off.hex(' ')}]")

    else:
        prog = args.program
        msg = build_program_change(channel, prog)
        conn.send_midi(msg)
        print(f"  Program Change → {prog}  [{msg.hex(' ')}]")

    await conn.flush()
    await asyncio.sleep(0.5)

    print("\nDone. Disconnecting.")
    await conn.disconnect()


def main():
    parser = argparse.ArgumentParser(
        prog="dlive-test-send",
        description="Send test MIDI messages directly to a dLive console over TCP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --dlive-ip 192.168.1.80                        # Program Change 0\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --program 5            # Program Change 5\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --sweep                # PC 0→9, one/sec\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --sweep --sweep-max 20 # PC 0→20\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --cc 7 --cc-value 100  # CC7 (volume)\n"
            "  %(prog)s --dlive-ip 192.168.1.80 --note 60              # Middle C\n"
        ),
    )

    parser.add_argument("--dlive-ip", required=True, help="IP address of the dLive")
    parser.add_argument("--dlive-port", type=int, help="TCP port (default: auto by target)")
    parser.add_argument(
        "--target", choices=["mixrack", "surface"], default="mixrack",
        help="MixRack (51325) or Surface (51328). Default: mixrack",
    )
    parser.add_argument(
        "--channel", type=int, default=1, choices=range(1, 17), metavar="1-16",
        help="MIDI channel (default: 1)",
    )

    group = parser.add_argument_group("message type (pick one)")
    group.add_argument("--program", type=int, default=0, metavar="0-127", help="Program number (default: 0)")
    group.add_argument("--sweep", action="store_true", help="Send program changes 0→N, one per second")
    group.add_argument("--sweep-max", type=int, default=9, metavar="N", help="Last program number for sweep (default: 9)")
    group.add_argument("--cc", type=int, metavar="0-127", help="Send a CC message (controller number)")
    group.add_argument("--cc-value", type=int, default=127, metavar="0-127", help="CC value (default: 127)")
    group.add_argument("--note", type=int, metavar="0-127", help="Send a Note On/Off")
    group.add_argument("--velocity", type=int, default=100, metavar="0-127", help="Note velocity (default: 100)")
    group.add_argument("--duration", type=float, default=1.0, help="Note duration in seconds (default: 1.0)")

    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    try:
        asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print("\nAborted.")


if __name__ == "__main__":
    main()
