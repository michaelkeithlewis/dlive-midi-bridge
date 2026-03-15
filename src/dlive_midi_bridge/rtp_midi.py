"""
RTP-MIDI (Apple Network MIDI) receiver.

Implements the minimal subset of the RTP-MIDI protocol (RFC 6295 / Apple
extension) needed to receive MIDI data from a network MIDI session advertised
via Bonjour (mDNS/DNS-SD).

Architecture:
  - Bonjour browser finds services of type "_apple-midi._udp"
  - For each discovered peer, we initiate an RTP-MIDI session (AppleMIDI)
  - MIDI payload is extracted from incoming RTP packets
  - Extracted bytes are handed to a callback for forwarding

The AppleMIDI session protocol uses two UDP ports:
  - Control port (even): session management (invitation, sync, bye)
  - Data port (odd = control + 1): RTP packets carrying MIDI payload
"""

import asyncio
import logging
import struct
import time
import random
from typing import Callable, Optional

from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo, ServiceStateChange

logger = logging.getLogger(__name__)

# ── AppleMIDI signature & command words ──────────────────────────────
APPLEMIDI_SIGNATURE = 0xFFFF
CMD_INVITATION      = 0x494E  # "IN"
CMD_INVITATION_ACK  = 0x4F4B  # "OK"
CMD_INVITATION_REJ  = 0x4E4F  # "NO"
CMD_SYNC            = 0x434B  # "CK"
CMD_BYE             = 0x4259  # "BY"

# RTP header constants
RTP_VERSION = 2
RTP_MIDI_PAYLOAD_TYPE = 97  # typical for RTP-MIDI


