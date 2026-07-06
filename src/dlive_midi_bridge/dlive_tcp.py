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
import socket
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Default ports per the A&H spec
DLIVE_MIXRACK_PORT = 51325          # MixRack, no encryption
DLIVE_MIXRACK_PORT_TLS = 51327     # MixRack, TLS
DLIVE_SURFACE_PORT = 51328          # Surface, no encryption
DLIVE_SURFACE_PORT_TLS = 51329     # Surface, TLS

MIDI_ACTIVE_SENSE = 0xFE
# Some dLive firmwares emit Active Sense (0xFE) ~every 300ms; others do not
# send it at all. So the Active-Sense watchdog is ADAPTIVE: it only enforces
# this timeout once we've actually seen Active Sense from this console. For
# consoles that never send it, we rely on TCP keepalive (below) instead.
KEEPALIVE_TIMEOUT = 5.0  # seconds without Active Sense (once seen) = stale

# TCP-level keepalive: the reliable, traffic-independent half-open detector.
# Kernel probes an idle peer; a dead link (yanked cable, console power-cut,
# switch glitch) is detected in ~KEEPIDLE + KEEPINTVL*KEEPCNT seconds even
# when no application data flows and no Active Sense is sent.
TCP_KEEPIDLE_S = 5
TCP_KEEPINTVL_S = 2
TCP_KEEPCNT = 3


