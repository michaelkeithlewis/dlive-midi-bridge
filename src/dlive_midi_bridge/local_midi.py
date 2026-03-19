"""
Local MIDI input via USB/hardware MIDI interfaces.

Uses python-rtmidi to read from local MIDI ports (USB controllers, hardware
interfaces, virtual ports) and forward to a callback. Supports hot-plugging
by periodically scanning for new devices.

Architecture:
  - Scans for available MIDI input ports on startup
  - Opens matching ports (all, or filtered by name)
  - Polls for new devices every few seconds (USB hot-plug)
  - Each received message is forwarded via the midi_callback
"""

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_rtmidi_available: Optional[bool] = None
_rtmidi_module = None


def _suppress_stderr():
    """Redirect stderr at the file-descriptor level to silence C library messages."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        os.dup2(devnull, 2)
        os.close(devnull)
        return saved
    except Exception:
        return None


def _restore_stderr(saved):
    if saved is not None:
        try:
            os.dup2(saved, 2)
            os.close(saved)
        except Exception:
            pass


def _ensure_alsa_sequencer():
    """On Linux, try to load the snd-seq kernel module if /dev/snd/seq is missing."""
    import platform
    if platform.system() != "Linux":
        return

    if os.path.exists("/dev/snd/seq"):
        return

    logger.info("ALSA sequencer not available — loading snd-seq module...")
    try:
        subprocess.run(
            ["sudo", "modprobe", "snd-seq"],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        logger.debug(f"Could not load snd-seq: {e}")


def _get_rtmidi():
    """
    Import rtmidi and verify it can actually create a MIDI client.
    Caches the result so ALSA errors only appear once (if at all).
    Returns the rtmidi module, or None if MIDI isn't available.
    """
    global _rtmidi_available, _rtmidi_module

    if _rtmidi_available is not None:
        return _rtmidi_module

    _ensure_alsa_sequencer()

    try:
        import rtmidi
    except ImportError as e:
        logger.warning(f"python-rtmidi not installed: {e}")
        _rtmidi_available = False
        return None

    # Actually try to create a client — suppress the C-level ALSA error on stderr
    saved = _suppress_stderr()
    try:
        probe = rtmidi.MidiIn()
        probe.close_port()
        del probe
        _rtmidi_module = rtmidi
        _rtmidi_available = True
        logger.debug("MIDI subsystem available")
    except Exception as e:
        _rtmidi_available = False
        _rtmidi_module = None
        _restore_stderr(saved)
        logger.info(f"MIDI subsystem not available (no sound hardware?): {e}")
        return None
    finally:
        _restore_stderr(saved)

    return _rtmidi_module

HOTPLUG_POLL_INTERVAL = 3.0


class LocalMIDIInput:
    """
    Reads MIDI from local hardware/USB/virtual ports.

    Args:
        midi_callback: Called with raw MIDI bytes for each message received.
        port_name_filter: Only open ports whose name contains this string.
                          None = open all available ports.
        log_midi: Log every message at INFO level.
    """

    def __init__(
        self,
        midi_callback: Callable[[bytes], None],
        port_name_filter: Optional[str] = None,
        log_midi: bool = False,
    ):
        self.midi_callback = midi_callback
        self.port_name_filter = port_name_filter
        self.log_midi = log_midi

        self._open_ports: dict[str, rtmidi.MidiIn] = {}
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

    def _matches_filter(self, port_name: str) -> bool:
        if self.port_name_filter is None:
            return True
        return self.port_name_filter.lower() in port_name.lower()

    def _make_callback(self, port_name: str):
        """Create a per-port callback that forwards MIDI bytes."""
        def callback(event, _data=None):
            message, _delta = event
            if not message:
                return

            midi_bytes = bytes(message)

            if self.log_midi:
                logger.info(f"Local MIDI [{port_name}]: {midi_bytes.hex(' ')}")

            self.midi_callback(midi_bytes)

        return callback

    def _scan_and_open(self):
        """Scan for MIDI input ports and open any new ones."""
        rtmidi = _get_rtmidi()
        if rtmidi is None:
            return
        try:
            probe = rtmidi.MidiIn()
            available = probe.get_ports()
            probe.close_port()
            del probe
        except Exception as e:
            logger.debug(f"MIDI port scan skipped (ALSA unavailable): {e}")
            return

        for i, name in enumerate(available):
            if name in self._open_ports:
                continue

            if not self._matches_filter(name):
                logger.debug(f"Skipping MIDI port: '{name}' (filter: {self.port_name_filter})")
                continue

            try:
                midi_in = rtmidi.MidiIn()  # rtmidi already imported via _get_rtmidi above
                midi_in.open_port(i)
                midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
                midi_in.set_callback(self._make_callback(name))
                self._open_ports[name] = midi_in
                logger.info(f"Opened local MIDI port: '{name}'")
            except Exception as e:
                logger.warning(f"Failed to open MIDI port '{name}': {e}")

        # Clean up ports that disappeared
        current_names = set(available)
        gone = [name for name in self._open_ports if name not in current_names]
        for name in gone:
            logger.info(f"Local MIDI port disconnected: '{name}'")
            try:
                self._open_ports[name].close_port()
            except Exception:
                pass
            del self._open_ports[name]

    def _poll_loop(self):
        """Background thread: periodically scan for new/removed MIDI devices."""
        while self._running:
            try:
                self._scan_and_open()
            except Exception as e:
                logger.warning(f"MIDI port scan error: {e}")
            time.sleep(HOTPLUG_POLL_INTERVAL)

    def start(self):
        """Start listening on local MIDI ports."""
        self._running = True

        rtmidi = _get_rtmidi()
        if rtmidi is None:
            logger.warning("Local MIDI disabled — python-rtmidi/ALSA not available")
            return

        try:
            probe = rtmidi.MidiIn()
            available = probe.get_ports()
            probe.close_port()
            del probe
        except Exception as e:
            logger.warning(f"Local MIDI disabled — ALSA error: {e}")
            return

        if available:
            logger.info(f"Found {len(available)} local MIDI port(s): {available}")
        else:
            logger.info("No local MIDI ports found yet (will keep scanning)")

        self._scan_and_open()

        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="midi-hotplug"
        )
        self._poll_thread.start()

    def stop(self):
        """Stop listening and close all ports."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)

        for name, midi_in in self._open_ports.items():
            try:
                midi_in.close_port()
                logger.debug(f"Closed MIDI port: '{name}'")
            except Exception:
                pass
        self._open_ports.clear()
        logger.info("Local MIDI input stopped")

    def list_ports(self) -> list[str]:
        """Return a list of currently available MIDI input port names."""
        rtmidi = _get_rtmidi()
        if rtmidi is None:
            return []
        try:
            probe = rtmidi.MidiIn()
            ports = probe.get_ports()
            probe.close_port()
            del probe
            return ports
        except Exception:
            return []