class AppleMIDISession:
    """Manages a single RTP-MIDI session with a remote peer."""

    def __init__(
        self,
        name: str,
        peer_addr: tuple[str, int],
        midi_callback: Callable[[bytes], None],
        local_port: int = 5004,
    ):
        self.name = name
        self.peer_addr = peer_addr  # (host, control_port)
        self.midi_callback = midi_callback
        self.local_port = local_port

        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.initiator_token = random.randint(0, 0xFFFFFFFF)
        self.peer_ssrc: Optional[int] = None
        self.connected = False

        self._control_transport: Optional[asyncio.DatagramTransport] = None
        self._data_transport: Optional[asyncio.DatagramTransport] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._sequence = 0

    # ── Protocol message builders ────────────────────────────────────

    def _build_invitation(self, token: int) -> bytes:
        """Build an AppleMIDI invitation packet (IN)."""
        name_bytes = self.name.encode("utf-8") + b"\x00"
        return struct.pack(
            ">HHIII",
            APPLEMIDI_SIGNATURE,
            CMD_INVITATION,
            2,  # protocol version
            token,
            self.ssrc,
        ) + name_bytes

    def _build_invitation_ack(self, token: int) -> bytes:
        """Build an AppleMIDI invitation accepted packet (OK)."""
        name_bytes = self.name.encode("utf-8") + b"\x00"
        return struct.pack(
            ">HHIII",
            APPLEMIDI_SIGNATURE,
            CMD_INVITATION_ACK,
            2,
            token,
            self.ssrc,
        ) + name_bytes

    def _build_sync(self, count: int, timestamps: list[int]) -> bytes:
        """Build an AppleMIDI sync (CK) packet."""
        pkt = struct.pack(
            ">HHIB",
            APPLEMIDI_SIGNATURE,
            CMD_SYNC,
            self.ssrc,
            count,
        )
        # Pad to include 3 timestamp slots (each 8 bytes)
        for i in range(3):
            ts = timestamps[i] if i < len(timestamps) else 0
            pkt += struct.pack(">Q", ts)
        return pkt

    def _build_bye(self) -> bytes:
        """Build an AppleMIDI bye (BY) packet."""
        return struct.pack(
            ">HHII",
            APPLEMIDI_SIGNATURE,
            CMD_BYE,
            2,
            self.ssrc,
        )

    # ── Timestamp helper ─────────────────────────────────────────────

    @staticmethod
    def _now_ts() -> int:
        """Return a microsecond timestamp suitable for sync packets."""
        return int(time.time() * 10000) & 0xFFFFFFFFFFFFFFFF

    # ── RTP-MIDI payload extraction ──────────────────────────────────

    def _extract_midi_from_rtp(self, data: bytes) -> Optional[bytes]:
        """
        Parse an RTP packet and extract the MIDI command section.

        RTP-MIDI payload format (RFC 6295, simplified):
          - RTP header: 12 bytes minimum
          - MIDI command section starts with a 1-byte header:
              Bit 7 (B): 1 if long header (length in 12 bits), 0 if short (4 bits)
              Bit 6 (J): 1 if journal present
              Bit 5 (Z): 1 if delta-time for first command
              Bit 4 (P): phantom flag
              Bits 3-0: length of MIDI list (short header)
        """
        if len(data) < 13:  # 12 byte RTP header + at least 1 byte payload
            return None

        # Quick RTP sanity check
        first_byte = data[0]
        version = (first_byte >> 6) & 0x03
        if version != RTP_VERSION:
            return None

        # Skip RTP header (12 bytes for no CSRC, no extension)
        cc = first_byte & 0x0F  # CSRC count
        has_extension = (first_byte >> 4) & 0x01
        offset = 12 + (cc * 4)

        if has_extension and len(data) > offset + 4:
            ext_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
            offset += 4 + (ext_length * 4)

        if offset >= len(data):
            return None

        # Parse the MIDI command section header
        midi_header = data[offset]
        b_flag = (midi_header >> 7) & 1  # long header
        # j_flag = (midi_header >> 6) & 1  # journal present
        z_flag = (midi_header >> 5) & 1  # delta-time on first cmd

        if b_flag:
            # Long header: length is 12 bits across 2 bytes
            if offset + 1 >= len(data):
                return None
            midi_len = ((midi_header & 0x0F) << 8) | data[offset + 1]
            offset += 2
        else:
            # Short header: length is lower 4 bits
            midi_len = midi_header & 0x0F
            offset += 1

        if midi_len == 0:
            return None

        midi_data = data[offset : offset + midi_len]

        # Strip delta-time prefixes if present (variable-length encoded)
        if z_flag and len(midi_data) > 0:
            midi_data = self._strip_delta_times(midi_data)

        return midi_data if midi_data else None

    @staticmethod
    def _strip_delta_times(data: bytes) -> bytes:
        """
        Strip variable-length delta-time values from MIDI command list.

        In RTP-MIDI, delta times are variable-length quantities that precede
        each MIDI command. We strip them to get raw MIDI bytes.
        """
        result = bytearray()
        i = 0
        running_status = 0

        while i < len(data):
            # Skip delta-time (variable length: MSB set = more bytes follow)
            while i < len(data) and (data[i] & 0x80) and data[i] < 0x80:
                i += 1
            # One more delta byte (MSB clear)
            if i < len(data) and data[i] < 0x80:
                i += 1

            if i >= len(data):
                break

            # Now read the MIDI message
            status = data[i]
            if status & 0x80:
                # New status byte
                running_status = status
                result.append(status)
                i += 1
            else:
                # Running status — reuse previous status
                if running_status:
                    result.append(running_status)
                result.append(data[i])
                i += 1

            # Read data bytes for this message
            while i < len(data) and not (data[i] & 0x80):
                result.append(data[i])
                i += 1

        return bytes(result)

    # ── RTP-MIDI send ──────────────────────────────────────────────

    def _build_rtp_midi_packet(self, midi_data: bytes) -> bytes:
        """Wrap raw MIDI bytes in an RTP packet for sending to a peer."""
        self._sequence = (self._sequence + 1) & 0xFFFF
        timestamp = self._now_ts() & 0xFFFFFFFF

        # RTP header: V=2, P=0, X=0, CC=0, M=1, PT=97
        rtp_header = struct.pack(
            ">BBHII",
            (RTP_VERSION << 6) | 0,  # V=2, P=0, X=0, CC=0
            0x80 | RTP_MIDI_PAYLOAD_TYPE,  # M=1, PT=97
            self._sequence,
            timestamp,
            self.ssrc,
        )

        # MIDI command section: short header (B=0, J=0, Z=0, P=0, len=N)
        midi_len = len(midi_data)
        if midi_len > 15:
            # Long header: B=1
            midi_header = bytes([
                0x80 | ((midi_len >> 8) & 0x0F),
                midi_len & 0xFF,
            ])
        else:
            midi_header = bytes([midi_len & 0x0F])

        return rtp_header + midi_header + midi_data

    def send_midi(self, data: bytes):
        """Send MIDI bytes to the connected peer via RTP."""
        if not self.connected or not self._data_transport or not self.peer_addr:
            return
        packet = self._build_rtp_midi_packet(data)
        data_addr = (self.peer_addr[0], self.peer_addr[1] + 1)
        try:
            self._data_transport.sendto(packet, data_addr)
            logger.debug(f"RTP-MIDI tx [{len(data)} bytes]: {data.hex(' ')}")
        except Exception as e:
            logger.warning(f"RTP-MIDI send failed: {e}")

    # ── UDP protocol handlers ────────────────────────────────────────

    class _ControlProtocol(asyncio.DatagramProtocol):
        """Handles the control port (session management)."""

        def __init__(self, session: "AppleMIDISession"):
            self.session = session

        def connection_made(self, transport):
            self.session._control_transport = transport

        def datagram_received(self, data: bytes, addr):
            self.session._handle_control_message(data, addr)

        def error_received(self, exc):
            logger.warning(f"Control port error: {exc}")

    class _DataProtocol(asyncio.DatagramProtocol):
        """Handles the data port (RTP packets with MIDI payload)."""

        def __init__(self, session: "AppleMIDISession"):
            self.session = session

        def connection_made(self, transport):
            self.session._data_transport = transport

        def datagram_received(self, data: bytes, addr):
            self.session._handle_data_message(data, addr)

        def error_received(self, exc):
            logger.warning(f"Data port error: {exc}")

    def _handle_control_message(self, data: bytes, addr: tuple):
        """Process an AppleMIDI control message."""
        if len(data) < 8:
            return

        sig, cmd = struct.unpack(">HH", data[:4])
        if sig != APPLEMIDI_SIGNATURE:
            return

        if cmd == CMD_INVITATION:
            # Peer is inviting us — accept
            _, _, _ver, token, peer_ssrc = struct.unpack(">HHIII", data[:16])
            self.peer_ssrc = peer_ssrc
            logger.info(f"Received invitation from {addr}, accepting")
            ack = self._build_invitation_ack(token)
            self._control_transport.sendto(ack, addr)

        elif cmd == CMD_INVITATION_ACK:
            # Our invitation was accepted
            _, _, _ver, token, peer_ssrc = struct.unpack(">HHIII", data[:16])
            self.peer_ssrc = peer_ssrc
            self.connected = True
            logger.info(f"Session established with {addr} (SSRC: {peer_ssrc:#x})")

        elif cmd == CMD_SYNC:
            # Respond to sync (CK) — clock synchronization
            if len(data) >= 36:
                _, _, sender_ssrc, count = struct.unpack(">HHIB", data[:9])
                ts1 = struct.unpack(">Q", data[9:17])[0]
                ts2 = struct.unpack(">Q", data[17:25])[0]
                ts3 = struct.unpack(">Q", data[25:33])[0]

                now = self._now_ts()
                if count == 0:
                    reply = self._build_sync(1, [ts1, now, 0])
                    self._control_transport.sendto(reply, addr)
                elif count == 1:
                    reply = self._build_sync(2, [ts1, ts2, now])
                    self._control_transport.sendto(reply, addr)

        elif cmd == CMD_BYE:
            logger.info(f"Peer {addr} sent BYE, session ending")
            self.connected = False

    def _handle_data_message(self, data: bytes, addr: tuple):
        """Process a data port message — either AppleMIDI control or RTP."""
        if len(data) >= 4:
            sig = struct.unpack(">H", data[:2])[0]
            if sig == APPLEMIDI_SIGNATURE:
                # AppleMIDI control message on data port (invitation for data channel)
                self._handle_control_message(data, addr)
                return

        # Otherwise it's an RTP packet
        midi_bytes = self._extract_midi_from_rtp(data)
        if midi_bytes:
            logger.debug(
                f"MIDI rx [{len(midi_bytes)} bytes]: {midi_bytes.hex(' ')}"
            )
            self.midi_callback(midi_bytes)

    # ── Session lifecycle ────────────────────────────────────────────

    async def start(self, loop: asyncio.AbstractEventLoop, bind_ip: Optional[str] = None):
        """Bind UDP sockets and optionally send invitation to peer."""
        control_port = self.local_port
        data_port = self.local_port + 1
        bind_addr = bind_ip or "0.0.0.0"

        _, _ = await loop.create_datagram_endpoint(
            lambda: self._ControlProtocol(self),
            local_addr=(bind_addr, control_port),
        )
        logger.info(f"Control port bound on {bind_addr}:{control_port}")

        _, _ = await loop.create_datagram_endpoint(
            lambda: self._DataProtocol(self),
            local_addr=(bind_addr, data_port),
        )
        logger.info(f"Data port bound on {bind_addr}:{data_port}")

        # If we have a specific peer, send invitation
        if self.peer_addr:
            await self._send_invitation()

        # Start periodic sync
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def _send_invitation(self):
        """Send invitation to the remote peer on both ports."""
        host, port = self.peer_addr
        inv = self._build_invitation(self.initiator_token)

        logger.info(f"Sending invitation to {host}:{port}")
        # Invite on control port
        self._control_transport.sendto(inv, (host, port))
        # Small delay then invite on data port
        await asyncio.sleep(0.1)
        if self._data_transport:
            self._data_transport.sendto(inv, (host, port + 1))

    async def _sync_loop(self):
        """Periodically send sync (CK) packets to keep session alive."""
        while True:
            await asyncio.sleep(10)
            if self.connected and self._control_transport and self.peer_addr:
                now = self._now_ts()
                sync = self._build_sync(0, [now])
                try:
                    self._control_transport.sendto(sync, self.peer_addr)
                except Exception as e:
                    logger.warning(f"Sync send failed: {e}")

    async def stop(self):
        """Gracefully end the session."""
        if self._sync_task:
            self._sync_task.cancel()
        if self.connected and self._control_transport and self.peer_addr:
            bye = self._build_bye()
            self._control_transport.sendto(bye, self.peer_addr)
        if self._control_transport:
            self._control_transport.close()
        if self._data_transport:
            self._data_transport.close()
        self.connected = False
        logger.info("Session stopped")


