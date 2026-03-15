"""
Allen & Heath dLive TCP MIDI sender.

Manages a persistent TCP connection to a dLive MixRack or Surface and
forwards raw MIDI bytes. The dLive accepts standard MIDI messages on
TCP port 51325 (unencrypted) with no additional framing — just raw bytes
on the wire.

The dLive sends 0xFE (Active Sense) every ~300ms as a keepalive.
We use this to detect connection health.

Reference:
  Allen & Heath — dLive MIDI Over TCP/IP Protocol, Firmware V2.0
"""

import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Default ports per the A&H spec
DLIVE_MIXRACK_PORT = 51325          # MixRack, no encryption
DLIVE_MIXRACK_PORT_TLS = 51327     # MixRack, TLS
DLIVE_SURFACE_PORT = 51328          # Surface, no encryption
DLIVE_SURFACE_PORT_TLS = 51329     # Surface, TLS

MIDI_ACTIVE_SENSE = 0xFE
KEEPALIVE_TIMEOUT = 5.0  # seconds without active sense = stale


class DLiveTCPConnection:
    """
    Persistent TCP connection to a dLive console for sending raw MIDI.

    Features:
      - Auto-reconnect on connection loss
      - Monitors incoming Active Sense (0xFE) for connection health
      - Thread-safe write via asyncio
      - Configurable target (MixRack vs Surface) and port

    Usage:
        conn = DLiveTCPConnection(host="192.168.1.80")
        await conn.connect()
        conn.send_midi(bytes([0x90, 0x00, 0x7F]))  # Note On
    """

    def __init__(
        self,
        host: str,
        port: int = DLIVE_MIXRACK_PORT,
        reconnect_interval: float = 3.0,
        on_connected: Optional[Callable] = None,
        on_disconnected: Optional[Callable] = None,
        midi_callback: Optional[Callable[[bytes], None]] = None,
    ):
        self.host = host
        self.port = port
        self.reconnect_interval = reconnect_interval
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.midi_callback = midi_callback

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._last_active_sense = 0.0
        self._reconnect_task: Optional[asyncio.Task] = None
        self._read_task: Optional[asyncio.Task] = None
        self._stats = {
            "midi_messages_sent": 0,
            "bytes_sent": 0,
            "active_sense_received": 0,
            "reconnects": 0,
        }

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def connect(self):
        """Establish TCP connection to the dLive."""
        try:
            logger.info(f"Connecting to dLive at {self.host}:{self.port}...")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=10.0,
            )
            self._connected = True
            self._last_active_sense = time.monotonic()
            logger.info(f"Connected to dLive at {self.host}:{self.port}")

            if self.on_connected:
                self.on_connected()

            # Start reading incoming data (Active Sense, etc.)
            self._read_task = asyncio.create_task(self._read_loop())

        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"Connection to dLive failed: {e}")
            self._connected = False
            self._schedule_reconnect()

    @staticmethod
    def _midi_msg_length(status: int) -> int:
        """Return expected data byte count for a given MIDI status byte."""
        kind = status & 0xF0
        if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            return 2  # two data bytes
        if kind in (0xC0, 0xD0):
            return 1  # one data byte
        return 0  # system realtime / unknown

    async def _read_loop(self):
        """Read incoming data from the dLive and parse MIDI messages."""
        midi_buf = bytearray()
        expected_len = 0
        try:
            while self._connected and self._reader:
                data = await self._reader.read(1024)
                if not data:
                    logger.warning("dLive closed the connection")
                    break

                for byte_val in data:
                    if byte_val == MIDI_ACTIVE_SENSE:
                        self._last_active_sense = time.monotonic()
                        self._stats["active_sense_received"] += 1
                        continue

                    # System realtime (0xF8-0xFF except 0xFE) — single byte, pass through
                    if byte_val >= 0xF8:
                        if self.midi_callback:
                            self.midi_callback(bytes([byte_val]))
                        continue

                    # Status byte — start a new message
                    if byte_val & 0x80:
                        # If we had a partial message in the buffer, discard it
                        if midi_buf:
                            logger.debug(f"dLive rx: discarding partial [{midi_buf.hex(' ')}]")
                        midi_buf = bytearray([byte_val])
                        expected_len = self._midi_msg_length(byte_val)
                        # SysEx (0xF0) accumulates until 0xF7
                        if byte_val == 0xF0:
                            expected_len = -1  # variable length
                        elif expected_len == 0:
                            # Single-byte message
                            if self.midi_callback:
                                self.midi_callback(bytes(midi_buf))
                            midi_buf = bytearray()
                        continue

                    # Data byte
                    if not midi_buf:
                        continue  # stray data byte with no status

                    midi_buf.append(byte_val)

                    # SysEx: wait for 0xF7 terminator
                    if midi_buf[0] == 0xF0:
                        if byte_val == 0xF7:
                            if self.midi_callback:
                                self.midi_callback(bytes(midi_buf))
                            midi_buf = bytearray()
                        continue

                    # Channel messages: check if we have all data bytes
                    if len(midi_buf) - 1 >= expected_len:
                        if self.midi_callback:
                            self.midi_callback(bytes(midi_buf))
                        logger.debug(f"dLive rx MIDI: [{bytes(midi_buf).hex(' ')}]")
                        midi_buf = bytearray()

        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f"Read error from dLive: {e}")
        finally:
            await self._handle_disconnect()

    async def _handle_disconnect(self):
        """Handle a lost connection."""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None

        logger.warning("Disconnected from dLive")
        if self.on_disconnected:
            self.on_disconnected()

        self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt."""
        loop = asyncio.get_event_loop()
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already scheduled
        self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Attempt to reconnect at regular intervals."""
        while not self._connected:
            self._stats["reconnects"] += 1
            logger.info(
                f"Reconnecting to dLive in {self.reconnect_interval}s "
                f"(attempt #{self._stats['reconnects']})"
            )
            await asyncio.sleep(self.reconnect_interval)
            await self.connect()

    def send_midi(self, data: bytes):
        """
        Send raw MIDI bytes to the dLive over TCP.

        The dLive expects raw MIDI — no framing, no length prefix.
        Just write the bytes directly to the socket.
        """
        if not self._connected or not self._writer:
            logger.warning(f"Cannot send MIDI — not connected to dLive")
            return False

        try:
            self._writer.write(data)
            # Don't await drain for every message — too slow for real-time MIDI.
            # The write buffer handles it. We'll catch errors in _read_loop.
            self._stats["midi_messages_sent"] += 1
            self._stats["bytes_sent"] += len(data)
            logger.debug(f"MIDI tx [{len(data)} bytes]: {data.hex(' ')}")
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f"Send failed: {e}")
            asyncio.get_event_loop().create_task(self._handle_disconnect())
            return False

    async def flush(self):
        """Flush the TCP write buffer (call periodically or after bursts)."""
        if self._writer:
            try:
                await self._writer.drain()
            except Exception:
                pass

    async def disconnect(self):
        """Gracefully close the connection."""
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("Disconnected from dLive (graceful)")
