"""
RTP-MIDI (Apple Network MIDI) session manager.

Implements the minimal subset of the RTP-MIDI protocol (RFC 6295 / Apple
extension) needed to send and receive MIDI data over a network session
advertised via Bonjour (mDNS/DNS-SD).

Architecture:
  - We advertise our own session via Bonjour ("_apple-midi._udp")
  - We also browse for remote sessions and invite them
  - A single UDP port pair handles all connected peers
  - Incoming MIDI from any peer is forwarded to the callback
  - Outgoing MIDI is broadcast to all connected peers

The AppleMIDI session protocol uses two UDP ports:
  - Control port (even): session management (invitation, sync, bye)
  - Data port (odd = control + 1): RTP packets carrying MIDI payload
"""

import asyncio
import logging
import struct
import subprocess
import shutil
import sys
import time
import random
import socket as _socket
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
RTP_MIDI_PAYLOAD_TYPE = 97


class _PeerInfo:
    """State for a single connected RTP-MIDI peer."""

    __slots__ = ("addr", "ssrc", "data_addr", "ctrl_ok", "data_ok", "rx_count")

    def __init__(self, addr: tuple[str, int], ssrc: int = 0):
        self.addr = addr                                    # (host, control_port)
        self.ssrc = ssrc
        self.data_addr = (addr[0], addr[1] + 1)            # updated on first data-port contact
        self.ctrl_ok = False
        self.data_ok = False
        self.rx_count = 0                                   # MIDI messages received from this peer

    @property
    def can_send(self) -> bool:
        """Can we send to this peer? Yes if we've had ANY interaction."""
        return self.ctrl_ok or self.data_ok

    @property
    def connected(self) -> bool:
        return self.ctrl_ok or self.data_ok