class DLiveTCPConnection:
    """
    Persistent TCP connection to a dLive console for sending raw MIDI.

    Features:
      - Auto-reconnect on connection loss
      - Monitors incoming Active Sense (0xFE) for connection health
      - Thread-safe write via asyncio
      - Configurable target (MixRack vs Surface) and port

    Usage:
        conn = DLiveTCPConnection(host="192.168.1.70")
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
        self._active_sense_seen = False      # has THIS console ever sent 0xFE?
        self._last_rx = 0.0                  # monotonic time of last inbound bytes
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closing = False                # True only during graceful shutdown
        self._down_fired = False             # idempotency guard for _mark_down
        self._reconnect_task: Optional[asyncio.Task] = None
        self._read_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
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
            self._loop = asyncio.get_running_loop()
            logger.info(f"Connecting to dLive at {self.host}:{self.port}...")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=10.0,
            )
            self._connected = True
            self._down_fired = False
            self._active_sense_seen = False
            now = time.monotonic()
            self._last_active_sense = now
            self._last_rx = now
            self._enable_tcp_keepalive()
            logger.info(f"Connected to dLive at {self.host}:{self.port}")

            if self.on_connected:
                self.on_connected()

            # Start reading incoming data (Active Sense, etc.) and the
            # connection-health watchdog.
            self._read_task = asyncio.create_task(self._read_loop())
            self._watchdog_task = asyncio.create_task(self._keepalive_watchdog())

        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"Connection to dLive failed: {e}")
            self._connected = False
            self._schedule_reconnect()

    def _enable_tcp_keepalive(self):
        """Turn on TCP keepalive so a half-open link is detected by the kernel
        even when no application data flows. Tuning options are Linux-specific
        and skipped gracefully where unavailable (e.g. macOS during tests)."""
        if not self._writer:
            return
        sock = self._writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, val in (
                ("TCP_KEEPIDLE", TCP_KEEPIDLE_S),
                ("TCP_KEEPINTVL", TCP_KEEPINTVL_S),
                ("TCP_KEEPCNT", TCP_KEEPCNT),
            ):
                opt = getattr(socket, name, None)
                if opt is not None:
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                    except OSError:
                        pass
        except OSError as e:
            logger.debug(f"Could not set TCP keepalive: {e}")

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
        running_status = 0
        try:
            while self._connected and self._reader:
                data = await self._reader.read(1024)
                if not data:
                    self._mark_down("dLive closed the connection (EOF)")
                    return

                self._last_rx = time.monotonic()

                for byte_val in data:
                    if byte_val == MIDI_ACTIVE_SENSE:
                        self._last_active_sense = time.monotonic()
                        self._active_sense_seen = True
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
                        expected_len = 0

                        # Channel voice status bytes support running status
                        if 0x80 <= byte_val <= 0xEF:
                            running_status = byte_val
                            expected_len = self._midi_msg_length(byte_val)
                        # SysEx (0xF0) accumulates until 0xF7
                        elif byte_val == 0xF0:
                            running_status = 0
                            expected_len = -1  # variable length
                        # System common message lengths
                        elif byte_val in (0xF1, 0xF3):
                            running_status = 0
                            expected_len = 1
                        elif byte_val == 0xF2:
                            running_status = 0
                            expected_len = 2
                        elif byte_val in (0xF6,):
                            running_status = 0
                            expected_len = 0
                        elif byte_val == 0xF7:
                            running_status = 0
                            expected_len = 0

                        if expected_len == 0 and byte_val != 0xF0:
                            # Single-byte message
                            if self.midi_callback:
                                self.midi_callback(bytes(midi_buf))
                            midi_buf = bytearray()
                        continue

                    # Data byte
                    if not midi_buf:
                        # Running status: data bytes can arrive without a status byte
                        if running_status:
                            midi_buf = bytearray([running_status, byte_val])
                            expected_len = self._midi_msg_length(running_status)
                            if len(midi_buf) - 1 >= expected_len:
                                if self.midi_callback:
                                    self.midi_callback(bytes(midi_buf))
                                logger.debug(f"dLive rx MIDI (running): [{bytes(midi_buf).hex(' ')}]")
                                midi_buf = bytearray()
                            continue
                        else:
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

        except asyncio.CancelledError:
            # The watchdog (or shutdown) cancelled us after already marking the
            # connection down. Just exit; do not fire another disconnect.
            return
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            self._mark_down(f"read error: {e}")
        finally:
            # Tear down this connection's watchdog (unless we ARE inside it).
            wt = self._watchdog_task
            if wt and wt is not asyncio.current_task():
                wt.cancel()

    def _mark_down(self, reason: str):
        """Mark the connection dead and schedule a reconnect. Idempotent per
        connection and safe to call from the read loop, the watchdog, or
        send_midi (including from a non-loop thread)."""
        if self._down_fired:
            return
        self._down_fired = True
        self._connected = False

        w = self._writer
        self._writer = None
        self._reader = None
        if w:
            try:
                w.close()          # sync close; no await so this is thread-safe
            except Exception:
                pass

        logger.warning(f"=== dLive connection LOST ({reason}) — will reconnect ===")
        if self.on_disconnected:
            try:
                self.on_disconnected()
            except Exception:
                pass

        if not self._closing:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt. Thread-safe: marshals onto the
        event loop so it is valid to call from an rtmidi callback thread too."""
        if self._closing:
            return
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._spawn_reconnect)

    def _spawn_reconnect(self):
        if self._closing or self._connected:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already scheduled
        self._reconnect_task = self._loop.create_task(self._reconnect_loop())

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

    async def _keepalive_watchdog(self):
        """Connection-health watchdog, run once per connection.

        Two checks, each ~1s:
          1. Adaptive Active-Sense timeout — ONLY if this console has actually
             sent 0xFE (some dLive firmwares never do). Avoids falsely killing
             a healthy link on consoles that don't emit Active Sense.
          2. Write backpressure — drain the write buffer with a timeout so a
             stalled-but-open peer (TCP zero-window) is surfaced instead of
             letting the send buffer grow unbounded. On a healthy socket
             drain() returns immediately.

        TCP keepalive (set at connect) is the traffic-independent backstop for
        dead links; this watchdog adds faster, application-level detection.
        """
        try:
            while self._connected:
                await asyncio.sleep(1.0)
                if not self._connected:
                    return

                if self._active_sense_seen:
                    silence = time.monotonic() - self._last_active_sense
                    if silence > KEEPALIVE_TIMEOUT:
                        self._mark_down(
                            f"no Active Sense for {silence:.1f}s "
                            f"(> {KEEPALIVE_TIMEOUT}s; half-open link)"
                        )
                        self._cancel_read_task()
                        return

                w = self._writer
                if w is not None:
                    try:
                        await asyncio.wait_for(w.drain(), timeout=KEEPALIVE_TIMEOUT)
                    except asyncio.TimeoutError:
                        self._mark_down("write buffer stalled (backpressure)")
                        self._cancel_read_task()
                        return
                    except (ConnectionResetError, BrokenPipeError, OSError) as e:
                        self._mark_down(f"drain error: {e}")
                        self._cancel_read_task()
                        return
        except asyncio.CancelledError:
            return

    def _cancel_read_task(self):
        rt = self._read_task
        if rt and rt is not asyncio.current_task() and not rt.done():
            rt.cancel()

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
            self._mark_down(f"send failed: {e}")
            return False

    async def flush(self):
        """Flush the TCP write buffer (call periodically or after bursts)."""
        if self._writer:
            try:
                await self._writer.drain()
            except Exception:
                pass

    async def disconnect(self):
        """Gracefully close the connection. Sets the closing flag first so no
        reconnect is scheduled by the read loop / watchdog teardown (C5)."""
        self._closing = True
        self._down_fired = True   # suppress any _mark_down reconnect scheduling
        self._connected = False
        for task in (self._read_task, self._watchdog_task, self._reconnect_task):
            if task:
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("Disconnected from dLive (graceful)")
