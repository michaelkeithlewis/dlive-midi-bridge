# dLive MIDI Bridge

**RTP-MIDI → Allen & Heath dLive TCP bridge.**

Receives Network MIDI (Apple Network MIDI / Bonjour / RTP-MIDI) and forwards raw MIDI bytes to an Allen & Heath dLive console over TCP. Also bridges MIDI *back* from the dLive into the network session. Supports USB MIDI controllers plugged directly into the host.

Designed to run on a Raspberry Pi velcroed to the back of your dLive.

## Signal Flow

```
Tracks Rig ──(RTP-MIDI/Bonjour)──┐                    ┌──→ Network MIDI peers
                                  ├──→ dLive MixRack ──┤
USB MIDI controller (local) ─────┘    (TCP:51325)      └──→ SuperRack, etc.
```

## Install (one command)

```bash
curl -sSL https://raw.githubusercontent.com/michaelkeithlewis/dlive-midi-bridge/main/install.sh | bash
```

This works on **Mac** and **Linux** (including Raspberry Pi). It clones the repo, installs everything, and launches the interactive setup wizard.

To re-run the wizard later:

```bash
dlive setup
```

## Manual Install

```bash
git clone https://github.com/michaelkeithlewis/dlive-midi-bridge.git
cd dlive-midi-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install .
dlive setup
```

## Commands

Everything is just `dlive <verb>`:

| Command           | What it does                                   |
|-------------------|------------------------------------------------|
| `dlive`           | Run the bridge (auto-finds your config)        |
| `dlive setup`     | Interactive setup wizard                       |
| `dlive scan`      | Find dLive consoles on the network             |
| `dlive test`      | Send test MIDI messages (interactive)          |
| `dlive start`     | Start the background service                   |
| `dlive stop`      | Stop the background service                    |
| `dlive restart`   | Restart the background service                 |
| `dlive status`    | Check if the bridge is running + show config   |

### Advanced (power-user flags)

```bash
dlive run --dlive-ip 192.168.1.80 --log-midi --verbose
dlive run --list-midi-ports
```

The old `dlive-midi-bridge` and `dlive-test-send` commands still work for backwards compatibility.

## Configuration

The setup wizard writes your config to:

- Mac: `~/.config/dlive-midi-bridge/config.yaml`
- Pi/Linux: `~/.config/dlive-midi-bridge/config.yaml` (or `/etc/dlive-midi-bridge/config.yaml` for root)

See `config/config.example.yaml` for all options.

## dLive TCP Protocol

The dLive accepts raw MIDI bytes on TCP with no framing. The console sends `0xFE` (Active Sense) every ~300ms as a keepalive.

| Target   | Port  | TLS Port |
|----------|-------|----------|
| MixRack  | 51325 | 51327    |
| Surface  | 51328 | 51329    |

## Uninstall

```bash
dlive uninstall
```

Stops the service, removes the config, symlinks, install directory, and logs. One command, done.

## Requirements

- Python 3.9+
- Network access to both the RTP-MIDI source and the dLive
- Avahi/Bonjour for mDNS discovery (built into macOS, installed automatically on Pi)

## License

MIT