class AppleMIDISession:
    """
    Manages an RTP-MIDI session that can communicate with multiple peers.

    Binds a single pair of UDP sockets (control + data) and tracks each
    peer independently.  Incoming invitations are accepted automatically;
    outgoing invitations are sent via ``invite_peer()``.
    """

    def __init__(
        self,
        name: str,
        midi_callback: Callable[[bytes], None],
        local_port: int = 5004,
    ):
        self.name = name
        self.midi_callback = midi_callback
        self.local_port = local_port

        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self._peers: dict[tuple, _PeerInfo] = {}            # keyed by (host, control_port)

        self._control_transport: Optional[asyncio.DatagramTransport] = None
        self._data_transport: Optional[asyncio.DatagramTransport] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._sequence = 0
        self._tx_count = 0

    # ── Protocol message builders ────────────────────────────────────

    def _build_invitation(self, token: int) -> bytes:
        name_bytes = self.name.encode("utf-8") + b"\x00"
        return struct.pack(
            ">HHIII",
            APPLEMIDI_SIGNATURE,
            CMD_INVITATION,
            2,
            token,
            self.ssrc,
        ) + name_bytes

    def _build_invitation_ack(self, token: int) -> bytes:
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
        # Apple MIDI CK format: sig(2) + cmd(2) + SSRC(4) + count(1) + pad(3) + 3×ts(24) = 36 bytes
        pkt = struct.pack(
            ">HHIB3x",
            APPLEMIDI_SIGNATURE,
            CMD_SYNC,
            self.ssrc,
            count,
        )
        for i in range(3):
            ts = timestamps[i] if i < len(timestamps) else 0
            pkt += struct.pack(">Q", ts)
        return pkt

    def _build_bye(self) -> bytes:
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
        return int(time.time() * 10000) & 0xFFFFFFFFFFFFFFFF

    # ── RTP-MIDI payload extraction ──────────────────────────────────

    def _extract_midi_from_rtp(self, data: bytes) -> Optional[bytes]:
        """
        Parse an RTP packet and extract the MIDI command section.

        RTP-MIDI payload format (RFC 6295, simplified):
          - RTP header: 12 bytes minimum
          - MIDI command section starts with a 1-byte header:
              Bit 7 (B): 1 = long header (length in 12 bits), 0 = short (4 bits)
              Bit 6 (J): 1 = journal present
              Bit 5 (Z): 1 = delta-time for first command
              Bit 4 (P): phantom flag
              Bits 3-0: length of MIDI list (short header)
        """
        if len(data) < 13:
            return None

        first_byte = data[0]
        version = (first_byte >> 6) & 0x03
        if version != RTP_VERSION:
            return None

        cc = first_byte & 0x0F
        has_extension = (first_byte >> 4) & 0x01
        offset = 12 + (cc * 4)

        if has_extension and len(data) > offset + 4:
            ext_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
            offset += 4 + (ext_length * 4)

        if offset >= len(data):
            return None

        midi_header = data[offset]
        b_flag = (midi_header >> 7) & 1
        z_flag = (midi_header >> 5) & 1

        if b_flag:
            if offset + 1 >= len(data):
                return None
            midi_len = ((midi_header & 0x0F) << 8) | data[offset + 1]
            offset += 2
        else:
            midi_len = midi_header & 0x0F
            offset += 1

        if midi_len == 0:
            return None

        midi_data = data[offset : offset + midi_len]

        if z_flag and len(midi_data) > 0:
            midi_data = self._strip_delta_times(midi_data)

        return midi_data if midi_data else None

    @staticmethod
    def _strip_delta_times(data: bytes) -> bytes:
        """Strip variable-length delta-time values from the MIDI command list.

        RTP-MIDI delta-times use VLQ encoding (high bit = continuation)
        interleaved before each MIDI command.  We need to skip them to
        extract raw MIDI bytes.
        """
        result = bytearray()
        i = 0
        running_status = 0
        first_cmd = True

        while i < len(data):
            # Skip VLQ delta-time (continuation bytes have bit 7 set,
            # final byte has bit 7 clear).  The first command always
            # has a delta-time when Z=1; subsequent commands may too.
            if first_cmd or (i < len(data) and data[i] < 0x80):
                # Skip final VLQ byte (bit 7 clear)
                while i < len(data) and data[i] & 0x80 and data[i] < 0xF0:
                    i += 1
                if i < len(data) and data[i] < 0x80:
                    i += 1
                first_cmd = False

            if i >= len(data):
                break

            b = data[i]
            if b & 0x80:
                running_status = b
                result.append(b)
                i += 1
            else:
                if running_status:
                    result.append(running_status)
                result.append(b)
                i += 1

            while i < len(data) and not (data[i] & 0x80):
                result.append(data[i])
                i += 1

        return bytes(result)

    # ── RTP-MIDI send ────────────────────────────────────────────────

    def _build_rtp_midi_packet(self, midi_data: bytes) -> bytes:
        """Wrap raw MIDI bytes in an RTP packet."""
        self._sequence = (self._sequence + 1) & 0xFFFF
        timestamp = self._now_ts() & 0xFFFFFFFF

        rtp_header = struct.pack(
            ">BBHII",
            (RTP_VERSION << 6) | 0,
            0x80 | RTP_MIDI_PAYLOAD_TYPE,
            self._sequence,
            timestamp,
            self.ssrc,
        )

        midi_len = len(midi_data)
        if midi_len > 15:
            midi_header = bytes([
                0x80 | ((midi_len >> 8) & 0x0F),
                midi_len & 0xFF,
            ])
        else:
            midi_header = bytes([midi_len & 0x0F])

        return rtp_header + midi_header + midi_data

    def send_midi(self, data: bytes):
        """Broadcast MIDI bytes to every known peer."""
        if not self._data_transport:
            logger.warning("send_midi: no data transport bound")
            return

        sendable = [p for p in self._peers.values() if p.can_send]
        if not sendable:
            # Log details about every tracked peer so we can diagnose
            for addr, p in self._peers.items():
                logger.warning(
                    f"  Peer {addr}: ctrl={p.ctrl_ok} data={p.data_ok} "
                    f"data_addr={p.data_addr} rx={p.rx_count}"
                )
            logger.warning(
                f"send_midi: 0 sendable peers out of {len(self._peers)} tracked"
            )
            return

        packet = self._build_rtp_midi_packet(data)
        for peer in sendable:
            try:
                self._data_transport.sendto(packet, peer.data_addr)
                self._tx_count += 1
                if self._tx_count <= 3:
                    logger.info(
                        f"*** RTP-MIDI tx #{self._tx_count} → "
                        f"{peer.data_addr[0]}:{peer.data_addr[1]} "
                        f"[{len(data)} bytes]: {data.hex(' ')} "
                        f"(pkt {len(packet)} bytes) ***"
                    )
                else:
                    logger.info(
                        f"RTP-MIDI tx → {peer.data_addr[0]}:{peer.data_addr[1]} "
                        f"[{len(data)} bytes]: {data.hex(' ')}"
                    )
            except Exception as e:
                logger.warning(f"RTP-MIDI send to {peer.data_addr} failed: {e}")

    # ── Peer management ──────────────────────────────────────────────

    def _find_peer_by_data_addr(self, addr: tuple) -> Optional[_PeerInfo]:
        """Find a peer by data port address. Try port-1 first, then IP match."""
        ctrl_key = (addr[0], addr[1] - 1)
        if ctrl_key in self._peers:
            return self._peers[ctrl_key]
        for key, p in self._peers.items():
            if key[0] == addr[0]:
                return p
        return None

    def _get_or_create_peer(self, addr: tuple) -> _PeerInfo:
        key = (addr[0], addr[1])
        if key not in self._peers:
            self._peers[key] = _PeerInfo(key)
            logger.debug(f"New peer registered: {key}")
        return self._peers[key]

    async def invite_peer(self, host: str, port: int):
        """Send an invitation to a remote peer (control + data port handshake)."""
        peer = self._get_or_create_peer((host, port))
        if peer.connected:
            logger.debug(f"Already connected to {host}:{port}")
            return

        token = random.randint(0, 0xFFFFFFFF)
        inv = self._build_invitation(token)

        # Phase 1: control port invitation (retry up to 3 times)
        for attempt in range(3):
            if peer.ctrl_ok:
                break
            logger.info(f"Sending control invitation to {host}:{port} (attempt {attempt + 1})")
            if self._control_transport:
                self._control_transport.sendto(inv, (host, port))
            for _ in range(20):
                await asyncio.sleep(0.1)
                if peer.ctrl_ok:
                    break

        # Phase 2: data port invitation (retry up to 3 times)
        for attempt in range(3):
            if peer.data_ok:
                break
            data_token = random.randint(0, 0xFFFFFFFF)
            data_inv = self._build_invitation(data_token)
            logger.info(f"Sending data invitation to {host}:{port + 1} (attempt {attempt + 1})")
            if self._data_transport:
                self._data_transport.sendto(data_inv, (host, port + 1))
            for _ in range(20):
                await asyncio.sleep(0.1)
                if peer.data_ok:
                    break

        if peer.data_ok:
            logger.info(f"Full handshake complete with {host}:{port} (ctrl=✓ data=✓)")
        elif peer.ctrl_ok:
            logger.warning(
                f"Partial handshake with {host}:{port} (ctrl=✓ data=·) — "
                f"peer may not accept our MIDI data"
            )

    async def _send_data_invitation(self, peer: _PeerInfo):
        """Send a data-port invitation to a peer (used after accepting an incoming control invitation)."""
        await asyncio.sleep(0.05)
        token = random.randint(0, 0xFFFFFFFF)
        inv = self._build_invitation(token)
        if self._data_transport:
            self._data_transport.sendto(inv, peer.data_addr)
            logger.info(f"Sent data-port invitation to {peer.addr[0]}:{peer.addr[1] + 1}")

    @property
    def has_connected_peers(self) -> bool:
        return any(p.connected for p in self._peers.values())

    # ── UDP protocol handlers ────────────────────────────────────────

    class _ControlProtocol(asyncio.DatagramProtocol):
        def __init__(self, session: "AppleMIDISession"):
            self.session = session

        def connection_made(self, transport):
            self.session._control_transport = transport

        def datagram_received(self, data: bytes, addr):
            self.session._handle_control_message(data, addr)

        def error_received(self, exc):
            logger.warning(f"Control port error: {exc}")

    class _DataProtocol(asyncio.DatagramProtocol):
        def __init__(self, session: "AppleMIDISession"):
            self.session = session

        def connection_made(self, transport):
            self.session._data_transport = transport

        def datagram_received(self, data: bytes, addr):
            self.session._handle_data_message(data, addr)

        def error_received(self, exc):
            logger.warning(f"Data port error: {exc}")

    def _handle_apple_midi(self, data: bytes, addr: tuple,
                           reply_transport: asyncio.DatagramTransport,
                           is_data_port: bool = False):
        """Process an AppleMIDI control message from either port."""
        if len(data) < 8:
            return

        sig, cmd = struct.unpack(">HH", data[:4])
        if sig != APPLEMIDI_SIGNATURE:
            return

        # For messages arriving on the data port, find the corresponding
        # control-port peer entry. Try port-1 first, then fall back to IP match.
        if is_data_port:
            ctrl_addr = (addr[0], addr[1] - 1)
            if ctrl_addr not in self._peers:
                for key in self._peers:
                    if key[0] == addr[0]:
                        ctrl_addr = key
                        break
        else:
            ctrl_addr = addr

        if cmd == CMD_INVITATION:
            if len(data) < 16:
                return
            _, _, _ver, token, peer_ssrc = struct.unpack(">HHIII", data[:16])
            peer = self._get_or_create_peer(ctrl_addr)
            peer.ssrc = peer_ssrc

            if is_data_port:
                peer.data_addr = addr
                peer.data_ok = True
            else:
                peer.ctrl_ok = True

            logger.info(
                f"Accepted invitation from {addr} "
                f"({'data' if is_data_port else 'control'} port, "
                f"SSRC: {peer_ssrc:#x}, "
                f"ctrl={'✓' if peer.ctrl_ok else '·'} "
                f"data={'✓' if peer.data_ok else '·'} "
                f"can_send={peer.can_send})"
            )
            ack = self._build_invitation_ack(token)
            reply_transport.sendto(ack, addr)

            # After accepting a control-port invitation, send our own data-port
            # invitation so the peer knows we want bidirectional data flow
            if not is_data_port:
                loop = asyncio.get_event_loop()
                loop.create_task(self._send_data_invitation(peer))

        elif cmd == CMD_INVITATION_ACK:
            if len(data) < 16:
                return
            _, _, _ver, token, peer_ssrc = struct.unpack(">HHIII", data[:16])
            peer = self._get_or_create_peer(ctrl_addr)
            peer.ssrc = peer_ssrc

            if is_data_port:
                peer.data_addr = addr
                peer.data_ok = True
            else:
                peer.ctrl_ok = True

            logger.info(
                f"Invitation accepted by {addr} "
                f"({'data' if is_data_port else 'control'} port, "
                f"SSRC: {peer_ssrc:#x}, "
                f"ctrl={'✓' if peer.ctrl_ok else '·'} "
                f"data={'✓' if peer.data_ok else '·'} "
                f"can_send={peer.can_send})"
            )

        elif cmd == CMD_SYNC:
            # Standard CK: 36 bytes (with 3-byte pad after count)
            # Accept 33+ bytes for compat with non-standard peers
            if len(data) >= 33:
                sender_ssrc = struct.unpack(">I", data[4:8])[0]
                count = data[8]
                # Standard layout: timestamps at offset 12 (after 3 pad bytes)
                ts_off = 12 if len(data) >= 36 else 9
                ts1 = struct.unpack(">Q", data[ts_off:ts_off + 8])[0]
                ts2 = struct.unpack(">Q", data[ts_off + 8:ts_off + 16])[0]
                ts3 = struct.unpack(">Q", data[ts_off + 16:ts_off + 24])[0]

                now = self._now_ts()
                if count == 0:
                    reply = self._build_sync(1, [ts1, now, 0])
                    reply_transport.sendto(reply, addr)
                    logger.debug(f"Sync CK0→CK1 with {addr}")
                elif count == 1:
                    reply = self._build_sync(2, [ts1, ts2, now])
                    reply_transport.sendto(reply, addr)
                    logger.debug(f"Sync CK1→CK2 with {addr}")

        elif cmd == CMD_BYE:
            ctrl_key = (addr[0], addr[1] - 1) if is_data_port else (addr[0], addr[1])
            if ctrl_key in self._peers:
                p = self._peers[ctrl_key]
                p.ctrl_ok = False
                p.data_ok = False
                logger.info(f"Peer {ctrl_key} sent BYE")

    def _handle_control_message(self, data: bytes, addr: tuple):
        if self._control_transport:
            self._handle_apple_midi(data, addr, self._control_transport, is_data_port=False)

    def _handle_data_message(self, data: bytes, addr: tuple):
        if len(data) >= 4:
            sig = struct.unpack(">H", data[:2])[0]
            if sig == APPLEMIDI_SIGNATURE:
                if self._data_transport:
                    self._handle_apple_midi(data, addr, self._data_transport, is_data_port=True)
                return

        # Actual RTP MIDI data — find the peer and capture real data address.
        peer = self._find_peer_by_data_addr(addr)

        if peer:
            peer.data_addr = addr
            peer.data_ok = True
            peer.rx_count += 1
            if peer.rx_count == 1:
                logger.info(
                    f"*** First MIDI from peer {peer.addr} "
                    f"(data_addr={addr}) — return path active ***"
                )

        midi_bytes = self._extract_midi_from_rtp(data)
        if midi_bytes:
            logger.info(
                f"RTP-MIDI rx ← {addr[0]} [{len(midi_bytes)} bytes]: "
                f"{midi_bytes.hex(' ')}"
            )
            self.midi_callback(midi_bytes)
        else:
            if len(data) >= 12:
                logger.debug(
                    f"RTP packet from {addr} but no MIDI extracted "
                    f"(len={len(data)}, hdr={data[:4].hex(' ')})"
                )

    # ── Session lifecycle ────────────────────────────────────────────

    async def start(self, loop: asyncio.AbstractEventLoop, bind_ip: Optional[str] = None):
        """Bind UDP sockets on the control and data ports."""
        control_port = self.local_port
        data_port = self.local_port + 1
        bind_addr = bind_ip or "0.0.0.0"

        # Create reusable sockets so restarts don't fail on TIME_WAIT
        ctrl_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        ctrl_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        if hasattr(_socket, "SO_REUSEPORT"):
            ctrl_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
        ctrl_sock.bind((bind_addr, control_port))

        data_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        data_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        if hasattr(_socket, "SO_REUSEPORT"):
            data_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
        data_sock.bind((bind_addr, data_port))

        _, _ = await loop.create_datagram_endpoint(
            lambda: self._ControlProtocol(self),
            sock=ctrl_sock,
        )
        logger.info(f"Control port bound on {bind_addr}:{control_port}")

        _, _ = await loop.create_datagram_endpoint(
            lambda: self._DataProtocol(self),
            sock=data_sock,
        )
        logger.info(f"Data port bound on {bind_addr}:{data_port}")

        self._sync_task = asyncio.create_task(self._sync_loop())

    async def _sync_loop(self):
        """Periodically send sync (CK) packets to all connected peers."""
        while True:
            await asyncio.sleep(10)
            if not self._control_transport:
                continue
            now = self._now_ts()
            sync = self._build_sync(0, [now])
            for peer in list(self._peers.values()):
                if peer.connected:
                    try:
                        self._control_transport.sendto(sync, peer.addr)
                    except Exception as e:
                        logger.warning(f"Sync to {peer.addr} failed: {e}")

    async def stop(self):
        """Send BYE to all peers and close transports."""
        if self._sync_task:
            self._sync_task.cancel()
        bye = self._build_bye()
        for peer in self._peers.values():
            if peer.connected and self._control_transport:
                try:
                    self._control_transport.sendto(bye, peer.addr)
                except Exception:
                    pass
        if self._control_transport:
            self._control_transport.close()
        if self._data_transport:
            self._data_transport.close()
        self._peers.clear()
        logger.info("Session stopped")