class BonjourMIDIBrowser:
    """
    Discovers RTP-MIDI sessions advertised via Bonjour (mDNS).

    Looks for services of type "_apple-midi._udp" on the local network.
    When a matching session is found, calls the on_discovered callback
    with the service name, host, and port.
    """

    SERVICE_TYPE = "_apple-midi._udp.local."

    def __init__(
        self,
        on_discovered: Callable[[str, str, int], None],
        filter_name: Optional[str] = None,
    ):
        self.on_discovered = on_discovered
        self.filter_name = filter_name
        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None

    def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ):
        if state_change == ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info:
                host = info.parsed_addresses()[0] if info.parsed_addresses() else None
                port = info.port
                svc_name = info.name

                if host and port:
                    # Apply optional name filter
                    if self.filter_name:
                        if self.filter_name.lower() not in svc_name.lower():
                            logger.debug(
                                f"Ignoring '{svc_name}' (filter: {self.filter_name})"
                            )
                            return

                    logger.info(
                        f"Discovered RTP-MIDI session: '{svc_name}' at {host}:{port}"
                    )
                    self.on_discovered(svc_name, host, port)

        elif state_change == ServiceStateChange.Removed:
            logger.info(f"RTP-MIDI session removed: {name}")

    def start(self, **zeroconf_kwargs):
        """Start browsing for RTP-MIDI services."""
        self._zeroconf = Zeroconf(**zeroconf_kwargs)
        self._browser = ServiceBrowser(
            self._zeroconf,
            self.SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )
        logger.info("Bonjour browser started, looking for RTP-MIDI sessions...")

    def stop(self):
        """Stop browsing."""
        if self._zeroconf:
            self._zeroconf.close()
        logger.info("Bonjour browser stopped")


