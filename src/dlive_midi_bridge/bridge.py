"""
Bidirectional MIDI bridge: RTP-MIDI <-> dLive TCP.

This is the core orchestrator that:
  1. Starts the RTP-MIDI receiver (Bonjour discovery + session handler)
  2. Connects to the dLive via TCP
  3. Pipes MIDI from network/USB/virtual → dLive TCP (forward path)
  4. Pipes MIDI from dLive TCP → RTP-MIDI network + virtual port (return path)
  5. Creates a virtual MIDI port so other apps can patch into the bridge
  6. Provides status/stats for monitoring
"""

import asyncio
import json
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .rtp_midi import RTPMIDIReceiver
from .dlive_tcp import DLiveTCPConnection, DLIVE_MIXRACK_PORT, DLIVE_SURFACE_PORT
from .local_midi import LocalMIDIInput

logger = logging.getLogger(__name__)

STATUS_FILE = Path("/tmp/dlive-midi-bridge-status.json")


class VirtualMIDIPort:
    """
    Creates a virtual MIDI device visible to all apps on the system.

    Output port: dLive MIDI appears here (apps receive from it).
    Input port:  apps send MIDI here → forwarded to the dLive.
    """

    def __init__(self, name: str, midi_callback=None):
        self.name = name
        self.midi_callback = midi_callback
        self._out = None
        self._in = None

    def start(self):
        from .local_midi import _get_rtmidi
        rtmidi = _get_rtmidi()
        if rtmidi is None:
            logger.info("Virtual MIDI port skipped — ALSA/rtmidi not available")
            return
        try:
            self._out = rtmidi.MidiOut()
            self._out.open_virtual_port(self.name)
            logger.info(f"Virtual MIDI output port: '{self.name}'")

            self._in = rtmidi.MidiIn()
            self._in.open_virtual_port(self.name)
            self._in.ignore_types(sysex=False, timing=True, active_sense=True)
            if self.midi_callback:
                self._in.set_callback(self._on_input)
            logger.info(f"Virtual MIDI input port: '{self.name}'")
        except Exception as e:
            logger.warning(f"Could not create virtual MIDI port: {e}")
            self._out = None
            self._in = None

    def _on_input(self, event, _data=None):
        message, _delta = event
        if message and self.midi_callback:
            self.midi_callback(bytes(message))

    def send(self, data: bytes):
        if self._out:
            try:
                self._out.send_message(list(data))
            except Exception as e:
                logger.debug(f"Virtual MIDI send error: {e}")

    def stop(self):
        if self._in:
            try:
                self._in.close_port()
            except Exception:
                pass
        if self._out:
            try:
                self._out.close_port()
            except Exception:
                pass
        logger.info("Virtual MIDI port closed")