# ── Bonjour browser ──────────────────────────────────────────────────

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
        self._zeroconf = Zeroconf(**zeroconf_kwargs)
        self._browser = ServiceBrowser(
            self._zeroconf,
            self.SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )
        logger.info("Bonjour browser started, looking for RTP-MIDI sessions...")

    def stop(self):
        if self._zeroconf:
            self._zeroconf.close()
        logger.info("Bonjour browser stopped")


# ── Bonjour advertiser ───────────────────────────────────────────────

class BonjourMIDIAdvertiser:
    """
    Advertises an RTP-MIDI session via Bonjour so other devices can
    discover and connect to us (e.g., Audio MIDI Setup on macOS,
    rtpMIDI on Windows).

    On Linux, uses avahi-publish (via avahi-daemon) for reliable mDNS
    advertisement. Falls back to Python Zeroconf on other platforms.
    """

    SERVICE_TYPE = "_apple-midi._udp.local."

    def __init__(self, name: str, port: int, bind_ip: Optional[str] = None):
        self.name = name
        self.port = port
        self.bind_ip = bind_ip
        self._zeroconf: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._avahi_proc: Optional[subprocess.Popen] = None

    def _try_avahi_publish(self) -> bool:
        """Use avahi-publish for reliable Linux mDNS advertisement."""
        if sys.platform != "linux":
            return False
        avahi = shutil.which("avahi-publish")
        if not avahi:
            logger.debug("avahi-publish not found, will use Python Zeroconf")
            return False
        try:
            cmd = [avahi, "-s", self.name, "_apple-midi._udp", str(self.port)]
            self._avahi_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            time.sleep(0.5)
            if self._avahi_proc.poll() is not None:
                err = self._avahi_proc.stderr.read().decode(errors="replace").strip()
                logger.warning(f"avahi-publish exited immediately: {err}")
                self._avahi_proc = None
                return False
            logger.info(
                f"Bonjour advertisement active via avahi-publish: "
                f"'{self.name}' on port {self.port}"
            )
            return True
        except Exception as e:
            logger.warning(f"avahi-publish failed: {e}")
            self._avahi_proc = None
            return False

    def start(self, **zeroconf_kwargs):
        ip_addr = self.bind_ip or _get_local_ip()
        if not ip_addr:
            logger.warning("Could not determine local IP for Bonjour advertisement")
            return

        logger.info(f"Bonjour: advertising '{self.name}' on {ip_addr}:{self.port}")

        # Prefer avahi-publish on Linux — much more reliable for LAN discovery
        if self._try_avahi_publish():
            return

        # Fall back to Python Zeroconf (macOS, Windows, or avahi missing)
        if zeroconf_kwargs:
            logger.info(f"Bonjour: zeroconf kwargs: {zeroconf_kwargs}")
        for attempt, kw in enumerate([zeroconf_kwargs, {}]):
            try:
                self._info = ServiceInfo(
                    self.SERVICE_TYPE,
                    f"{self.name}.{self.SERVICE_TYPE}",
                    addresses=[_socket.inet_aton(ip_addr)],
                    port=self.port,
                    properties={},
                )
                self._zeroconf = Zeroconf(**kw)
                self._zeroconf.register_service(self._info)
                logger.info(
                    f"Bonjour advertisement active via Zeroconf: '{self.name}' "
                    f"on {ip_addr}:{self.port}"
                    + (f" (attempt {attempt + 1})" if attempt > 0 else "")
                )
                return
            except Exception as e:
                logger.warning(
                    f"Bonjour Zeroconf attempt {attempt + 1} failed: {e}"
                )
                if self._zeroconf:
                    try:
                        self._zeroconf.close()
                    except Exception:
                        pass
                self._zeroconf = None
                self._info = None

        logger.error(
            "Bonjour advertisement FAILED — peers cannot discover this bridge. "
            "Check that avahi-daemon is running: sudo systemctl start avahi-daemon"
        )

    def stop(self):
        if self._avahi_proc:
            self._avahi_proc.terminate()
            try:
                self._avahi_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._avahi_proc.kill()
            self._avahi_proc = None
        if self._zeroconf and self._info:
            self._zeroconf.unregister_service(self._info)
        if self._zeroconf:
            self._zeroconf.close()
        logger.info("Bonjour advertisement stopped")