class RTPMIDIReceiver:
    """
    High-level receiver: combines Bonjour discovery with session management.

    Usage:
        receiver = RTPMIDIReceiver(
            midi_callback=my_handler,
            session_name="dLive Bridge",
            local_port=5004,
        )
        await receiver.start()
    """

    def __init__(
        self,
        midi_callback: Callable[[bytes], None],
        session_name: str = "dLive-MIDI-Bridge",
        local_port: int = 5004,
        filter_name: Optional[str] = None,
        bind_ip: Optional[str] = None,
    ):
        self.midi_callback = midi_callback
        self.session_name = session_name
        self.local_port = local_port
        self.filter_name = filter_name
        self.bind_ip = bind_ip

        self._sessions: dict[str, AppleMIDISession] = {}
        self._browser: Optional[BonjourMIDIBrowser] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _on_peer_discovered(self, name: str, host: str, port: int):
        """Called when a new RTP-MIDI peer appears on the network."""
        key = f"{host}:{port}"
        if key in self._sessions:
            logger.debug(f"Already tracking session at {key}")
            return

        session = AppleMIDISession(
            name=self.session_name,
            peer_addr=(host, port),
            midi_callback=self.midi_callback,
            local_port=self.local_port,
        )
        self._sessions[key] = session

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                session.start(self._loop, bind_ip=self.bind_ip), self._loop
            )

    async def start(self):
        """Start the receiver: browse for peers and accept sessions."""
        self._loop = asyncio.get_running_loop()

        # Start Bonjour browsing (scoped to interface if bind_ip set)
        zc_kwargs = {}
        if self.bind_ip:
            import ipaddress
            zc_kwargs["interfaces"] = [self.bind_ip]
            logger.info(f"Zeroconf bound to {self.bind_ip}")

        self._browser = BonjourMIDIBrowser(
            on_discovered=self._on_peer_discovered,
            filter_name=self.filter_name,
        )
        self._browser.start(**zc_kwargs)

    def send_midi(self, data: bytes):
        """Send MIDI bytes to all connected RTP-MIDI peers."""
        for session in self._sessions.values():
            session.send_midi(data)

    async def stop(self):
        """Stop all sessions and browsing."""
        if self._browser:
            self._browser.stop()
        for session in self._sessions.values():
            await session.stop()
        self._sessions.clear()
        logger.info("RTP-MIDI receiver stopped")
