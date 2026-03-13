# dLive MIDI Bridge

**RTP-MIDI → Allen & Heath dLive TCP bridge.**

Receives Network MIDI (Apple Network MIDI / Bonjour / RTP-MIDI) and forwards raw MIDI bytes to an Allen & Heath dLive console over TCP. Also supports USB MIDI controllers plugged directly into the host. Eliminates the need for a Mac Mini running MIDI Pipe + A&H MIDI Control app in your FOH rack.

Designed to run on a Raspberry Pi velcroed to the back of your dLive.

## Signal Flow

```
Tracks Rig ──(RTP-MIDI/Bonjour)──┐
                                  ├──→ [this bridge] ──(TCP:51325)──→ dLive MixRack
USB MIDI controller (local) ─────┘
```

## Install (one command)

```bash
curl -sSL https://raw.githubusercontent.com/michaelkeithlewis/dlive-midi-bridge/main/install.sh | bash
```

This works on **Mac** and **Linux** (including Raspberry Pi). It clones the repo, installs everything into a virtual environment, and launches the interactive setup wizard.

The wizard walks you through:
1. dLive IP address (with a connection test)
2. MixRack or Surface
3. RTP-MIDI session settings
4. Local MIDI (scans for USB controllers)
5. MIDI channel filter and logging
6. Writing your config file
7. Installing as a system service (launchd on Mac, systemd on Linux)

To re-run the wizard later:

```bash
dlive-midi-bridge setup
```

## Manual Install

If you prefer to do it yourself:

```bash
git clone https://github.com/michaelkeithlewis/dlive-midi-bridge.git
cd dlive-midi-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install .
dlive-midi-bridge setup
```

## Usage

```bash
# Run the bridge
dlive-midi-bridge run --dlive-ip 192.168.1.80
dlive-midi-bridge run --config ~/.config/dlive-midi-bridge/config.yaml

# Run with all the options
dlive-midi-bridge run --dlive-ip 192.168.1.80 \
    --target surface \
    --filter "Tracks" \
    --midi-channel 1 \
    --local-midi \
    --log-midi --verbose

# List USB MIDI devices
dlive-midi-bridge run --list-midi-ports

# Test without RTP-MIDI (send messages directly to dLive)
dlive-test-send --dlive-ip 192.168.1.80 --program 5
dlive-test-send --dlive-ip 192.168.1.80 --sweep
```

## Configuration

See `config/config.example.yaml` for all options. The setup wizard writes this for you, or copy it manually:

- Mac: `~/.config/dlive-midi-bridge/config.yaml`
- Pi/Linux (root): `/etc/dlive-midi-bridge/config.yaml`
- Pi/Linux (user): `~/.config/dlive-midi-bridge/config.yaml`

## dLive TCP Protocol

The dLive accepts raw MIDI bytes on TCP with no framing. The console sends `0xFE` (Active Sense) every ~300ms as a keepalive.

| Target   | Port  | TLS Port |
|----------|-------|----------|
| MixRack  | 51325 | 51327    |
| Surface  | 51328 | 51329    |

## Uninstall

```bash
pip uninstall dlive-midi-bridge
rm -rf ~/.local/share/dlive-midi-bridge
```

To remove the system service:
- Mac: `launchctl unload ~/Library/LaunchAgents/com.backlinelogic.dlive-midi-bridge.plist`
- Linux: `sudo systemctl disable --now dlive-midi-bridge`

## Requirements

- Python 3.9+
- Network access to both the RTP-MIDI source and the dLive
- Avahi/Bonjour for mDNS discovery (built into macOS, installed automatically on Pi)

## License

MIT
