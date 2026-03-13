# dLive MIDI Bridge

**RTP-MIDI → Allen & Heath dLive TCP bridge.**

Receives Network MIDI (Apple Network MIDI / Bonjour / RTP-MIDI) and forwards raw MIDI bytes to an Allen & Heath dLive console over TCP. Eliminates the need for a Mac Mini running MIDI Pipe + A&H MIDI Control app in your FOH rack.

Designed to run on a Raspberry Pi velcroed to the back of your dLive.

## Signal Flow

```
Tracks Rig ──(RTP-MIDI/Bonjour)──→ [this bridge] ──(TCP:51325)──→ dLive MixRack
```

## Quick Start

### On Mac/Linux (testing)

```bash
# Install
pip install .

# Run (replace with your dLive MixRack IP)
dlive-midi-bridge --dlive-ip 192.168.1.80 --log-midi --verbose
```

### On Raspberry Pi (production)

```bash
# Clone or copy the project to the Pi, then:
sudo ./install-pi.sh

# Edit config with your dLive IP
sudo nano /etc/dlive-midi-bridge/config.yaml

# Start
sudo systemctl start dlive-midi-bridge

# Check logs
journalctl -u dlive-midi-bridge -f
```

## CLI Options

```
dlive-midi-bridge --dlive-ip 192.168.1.80          # basic
dlive-midi-bridge --dlive-ip 192.168.1.80 \
                  --target surface \                 # connect to Surface instead of MixRack
                  --filter "Tracks" \                # only accept peers named "Tracks"
                  --midi-channel 1 \                 # filter to channel 1
                  --log-midi                         # log every MIDI message
dlive-midi-bridge --config /path/to/config.yaml     # use config file
```

## Configuration

See `config/config.example.yaml` for all options. Copy it to:
- Pi: `/etc/dlive-midi-bridge/config.yaml`
- Mac: `~/.config/dlive-midi-bridge/config.yaml`

## dLive TCP Protocol

The dLive accepts raw MIDI bytes on TCP port 51325 (MixRack, unencrypted). No framing, no length prefix — just MIDI bytes on the wire. The console sends `0xFE` (Active Sense) every ~300ms as a keepalive.

| Target   | Port  | TLS Port |
|----------|-------|----------|
| MixRack  | 51325 | 51327    |
| Surface  | 51328 | 51329    |

## Requirements

- Python 3.9+
- Network access to both the RTP-MIDI source and the dLive
- Avahi/Bonjour for mDNS discovery (installed automatically on Pi)

## License

MIT
