"""
Bidirectional MIDI bridge: RTP-MIDI <-> dLive TCP.

This is the core orchestrator that:
  1. Starts the RTP-MIDI receiver (Bonjour discovery + session handler)
  2. Connects to the dLive via TCP
  3. Pipes MIDI from network/USB → dLive TCP (forward path)
  4. Pipes MIDI from dLive TCP → RTP-MIDI network (return path)
  5. Provides status/stats for monitoring
"""

import asyncio
import logging
import signal
from typing import Optional

from .rtp_midi import RTPMIDIReceiver
from .dlive_tcp import DLiveTCPConnection, DLIVE_MIXRACK_PORT, DLIVE_SURFACE_PORT
from .local_midi import LocalMIDIInput

logger = logging.getLogger(__name__)


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

        self._dlive: Optional[DLiveTCPConnection] = None
        self._receiver: Optional[RTPMIDIReceiver] = None
        self._local_midi: Optional[LocalMIDIInput] = None
        self._running = False
        self._midi_count = 0
        self._midi_return_count = 0

    def _on_midi_received(self, data: bytes):
        """
        Callback: MIDI bytes received from RTP-MIDI → forward to dLive.

        Optionally filter by MIDI channel if configured.
        """
        if not data:
            return

        # Optional channel filter
        if self.midi_channel is not None and len(data) >= 1:
            status = data[0]
            if 0x80 <= status <= 0xEF:
                msg_channel = status & 0x0F
                if msg_channel != self.midi_channel:
                    return

        self._midi_count += 1

        if self.log_midi:
            self._log_midi_message(data)

        if self._dlive:
            self._dlive.send_midi(data)

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
        Callback: MIDI bytes received from dLive TCP → forward to RTP-MIDI peers.

        This is the return path: dLive scene feedback, fader moves, etc.
        sent back to the network so other devices can see them.
        """
        if not data:
            return

        self._midi_return_count += 1

        if self.log_midi:
            self._log_midi_message(data)

        if self._receiver:
            self._receiver.send_midi(data)

    def _on_dlive_connected(self):
        logger.info("=== dLive connection ACTIVE — bridge is live ===")

    def _on_dlive_disconnected(self):
        logger.warning("=== dLive connection LOST — will reconnect ===")

    async def start(self):
        """Start the bridge."""
        self._running = True

        logger.info("=" * 60)
        logger.info("  dLive MIDI Bridge")
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

        logger.info("Bridge running. Waiting for MIDI...")

        # Periodic status
        asyncio.create_task(self._status_loop())

    async def _status_loop(self):
        """Print periodic status info."""
        while self._running:
            await asyncio.sleep(30)
            dlive_status = "CONNECTED" if self._dlive and self._dlive.connected else "DISCONNECTED"
            stats = self._dlive.stats if self._dlive else {}
            logger.info(
                f"Status: dLive={dlive_status} | "
                f"MIDI in→dLive={self._midi_count} | "
                f"dLive→network={self._midi_return_count} | "
                f"TCP bytes sent={stats.get('bytes_sent', 0)} | "
                f"Active Sense rx={stats.get('active_sense_received', 0)}"
            )

    async def stop(self):
        """Stop the bridge gracefully."""
        self._running = False
        logger.info("Shutting down bridge...")
        if self._local_midi:
            self._local_midi.stop()
        if self._receiver:
            await self._receiver.stop()
        if self._dlive:
            await self._dlive.disconnect()
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