def _get_local_ip() -> Optional[str]:
    """Best-effort detection of a routable local IPv4 address."""
    # Try a UDP connect trick (works even if 8.8.8.8 is unreachable — no packet sent)
    for dest in ("8.8.8.8", "10.255.255.255", "192.168.255.255"):
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            s.settimeout(0)
            s.connect((dest, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip and ip != "0.0.0.0":
                return ip
        except Exception:
            continue

    # Fallback: enumerate interfaces
    try:
        hostname = _socket.gethostname()
        for info in _socket.getaddrinfo(hostname, None, _socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None


# ── High-level receiver / sender ─────────────────────────────────────

class RTPMIDIReceiver:
    """
    High-level RTP-MIDI node: advertises via Bonjour, accepts incoming
    connections, discovers remote sessions, and handles bidirectional MIDI.

    Usage:
        receiver = RTPMIDIReceiver(
            midi_callback=my_handler,
            session_name="dLive Bridge",
            local_port=5004,
        )
        await receiver.start()
        ...
        receiver.send_midi(b"\\xc0\\x05")   # broadcast PC to all peers
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

        self._session: Optional[AppleMIDISession] = None
        self._browser: Optional[BonjourMIDIBrowser] = None
        self._advertiser: Optional[BonjourMIDIAdvertiser] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._known_peers: set[str] = set()

    def _is_own_session(self, name: str, host: str, port: int) -> bool:
        """Detect whether a discovered service is our own advertisement."""
        # Bonjour names include the service type, e.g. "My Session._apple-midi._udp.local."
        if self.session_name.lower() in name.lower() and port == self.local_port:
            return True
        # Also check by IP: if the host is us and the port matches
        local_ips = set()
        if self.bind_ip:
            local_ips.add(self.bind_ip)
        detected = _get_local_ip()
        if detected:
            local_ips.add(detected)
        local_ips.add("127.0.0.1")
        try:
            local_ips.add(_socket.gethostbyname(_socket.gethostname()))
        except Exception:
            pass
        if host in local_ips and port == self.local_port:
            return True
        return False

    def _on_peer_discovered(self, name: str, host: str, port: int):
        """Called by the Bonjour browser when a remote session appears."""
        if self._is_own_session(name, host, port):
            logger.debug(f"Ignoring own session '{name}' at {host}:{port}")
            return

        key = f"{host}:{port}"
        if key in self._known_peers:
            logger.debug(f"Already tracking peer at {key}")
            return
        self._known_peers.add(key)

        logger.info(f"Connecting to discovered peer '{name}' at {host}:{port}")
        if self._session and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._session.invite_peer(host, port), self._loop
            )

    async def start(self):
        """Start the RTP-MIDI node: bind, advertise, browse, accept."""
        self._loop = asyncio.get_running_loop()

        zc_kwargs = {}
        if self.bind_ip:
            zc_kwargs["interfaces"] = [self.bind_ip]
            logger.info(f"Zeroconf scoped to {self.bind_ip}")

        # 1. Bind our UDP session on local_port / local_port+1
        self._session = AppleMIDISession(
            name=self.session_name,
            midi_callback=self.midi_callback,
            local_port=self.local_port,
        )
        await self._session.start(self._loop, bind_ip=self.bind_ip)

        # 2. Advertise via Bonjour so peers can find and connect to us
        self._advertiser = BonjourMIDIAdvertiser(
            name=self.session_name,
            port=self.local_port,
            bind_ip=self.bind_ip,
        )
        self._advertiser.start(**zc_kwargs)

        # 3. Browse for existing sessions and send them invitations
        self._browser = BonjourMIDIBrowser(
            on_discovered=self._on_peer_discovered,
            filter_name=self.filter_name,
        )
        self._browser.start(**zc_kwargs)

    def send_midi(self, data: bytes):
        """Broadcast MIDI bytes to all connected RTP-MIDI peers."""
        if self._session:
            self._session.send_midi(data)

    async def stop(self):
        """Shut down everything gracefully."""
        if self._advertiser:
            self._advertiser.stop()
        if self._browser:
            self._browser.stop()
        if self._session:
            await self._session.stop()
        self._known_peers.clear()
        logger.info("RTP-MIDI receiver stopped")