class MIDIBridge:
    """
    Main bridge: RTP-MIDI in → dLive TCP out.

    Config:
      dlive_host:      IP address of dLive MixRack or Surface
      dlive_port:      TCP port (default 51325 for MixRack)
      local_port:      UDP port for RTP-MIDI (default 5004)
      session_name:    Name to advertise in RTP-MIDI sessions
      filter_name:     Only connect to RTP-MIDI peers matching this string
      midi_channel:    Optional channel filter (None = pass all)
    """

    def __init__(
        self,
        dlive_host: str,
        dlive_port: int = DLIVE_MIXRACK_PORT,
        local_port: int = 5004,
        session_name: str = "dLive-MIDI-Bridge",
        filter_name: Optional[str] = None,
        midi_channel: Optional[int] = None,
        log_midi: bool = False,
        enable_local_midi: bool = False,
        local_midi_filter: Optional[str] = None,
        bind_ip: Optional[str] = None,
        snapshot_note_shim: bool = True,
        snapshot_pc_channel: int = 8,
        snapshot_pc_program: int = 7,
        snapshot_note_hex: str = "98 3C 7F",
        passive_mode: bool = True,
    ):
        self.dlive_host = dlive_host
        self.dlive_port = dlive_port
        self.local_port = local_port
        self.session_name = session_name
        self.filter_name = filter_name
        self.midi_channel = midi_channel
        self.log_midi = log_midi
        self.enable_local_midi = enable_local_midi
        self.local_midi_filter = local_midi_filter
        self.bind_ip = bind_ip
        self.snapshot_note_shim = snapshot_note_shim
        self.snapshot_pc_channel = snapshot_pc_channel
        self.snapshot_pc_program = snapshot_pc_program
        self.passive_mode = passive_mode
        try:
            self.snapshot_note_bytes = bytes.fromhex(snapshot_note_hex)
        except ValueError:
            self.snapshot_note_bytes = b"\x98\x3c\x7f"

        self._dlive: Optional[DLiveTCPConnection] = None
        self._receiver: Optional[RTPMIDIReceiver] = None
        self._local_midi: Optional[LocalMIDIInput] = None
        self._virtual_port: Optional[VirtualMIDIPort] = None
        self._running = False
        self._midi_count = 0
        self._midi_return_count = 0

    def _on_midi_received(self, data: bytes):
        """
        Callback: MIDI bytes received from RTP-MIDI / local / virtual
        → forward to dLive AND relay to all other RTP-MIDI peers.
        """
        if not data:
            return

        # Optional channel filter
        if self.midi_channel is not None and len(data) >= 1:
            status = data[0]
            if 0x80 <= status <= 0xEF:
                msg_channel = status & 0x0F
                if msg_channel != self.midi_channel:
                    logger.debug(
                        f"Channel filter: dropped ch{msg_channel+1} "
                        f"(want ch{self.midi_channel+1}) [{data.hex(' ')}]"
                    )
                    return

        self._midi_count += 1
        logger.info(f"MIDI → dLive: [{data.hex(' ')}]")

        if self.log_midi:
            self._log_midi_message(data)

        # Forward to dLive
        if self._dlive:
            sent = self._dlive.send_midi(data)
            if not sent:
                logger.warning("dLive send FAILED (not connected)")

        # Relay to all RTP-MIDI peers (so SuperRack gets Tracks MIDI, etc.)
        if self._receiver:
            self._receiver.send_midi(data)

    def _log_midi_message(self, data: bytes):
        """Human-readable MIDI message logging."""
        if len(data) < 1:
            return

        status = data[0]
        hex_str = data.hex(" ")

        # Decode common message types
        if 0x80 <= status <= 0x8F:
            ch = status & 0x0F
            note = data[1] if len(data) > 1 else 0
            vel = data[2] if len(data) > 2 else 0
            logger.info(f"MIDI: Note Off  ch={ch+1} note={note} vel={vel}  [{hex_str}]")
        elif 0x90 <= status <= 0x9F:
            ch = status & 0x0F
            note = data[1] if len(data) > 1 else 0
            vel = data[2] if len(data) > 2 else 0
            desc = "Note Off" if vel == 0 else "Note On"
            logger.info(f"MIDI: {desc}  ch={ch+1} note={note} vel={vel}  [{hex_str}]")
        elif 0xB0 <= status <= 0xBF:
            ch = status & 0x0F
            cc = data[1] if len(data) > 1 else 0
            val = data[2] if len(data) > 2 else 0
            logger.info(f"MIDI: CC  ch={ch+1} cc={cc} val={val}  [{hex_str}]")
        elif 0xC0 <= status <= 0xCF:
            ch = status & 0x0F
            prog = data[1] if len(data) > 1 else 0
            logger.info(f"MIDI: Program Change  ch={ch+1} prog={prog}  [{hex_str}]")
        elif 0xE0 <= status <= 0xEF:
            ch = status & 0x0F
            lsb = data[1] if len(data) > 1 else 0
            msb = data[2] if len(data) > 2 else 0
            val = (msb << 7) | lsb
            logger.info(f"MIDI: Pitchbend  ch={ch+1} val={val}  [{hex_str}]")
        elif status == 0xF0:
            logger.info(f"MIDI: SysEx  [{hex_str}]")
        else:
            logger.info(f"MIDI: [{hex_str}]")

    def _on_dlive_midi_received(self, data: bytes):
        """
        Callback: MIDI bytes received from dLive TCP → forward to RTP-MIDI peers
        and to the virtual MIDI port (so local apps like SuperRack can see it).
        """
        if not data:
            return

        self._midi_return_count += 1

        # Always decode dLive→network messages for visibility
        self._log_midi_message(data)
        logger.info(f"dLive → network [{data.hex(' ')}]")

        if self._receiver:
            self._receiver.send_midi(data)
        else:
            logger.warning("No RTP-MIDI receiver — cannot forward to network")

        # Compatibility shim:
        # Some dLive snapshot workflows emit Bank+Program, while show software
        # expects a Note On trigger. Emit the configured note when the expected
        # Program Change is seen.
        if (
            self.snapshot_note_shim
            and len(data) >= 2
            and (data[0] & 0xF0) == 0xC0
            and ((data[0] & 0x0F) + 1) == self.snapshot_pc_channel
            and data[1] == self.snapshot_pc_program
        ):
            logger.info(
                "Snapshot shim: matched Program Change "
                f"ch={self.snapshot_pc_channel} prog={self.snapshot_pc_program}; "
                f"emitting Note [{self.snapshot_note_bytes.hex(' ')}]"
            )
            if self._receiver:
                self._receiver.send_midi(self.snapshot_note_bytes)
            if self._virtual_port:
                self._virtual_port.send(self.snapshot_note_bytes)

        if self._virtual_port:
            self._virtual_port.send(data)

    def _on_dlive_connected(self):
        logger.info("=== dLive connection ACTIVE — bridge is live ===")

    def _on_dlive_disconnected(self):
        logger.warning("=== dLive connection LOST — will reconnect ===")

    async def start(self):
        """Start the bridge."""
        self._running = True

        logger.info("=" * 60)
        logger.info(f"  dLive MIDI Bridge  v{__version__}")
        logger.info(f"  dLive target:  {self.dlive_host}:{self.dlive_port}")
        logger.info(f"  RTP-MIDI port: {self.local_port}")
        logger.info(f"  Session name:  {self.session_name}")
        if self.filter_name:
            logger.info(f"  Peer filter:   {self.filter_name}")
        if self.midi_channel is not None:
            logger.info(f"  MIDI channel:  {self.midi_channel + 1}")
        if self.enable_local_midi:
            filt = self.local_midi_filter or "all"
            logger.info(f"  Local MIDI:    enabled (filter: {filt})")
        logger.info(f"  Virtual port:  {self.session_name}")
        logger.info("=" * 60)

        # Start dLive TCP connection (bidirectional)
        self._dlive = DLiveTCPConnection(
            host=self.dlive_host,
            port=self.dlive_port,
            on_connected=self._on_dlive_connected,
            on_disconnected=self._on_dlive_disconnected,
            midi_callback=self._on_dlive_midi_received,
        )
        await self._dlive.connect()

        # Start RTP-MIDI receiver
        self._receiver = RTPMIDIReceiver(
            midi_callback=self._on_midi_received,
            session_name=self.session_name,
            local_port=self.local_port,
            filter_name=self.filter_name,
            bind_ip=self.bind_ip,
            passive_mode=self.passive_mode,
        )
        await self._receiver.start()

        # Start local MIDI input (USB controllers, hardware interfaces)
        if self.enable_local_midi:
            self._local_midi = LocalMIDIInput(
                midi_callback=self._on_midi_received,
                port_name_filter=self.local_midi_filter,
                log_midi=self.log_midi,
            )
            self._local_midi.start()

        # Create virtual MIDI port (shows up as a device in other apps)
        self._virtual_port = VirtualMIDIPort(
            name=self.session_name,
            midi_callback=self._on_midi_received,
        )
        self._virtual_port.start()

        logger.info("Bridge running. Waiting for MIDI...")

        # Write status immediately so `dlive status` works right away
        self._log_status()
        asyncio.create_task(self._status_loop())

    async def _status_loop(self):
        """Write status file frequently so CLI always has fresh data."""
        while self._running:
            await asyncio.sleep(5)
            self._log_status()

    def _log_status(self):
        dlive_status = "CONNECTED" if self._dlive and self._dlive.connected else "DISCONNECTED"
        stats = self._dlive.stats if self._dlive else {}

        peers_list = []
        rtp_peers = 0
        if self._receiver and self._receiver._session:
            for addr, p in self._receiver._session._peers.items():
                peers_list.append({
                    "host": addr[0],
                    "port": addr[1],
                    "connected": bool(p.connected),
                    "can_send": bool(p.can_send),
                    "ctrl_ok": bool(p.ctrl_ok),
                    "data_ok": bool(p.data_ok),
                    "data_addr": f"{p.data_addr[0]}:{p.data_addr[1]}",
                    "rx_count": p.rx_count,
                    "tx_count": p.tx_count,
                })
                if p.can_send:
                    rtp_peers += 1

        logger.info(
            f"Status: dLive={dlive_status} | "
            f"RTP peers={rtp_peers} | "
            f"MIDI in→dLive={self._midi_count} | "
            f"dLive→network={self._midi_return_count} | "
            f"Active Sense rx={stats.get('active_sense_received', 0)}"
        )
        for p in peers_list:
            state = "CAN_SEND" if p["can_send"] else "NO_SEND"
            ctrl = "✓" if p["ctrl_ok"] else "·"
            data = "✓" if p["data_ok"] else "·"
            logger.info(
                f"    Peer {p['host']}:{p['port']} = {state} "
                f"ctrl={ctrl} data={data} "
                f"rx={p['rx_count']} tx_to={p['data_addr']}"
            )

        self._write_status_file(dlive_status, rtp_peers, peers_list, stats)

    def _write_status_file(self, dlive_status, rtp_peers, peers_list, stats):
        status = {
            "updated": time.time(),
            "version": __version__,
            "dlive": {
                "host": self.dlive_host,
                "port": self.dlive_port,
                "connected": dlive_status == "CONNECTED",
            },
            "rtp_midi": {
                "session_name": self.session_name,
                "local_port": self.local_port,
                "connected_peers": rtp_peers,
                "peers": peers_list,
            },
            "counters": {
                "midi_to_dlive": self._midi_count,
                "dlive_to_network": self._midi_return_count,
                "active_sense_rx": stats.get("active_sense_received", 0),
            },
        }
        try:
            STATUS_FILE.write_text(json.dumps(status, indent=2))
        except Exception:
            pass

    async def stop(self):
        """Stop the bridge gracefully."""
        self._running = False
        logger.info("Shutting down bridge...")
        if self._virtual_port:
            self._virtual_port.stop()
        if self._local_midi:
            self._local_midi.stop()
        if self._receiver:
            await self._receiver.stop()
        if self._dlive:
            await self._dlive.disconnect()
        STATUS_FILE.unlink(missing_ok=True)
        logger.info("Bridge stopped")

    async def run_forever(self):
        """Start the bridge and run until interrupted."""
        await self.start()

        # Handle graceful shutdown on SIGINT/SIGTERM
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass  # Windows

        await stop_event.wait()
        await self.stop()
