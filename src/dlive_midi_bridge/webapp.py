"""
dLive Bridge web control panel — stdlib-only admin page.

A dependency-free (Python standard library) HTTP server that exposes the
bridge's status, configuration, live MIDI log, and network/WiFi setup in a
browser.  Runs as its own systemd service (dlive-bridge-web) so a web crash
never touches the audio path.

Design constraints:
  * NO third-party dependencies — the Pi is offline on the show LAN and its
    venv has no build tools, so we cannot pip-install Flask/FastAPI.  Uses
    http.server + a single self-contained HTML page (inline CSS/JS, no CDN).
  * Talks to the system the same way the CLI does: reads the bridge's status
    file, edits config.yaml in place (preserving comments), and drives
    systemctl / nmcli via subprocess.

Run:  python3 -m dlive_midi_bridge.webapp --bind 0.0.0.0 --port 8080
"""

import argparse
import concurrent.futures
import ipaddress
import json
import logging
import re
import shutil
import subprocess
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# Small offline OUI (MAC prefix → vendor) table. The Pi has no internet on the
# show LAN, so we can't query an OUI service; this covers gear we actually see.
# Keys are the first three MAC octets, lowercase, colon-separated.
OUI_VENDORS = {
    # Audiotonix = Allen & Heath / DiGiCo (the dLive lives here)
    "00:04:c4": "Allen & Heath",
    # Raspberry Pi Foundation / Trading
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi",
    "e4:5f:01": "Raspberry Pi", "d8:3a:dd": "Raspberry Pi",
    "28:cd:c1": "Raspberry Pi", "2c:cf:67": "Raspberry Pi",
    # Apple
    "f0:18:98": "Apple", "a4:83:e7": "Apple", "3c:22:fb": "Apple",
    "ac:bc:32": "Apple", "f0:99:bf": "Apple", "04:0c:ce": "Apple",
    "88:66:5a": "Apple", "14:98:77": "Apple",
    # Networking gear
    "2c:30:33": "Netgear", "2c:b0:5d": "Netgear",
    "18:e8:29": "Ubiquiti", "24:5a:4c": "Ubiquiti", "fc:ec:da": "Ubiquiti",
    "50:c7:bf": "TP-Link", "b0:be:76": "TP-Link", "00:0e:dd": "TP-Link",
    "00:1a:2b": "Cisco", "00:1b:54": "Cisco",
    # Consumer
    "00:17:88": "Philips Hue", "b8:e9:37": "Sonos", "5c:aa:fd": "Sonos",
}

# ── Locations (mirror cli.py / bridge.py) ────────────────────────────
CONFIG_SEARCH_PATHS = [
    Path.home() / ".config" / "dlive-midi-bridge" / "config.yaml",
    Path("/etc/dlive-midi-bridge/config.yaml"),
]
STATUS_FILE = Path("/tmp/dlive-midi-bridge-status.json")
SYSTEMD_UNIT = "dlive-midi-bridge"

# Config fields the settings form is allowed to edit, with their types.
# Anything not listed here is left untouched in config.yaml.
EDITABLE_FIELDS = {
    "dlive_ip": "str",
    "dlive_port": "int",
    "bind_ip": "str",
    "bind_interface": "str",
    "session_name": "str",
    "passive_mode": "bool",
    "auto_data_invite": "bool",
    "log_midi": "bool",
    "local_midi": "bool",
    "snapshot_note_shim": "bool",
    "snapshot_pc_channel": "int",
    "snapshot_pc_program": "int",
    "snapshot_note_hex": "str",
}


# ── Config helpers ───────────────────────────────────────────────────

def find_config():
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return None


def load_config():
    """Load config.yaml. Uses PyYAML if available (it is, as a bridge dep),
    else a minimal fallback parser for flat key: value files."""
    path = find_config()
    if not path:
        return {}, None
    text = path.read_text()
    try:
        import yaml
        return (yaml.safe_load(text) or {}), path
    except Exception:
        cfg = {}
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" in line:
                k, v = line.split(":", 1)
                cfg[k.strip()] = _coerce(v.strip())
        return cfg, path


def _coerce(v):
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        return v.strip().strip('"').strip("'")


def _format_value(field_type, value):
    if field_type == "bool":
        return "true" if value in (True, "true", "True", "on", 1, "1") else "false"
    if field_type == "int":
        return str(int(value))
    return str(value)


def save_config(updates):
    """Update known keys in config.yaml IN PLACE via line replacement so
    comments and layout are preserved (we can't use ruamel offline).
    Returns (ok, message)."""
    path = find_config()
    if not path:
        return False, "config.yaml not found"

    lines = path.read_text().splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*):(\s*)(.*)$", line)
        if not m:
            continue
        key = m.group(2)
        if key in remaining and key in EDITABLE_FIELDS:
            indent, sp1, sp2 = m.group(1), m.group(3), m.group(4)
            # keep any trailing inline comment
            comment = ""
            cm = re.search(r"(\s+#.*)$", m.group(5))
            if cm:
                comment = cm.group(1)
            val = _format_value(EDITABLE_FIELDS[key], remaining.pop(key))
            lines[i] = f"{indent}{key}{sp1}:{sp2}{val}{comment}"

    # append any editable keys that weren't already present
    for key, value in remaining.items():
        if key in EDITABLE_FIELDS:
            lines.append(f"{key}: {_format_value(EDITABLE_FIELDS[key], value)}")

    # atomic write
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    return True, f"Saved {len(updates)} setting(s) to {path}"


# ── System helpers ───────────────────────────────────────────────────

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def service_active():
    rc, out, _ = _run(["systemctl", "is-active", SYSTEMD_UNIT])
    return out.strip() == "active"


def service_control(action):
    if action not in ("start", "stop", "restart"):
        return False, "invalid action"
    rc, out, err = _run(["sudo", "-n", "systemctl", action, SYSTEMD_UNIT], timeout=30)
    return rc == 0, (err or out or f"{action} ok").strip()


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return None


def recent_log(lines=150):
    rc, out, err = _run(
        ["journalctl", "-u", SYSTEMD_UNIT, "-n", str(lines),
         "--no-pager", "-o", "cat"], timeout=15)
    if rc != 0:
        rc, out, err = _run(
            ["sudo", "-n", "journalctl", "-u", SYSTEMD_UNIT, "-n", str(lines),
             "--no-pager", "-o", "cat"], timeout=15)
    return out


def network_info():
    info = {"interfaces": [], "hostname": socket.gethostname()}
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show"])
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and m.group(1) != "lo":
            info["interfaces"].append({"iface": m.group(1), "ip": m.group(2)})
    return info


def wifi_scan():
    if not shutil.which("nmcli"):
        return {"available": False, "networks": []}
    rc, out, _ = _run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                       "dev", "wifi", "list"], timeout=20)
    nets = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if not ssid:
            continue
        # keep the strongest sighting of each SSID
        sig = int(signal) if signal.isdigit() else 0
        if ssid not in nets or sig > nets[ssid]["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig,
                          "security": security or "open",
                          "active": in_use.strip() == "*"}
    return {"available": True,
            "networks": sorted(nets.values(), key=lambda n: -n["signal"])}


def wifi_connect(ssid, password):
    if not shutil.which("nmcli"):
        return False, "nmcli not available"
    cmd = ["sudo", "-n", "nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out, err = _run(cmd, timeout=45)
    return rc == 0, (out or err or "").strip()


def _unesc(s):
    """Un-escape nmcli terse output (it backslash-escapes ':' and '\\')."""
    return s.replace("\\:", ":").replace("\\\\", "\\")


def _active_wifi_name():
    """The NetworkManager connection NAME currently active on wlan0 (or None).
    Uses the connection name — not the SSID — so profiles like the iPhone
    hotspot (name != SSID) match correctly."""
    rc, out, _ = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"])
    for line in out.splitlines():
        parts = line.split(":", 3)  # DEVICE/TYPE/STATE are colon-free; NAME is last
        if len(parts) == 4 and parts[0] == "wlan0" and parts[1] == "wifi" \
                and parts[2] == "connected":
            return _unesc(parts[3]) or None
    return None


def wifi_current():
    """The WiFi network wlan0 is currently on, if any."""
    if not shutil.which("nmcli"):
        return {"connected": False}
    ssid, signal = None, None
    rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"])
    for line in out.splitlines():
        if line.startswith("yes:"):
            rest = line[4:].rsplit(":", 1)          # SIGNAL is the last field
            ssid = _unesc(rest[0])
            signal = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
            break
    ip = None
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show", "wlan0"])
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    if m:
        ip = m.group(1)
    return {"connected": bool(ssid), "ssid": ssid, "signal": signal,
            "ip": ip, "connection": _active_wifi_name()}


def wifi_saved():
    """WiFi profiles NetworkManager remembers, newest-first, active flagged."""
    if not shutil.which("nmcli"):
        return []
    active = _active_wifi_name()
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"])
    saved = []
    for line in out.splitlines():
        r = line.rsplit(":", 1)                     # TYPE is colon-free and last
        if len(r) != 2 or r[1] != "802-11-wireless":
            continue
        name = _unesc(r[0])
        saved.append({"name": name, "active": name == active})
    saved.sort(key=lambda s: (not s["active"], s["name"].lower()))
    return saved


def wifi_up(name):
    """Bring up a saved connection by name (reuses its stored password)."""
    if not shutil.which("nmcli"):
        return False, "nmcli not available"
    rc, out, err = _run(["sudo", "-n", "nmcli", "con", "up", "id", name], timeout=45)
    return rc == 0, (out or err or "").strip()


def wifi_forget(name):
    if not shutil.which("nmcli"):
        return False, "nmcli not available"
    rc, out, err = _run(["sudo", "-n", "nmcli", "con", "delete", "id", name], timeout=20)
    return rc == 0, (out or err or "deleted").strip()


# ── LAN device scan (lanscan-style, stdlib only) ─────────────────────
# Strategy on an offline Pi with no nmap/arp-scan:
#   1. Fan out unprivileged `ping` across the local /24 to populate the
#      kernel's ARP/neighbour table (each host that answers or that we route
#      to gets an lladdr).
#   2. Read `ip neigh` for the resolved MAC addresses — that's ground truth
#      for "who is on this wire", including hosts that don't reply to ping.
#   3. Best-effort reverse-DNS (bounded — see note) and offline OUI lookup.

def _iface_mac(iface):
    try:
        return Path(f"/sys/class/net/{iface}/address").read_text().strip().lower()
    except Exception:
        return ""


# Cap on how many addresses a single scan may sweep, so an over-wide CIDR
# (e.g. a /16 = 65k hosts) can't tie the Pi up for minutes.
MAX_SCAN_HOSTS = 2048


def list_interfaces():
    """All global IPv4 interfaces (not lo), each with its own network CIDR."""
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    ifaces = []
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if m and m.group(1) != "lo":
            ip, prefix = m.group(2), int(m.group(3))
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
            ifaces.append({"iface": m.group(1), "ip": ip, "prefix": prefix,
                           "cidr": str(net)})
    return ifaces


def _primary_iface():
    """First global IPv4 interface (not lo): {iface, ip, prefix} or None."""
    ifaces = list_interfaces()
    return ifaces[0] if ifaces else None


def _vendor(mac):
    return OUI_VENDORS.get(mac[:8].lower(), "") if mac else ""


def _ping_one(ip):
    subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _rdns_batch(ips, budget=1.2):
    """Reverse-DNS lookups in daemon threads. On the show LAN there is no DNS
    server, so a blocking gethostbyaddr hangs ~10s; we start all lookups, wait
    only `budget` seconds, and keep whatever resolved. Stragglers are abandoned
    (daemon threads) and cost nothing to the request."""
    out, lock = {}, threading.Lock()

    def worker(ip):
        try:
            name = socket.gethostbyaddr(ip)[0]
        except Exception:
            return
        with lock:
            out[ip] = name

    threads = [threading.Thread(target=worker, args=(ip,), daemon=True) for ip in ips]
    for t in threads:
        t.start()
    deadline = time.time() + budget
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.time()))
    return out


def scan_lan(iface=None, cidr=None, do_rdns=True):
    ifaces = list_interfaces()
    if not ifaces:
        return {"available": False, "devices": [], "error": "no IPv4 interface"}

    # pick the interface (requested name, else the first global one)
    chosen = next((x for x in ifaces if x["iface"] == iface), None) or ifaces[0]

    # pick the range to sweep: an explicit CIDR, else the interface's own net
    try:
        net = ipaddress.ip_network(cidr, strict=False) if cidr else \
            ipaddress.ip_network(f"{chosen['ip']}/{chosen['prefix']}", strict=False)
    except ValueError as e:
        return {"available": False, "devices": [], "error": f"bad subnet: {e}"}
    if net.version != 4:
        return {"available": False, "devices": [], "error": "IPv4 only"}

    hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
    truncated = len(hosts) > MAX_SCAN_HOSTS
    if truncated:
        hosts = hosts[:MAX_SCAN_HOSTS]

    # 1) populate ARP via a parallel ping sweep (only on-link IPs will get a MAC)
    t0 = time.time()
    workers = min(200, max(16, len(hosts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_ping_one, hosts))

    # 2) read the neighbour table for resolved MACs, keep those inside the range
    rc, out, _ = _run(["ip", "neigh", "show", "dev", chosen["iface"]])
    devices = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        state = parts[-1]
        mac = parts[parts.index("lladdr") + 1].lower() if "lladdr" in parts else ""
        if not mac or state in ("FAILED", "INCOMPLETE"):
            continue
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        devices[ip] = {"ip": ip, "mac": mac, "state": state}

    # include the Pi itself if it falls inside the scanned range (it never
    # appears in its own neigh table)
    if ipaddress.ip_address(chosen["ip"]) in net:
        devices[chosen["ip"]] = {"ip": chosen["ip"],
                                 "mac": _iface_mac(chosen["iface"]),
                                 "state": "self"}

    # 3) reverse DNS (bounded) + offline vendor + friendly notes
    names = _rdns_batch(list(devices), budget=1.2) if do_rdns else {}
    dlive_ip = (load_config()[0] or {}).get("dlive_ip")
    gw = {str(net.network_address + 1)}  # a common gateway address
    for ip, d in devices.items():
        d["host"] = names.get(ip, "")
        d["vendor"] = _vendor(d["mac"])
        d["is_self"] = (ip == chosen["ip"])
        note = ""
        if d["is_self"]:
            note = "This bridge (Raspberry Pi)"
        elif ip == dlive_ip:
            note = "dLive mixer"
        elif ip.endswith(".254") or ip in gw:
            note = "Gateway / router"
        d["note"] = note

    ordered = sorted(devices.values(),
                     key=lambda d: [int(x) for x in d["ip"].split(".")])
    return {
        "available": True,
        "iface": chosen["iface"],
        "subnet": str(net),
        "own_ip": chosen["ip"],
        "hosts_scanned": len(hosts),
        "truncated": truncated,
        "max_hosts": MAX_SCAN_HOSTS,
        "count": len(ordered),
        "scan_ms": int((time.time() - t0) * 1000),
        "devices": ordered,
    }


# ── HTTP handler ─────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "dLiveBridgeWeb"

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- response helpers --
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # -- routing --
    def do_GET(self):
        route = urlparse(self.path)
        p = route.path
        try:
            if p == "/" or p == "/index.html":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if p == "/api/status":
                return self._send(200, {
                    "service_active": service_active(),
                    "status": read_status(),
                    "network": network_info(),
                })
            if p == "/api/config":
                cfg, path = load_config()
                return self._send(200, {
                    "config": cfg,
                    "path": str(path) if path else None,
                    "editable": list(EDITABLE_FIELDS),
                })
            if p == "/api/log":
                qs = parse_qs(route.query)
                n = int(qs.get("lines", ["150"])[0])
                return self._send(200, {"log": recent_log(min(n, 500))})
            if p == "/api/wifi/scan":
                return self._send(200, wifi_scan())
            if p == "/api/wifi/state":
                return self._send(200, {"current": wifi_current(),
                                        "saved": wifi_saved()})
            if p == "/api/devices/interfaces":
                return self._send(200, {"interfaces": list_interfaces()})
            if p == "/api/devices/scan":
                qs = parse_qs(route.query)
                rdns = qs.get("rdns", ["1"])[0] != "0"
                iface = qs.get("iface", [None])[0] or None
                cidr = qs.get("cidr", [None])[0] or None
                return self._send(200, scan_lan(iface=iface, cidr=cidr, do_rdns=rdns))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            logger.exception("GET %s failed", p)
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self._json_body()
            if p == "/api/config":
                updates = {k: v for k, v in body.items() if k in EDITABLE_FIELDS}
                ok, msg = save_config(updates)
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            if p == "/api/service":
                ok, msg = service_control(body.get("action", ""))
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            if p == "/api/wifi/connect":
                ok, msg = wifi_connect(body.get("ssid", ""), body.get("password", ""))
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            if p == "/api/wifi/up":
                ok, msg = wifi_up(body.get("name", ""))
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            if p == "/api/wifi/forget":
                ok, msg = wifi_forget(body.get("name", ""))
                return self._send(200 if ok else 400, {"ok": ok, "message": msg})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            logger.exception("POST %s failed", p)
            return self._send(500, {"error": str(e)})


# ── The single-page UI (inline, no external assets) ──────────────────
PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>dLive Bridge</title>
<style>
:root{--bg:#0f1216;--panel:#181d24;--line:#2a323d;--ink:#e7edf3;--mut:#8a97a6;--acc:#4a9eff;--ok:#3fb950;--bad:#f85149;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--ok)}.dot.off{background:var(--bad)}
nav{display:flex;gap:4px;padding:10px 18px 0;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
nav button{background:none;border:0;color:var(--mut);padding:8px 14px;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px}
nav button.active{color:var(--ink);background:var(--bg);border:1px solid var(--line);border-bottom-color:var(--bg);margin-bottom:-1px}
main{padding:18px;max-width:820px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat .k{color:var(--mut);font-size:12px}.stat .v{font-size:22px;font-weight:600;margin-top:2px}
label{display:block;margin:10px 0 4px;color:var(--mut);font-size:13px}
input[type=text],input[type=number],input[type=password]{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:8px 10px;font-size:14px}
select{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:8px 10px;font-size:14px}
.row{display:flex;align-items:center;gap:10px;margin:8px 0}
.row input[type=checkbox]{width:18px;height:18px}
button.act{background:var(--acc);border:0;color:#fff;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500}
button.ghost{background:none;border:1px solid var(--line);color:var(--ink)}
button.danger{background:var(--bad)}
.hint{color:var(--mut);font-size:12px}
pre#log{background:#0a0d11;border:1px solid var(--line);border-radius:10px;padding:12px;height:360px;overflow:auto;font:12px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.midi{color:var(--acc)}.err{color:var(--bad)}
.toast{position:fixed;bottom:18px;right:18px;background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--ok);padding:10px 14px;border-radius:8px;opacity:0;transition:.25s;max-width:360px}
.toast.show{opacity:1}.toast.bad{border-left-color:var(--bad)}
.wifi{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin:6px 0;background:var(--bg);cursor:pointer}
.wifi:hover{border-color:var(--acc)}.wifi .s{color:var(--mut);font-size:12px}
.pill{font-size:11px;padding:2px 7px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
#devWrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:12px}
td.mac{font-family:ui-monospace,Menlo,monospace;color:var(--mut)}
tr.self td:first-child,tr.note td:first-child{font-weight:600}
td .tag{font-size:11px;padding:1px 7px;border-radius:20px;background:var(--bg);border:1px solid var(--line);color:var(--acc);margin-left:6px}
</style></head><body>
<header><span class="dot" id="svcDot"></span><h1>dLive Bridge</h1><span class="hint" id="ver"></span></header>
<nav>
 <button data-t="dash" class="active">Dashboard</button>
 <button data-t="settings">Settings</button>
 <button data-t="monitor">MIDI Monitor</button>
 <button data-t="devices">Devices</button>
 <button data-t="network">Network</button>
</nav>
<main>
 <section id="dash">
  <div class="card"><div class="grid" id="stats"></div></div>
  <div class="card">
   <b>Service</b>
   <div class="row" style="margin-top:10px">
    <button class="act" onclick="svc('restart')">Restart bridge</button>
    <button class="act ghost" onclick="svc('stop')">Stop</button>
    <button class="act ghost" onclick="svc('start')">Start</button>
   </div>
   <div class="hint" id="peers"></div>
  </div>
 </section>

 <section id="settings" hidden>
  <div class="card">
   <b>Bridge settings</b>
   <div class="hint" id="cfgPath"></div>
   <div id="form"></div>
   <div class="row" style="margin-top:14px">
    <button class="act" onclick="saveCfg()">Save &amp; restart</button>
    <span class="hint">Saving applies on the next restart.</span>
   </div>
  </div>
 </section>

 <section id="monitor" hidden>
  <div class="card">
   <div class="row"><b>Live MIDI log</b>
    <label class="row" style="margin-left:auto"><input type="checkbox" id="follow" checked> follow</label>
    <label class="row"><input type="checkbox" id="midiOnly" checked> MIDI only</label>
   </div>
   <pre id="log">…</pre>
  </div>
 </section>

 <section id="devices" hidden>
  <div class="card">
   <div class="row"><b>Devices on the network</b>
    <span class="hint" id="devMeta" style="margin-left:8px"></span>
   </div>
   <div class="row" style="flex-wrap:wrap;margin-top:8px">
    <select id="devIface" onchange="ifaceChanged()"></select>
    <input type="text" id="devCidr" placeholder="192.168.1.0/24" style="width:180px" title="Subnet to scan in CIDR notation">
    <button class="act ghost" onclick="resetCidr()" title="Reset to this interface's subnet">↺</button>
    <button class="act" id="devBtn" style="margin-left:auto" onclick="scanDevices()">Scan</button>
   </div>
   <div class="hint" style="margin:6px 0 10px">Pings every address in the range and reads the ARP table — no extra tools required. Only devices on the same wire (interface subnet) return a MAC. Ranges are capped at 2048 addresses.</div>
   <div id="devWrap"><table id="devTable"><thead><tr>
     <th>IP</th><th>MAC</th><th>Vendor</th><th>Name</th><th>State</th></tr></thead>
     <tbody id="devBody"><tr><td colspan="5" class="hint">Tap Rescan to discover devices.</td></tr></tbody>
   </table></div>
  </div>
 </section>

 <section id="network" hidden>
  <div class="card"><div class="row"><b>Current connection</b><button class="act ghost" style="margin-left:auto" onclick="loadNet()">Refresh</button></div><div id="net" class="hint" style="margin-top:8px">…</div></div>
  <div class="card">
   <div class="row"><b>Saved networks</b><span class="hint" style="margin-left:auto">tap to connect · ✕ to forget</span></div>
   <div id="savedList" class="hint">…</div>
  </div>
  <div class="card">
   <div class="row"><b>Available WiFi</b><button class="act ghost" style="margin-left:auto" onclick="scanWifi()">Scan</button></div>
   <div id="wifiList" class="hint">Tap Scan to list networks.</div>
   <div id="wifiJoin" hidden style="margin-top:10px">
    <label>Password for <b id="wifiSsid"></b></label>
    <input type="password" id="wifiPw" placeholder="network password">
    <div class="row" style="margin-top:8px">
     <button class="act" onclick="joinWifi()">Join</button>
     <button class="act ghost" onclick="wifiJoin.hidden=true">Cancel</button>
    </div>
   </div>
  </div>
 </section>
</main>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s), api=(u,o)=>fetch(u,o).then(r=>r.json());
let cfg={}, editable=[], logTimer=null;
function toast(m,bad){const t=$('#toast');t.textContent=m;t.className='toast show'+(bad?' bad':'');setTimeout(()=>t.className='toast',2600);}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
 b.classList.add('active');
 ['dash','settings','monitor','devices','network'].forEach(id=>$('#'+id).hidden=(id!==b.dataset.t));
 if(b.dataset.t==='monitor')startLog(); else stopLog();
 if(b.dataset.t==='settings')loadCfg();
 if(b.dataset.t==='devices')initDevices();
 if(b.dataset.t==='network')loadNet();
});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let devIfaces=[];
async function initDevices(){
 if(window._devLoaded)return; window._devLoaded=1;
 const d=await api('/api/devices/interfaces');devIfaces=d.interfaces||[];
 const sel=$('#devIface');
 sel.innerHTML=devIfaces.map(i=>`<option value="${esc(i.iface)}">${esc(i.iface)} — ${esc(i.ip)}/${i.prefix}</option>`).join('')||'<option>(none)</option>';
 resetCidr();
 if(devIfaces.length)scanDevices();
}
function curIface(){return devIfaces.find(i=>i.iface===$('#devIface').value);}
function resetCidr(){const i=curIface();if(i)$('#devCidr').value=i.cidr;}
function ifaceChanged(){resetCidr();}
async function scanDevices(){
 const btn=$('#devBtn');btn.disabled=true;const t=btn.textContent;btn.textContent='Scanning…';
 $('#devMeta').textContent='';
 const iface=$('#devIface').value||'', cidr=$('#devCidr').value.trim();
 $('#devBody').innerHTML='<tr><td colspan="5" class="hint">Pinging '+esc(cidr||'subnet')+'…</td></tr>';
 try{
  const q='/api/devices/scan?iface='+encodeURIComponent(iface)+(cidr?'&cidr='+encodeURIComponent(cidr):'');
  const d=await api(q);
  if(!d.available){$('#devBody').innerHTML='<tr><td colspan="5" class="err">'+esc(d.error||'scan failed')+'</td></tr>';return;}
  $('#devMeta').innerHTML=d.count+' device'+(d.count===1?'':'s')+' · '+esc(d.subnet)+' · '+d.hosts_scanned+' scanned · '+d.scan_ms+' ms'+(d.truncated?` <span class="err">(capped at ${d.max_hosts})</span>`:'');
  $('#devBody').innerHTML=(d.devices||[]).map(x=>{
   const cls=x.is_self?'self':(x.note?'note':'');
   const tag=x.note?`<span class="tag">${esc(x.note)}</span>`:'';
   const st=x.state==='self'?'this device':esc(x.state.toLowerCase());
   return `<tr class="${cls}"><td>${esc(x.ip)}${tag}</td><td class="mac">${esc(x.mac)}</td>`+
          `<td>${esc(x.vendor||'')}</td><td>${esc(x.host||'')}</td><td class="hint">${st}</td></tr>`;
  }).join('')||'<tr><td colspan="5" class="hint">No devices found.</td></tr>';
 }catch(e){$('#devBody').innerHTML='<tr><td colspan="5" class="err">'+esc(e)+'</td></tr>';}
 finally{btn.disabled=false;btn.textContent=t;}
}
async function refresh(){
 const d=await api('/api/status');
 $('#svcDot').className='dot '+(d.service_active?'on':'off');
 const s=d.status||{}, c=(s.counters||{}), r=(s.rtp_midi||{}), dl=(s.dlive||{});
 $('#ver').textContent=s.version?('v'+s.version):'';
 $('#stats').innerHTML=[
  ['dLive',dl.connected?'CONNECTED':'—',dl.connected?'ok':'bad'],
  ['RTP peers',r.connected_peers??0],
  ['MIDI → dLive',c.midi_to_dlive??0],
  ['dLive → net',c.dlive_to_network??0],
 ].map(([k,v,cls])=>`<div class="stat"><div class="k">${k}</div><div class="v ${cls==='ok'?'':''}" style="color:${cls==='bad'?'var(--bad)':cls==='ok'?'var(--ok)':'var(--ink)'}">${v}</div></div>`).join('');
 const peers=(r.peers||[]);
 $('#peers').innerHTML=peers.length?('Peers: '+peers.map(p=>`${p.host} ${p.can_send?'✓send':'·'} rx=${p.rx_count}`).join(' · ')):'No RTP peers connected.';
}
async function svc(a){toast('…');const r=await api('/api/service',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});toast(r.message||(r.ok?'ok':'failed'),!r.ok);setTimeout(refresh,1200);}
async function loadCfg(){
 const d=await api('/api/config');cfg=d.config||{};editable=d.editable||[];
 $('#cfgPath').textContent=d.path||'';
 const bools=['passive_mode','auto_data_invite','log_midi','local_midi','snapshot_note_shim'];
 const labels={dlive_ip:'dLive IP',dlive_port:'dLive port',bind_ip:'Bind IP',bind_interface:'Bind interface',session_name:'Session name',passive_mode:'Passive mode',auto_data_invite:'Auto data-invite (reverse MIDI)',log_midi:'Log MIDI',local_midi:'Local USB MIDI',snapshot_note_shim:'Snapshot PC→Note shim',snapshot_pc_channel:'Shim PC channel',snapshot_pc_program:'Shim PC program',snapshot_note_hex:'Shim note (hex)'};
 $('#form').innerHTML=editable.map(k=>{
  const v=cfg[k], L=labels[k]||k;
  if(bools.includes(k))return `<div class="row"><input type="checkbox" id="f_${k}" ${v?'checked':''}><label style="margin:0" for="f_${k}">${L}</label></div>`;
  const t=(typeof v==='number')?'number':'text';
  return `<label>${L}</label><input type="${t}" id="f_${k}" value="${v??''}">`;
 }).join('');
}
async function saveCfg(){
 const bools=['passive_mode','auto_data_invite','log_midi','local_midi','snapshot_note_shim'];
 const out={};editable.forEach(k=>{const el=$('#f_'+k);if(!el)return;out[k]=bools.includes(k)?el.checked:(el.type==='number'?Number(el.value):el.value);});
 const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});
 toast(r.message||'saved',!r.ok);
 if(r.ok)await svc('restart');
}
function startLog(){stopLog();const pull=async()=>{const d=await api('/api/log?lines=200');let ls=(d.log||'').split('\n');if($('#midiOnly').checked)ls=ls.filter(l=>/MIDI|dLive →|Registered peer|RTP-MIDI|Peer|Sync/.test(l));$('#log').innerHTML=ls.map(l=>{let c='';if(/dLive →|RTP-MIDI|Registered peer/.test(l))c='midi';if(/WARN|ERROR|FAIL|LOST/.test(l))c='err';return `<span class="${c}">${l.replace(/</g,'&lt;')}</span>`;}).join('\n');if($('#follow').checked)$('#log').scrollTop=$('#log').scrollHeight;};pull();logTimer=setInterval(pull,1500);}
function stopLog(){if(logTimer){clearInterval(logTimer);logTimer=null;}}
async function loadNet(){
 const [s,w]=await Promise.all([api('/api/status'),api('/api/wifi/state')]);
 const n=s.network||{}, c=(w.current||{});
 const wifiLine=c.connected
   ?`WiFi: <b>${c.ssid}</b>${c.signal!=null?` <span class=pill>${c.signal}%</span>`:''} — ${c.ip||'—'}`
   :'WiFi: <span style="color:var(--warn)">not connected</span>';
 $('#net').innerHTML='Hostname: <b>'+(n.hostname||'?')+'</b><br>'+wifiLine+'<br>'+
   (n.interfaces||[]).map(i=>`${i.iface}: <b>${i.ip}</b>`).join('<br>');
 renderSaved(w.saved||[]);
}
function renderSaved(saved){
 if(!saved.length){$('#savedList').textContent='No saved networks yet.';return;}
 $('#savedList').innerHTML=saved.map(s=>{const nm=s.name.replace(/'/g,"\\'");
  return `<div class="wifi"><span style="flex:1;cursor:pointer" onclick="upSaved('${nm}')">${s.name} ${s.active?'<span class=pill>connected</span>':''}</span><span class="s" title="forget" style="cursor:pointer;padding:0 6px" onclick="forgetSaved('${nm}')">✕</span></div>`;
 }).join('');
}
async function upSaved(name){toast('Connecting to '+name+'…');const r=await api('/api/wifi/up',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});toast(r.message||(r.ok?'connected':'failed'),!r.ok);setTimeout(loadNet,1400);}
async function forgetSaved(name){if(!confirm('Forget "'+name+'"?'))return;const r=await api('/api/wifi/forget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});toast(r.message||'forgotten',!r.ok);setTimeout(loadNet,500);}
async function scanWifi(){$('#wifiList').textContent='Scanning…';const d=await api('/api/wifi/scan');if(!d.available){$('#wifiList').textContent='nmcli not available on this device.';return;}$('#wifiList').innerHTML=(d.networks||[]).map(w=>`<div class="wifi" onclick="pickWifi('${w.ssid.replace(/'/g,"\\'")}')"><span>${w.ssid} ${w.active?'<span class=pill>connected</span>':''}</span><span class="s">${w.signal}% · ${w.security}</span></div>`).join('')||'No networks found.';}
function pickWifi(ssid){$('#wifiSsid').textContent=ssid;$('#wifiJoin').hidden=false;$('#wifiPw').value='';$('#wifiPw').focus();$('#wifiJoin').dataset.ssid=ssid;}
async function joinWifi(){const ssid=$('#wifiJoin').dataset.ssid,pw=$('#wifiPw').value;toast('Joining '+ssid+'…');const r=await api('/api/wifi/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password:pw})});toast(r.message||(r.ok?'joined':'failed'),!r.ok);if(r.ok){$('#wifiJoin').hidden=true;loadNet();}}
refresh();setInterval(()=>{if(!$('#dash').hidden)refresh();},4000);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="dLive Bridge web control panel")
    ap.add_argument("--bind", default="0.0.0.0", help="address to bind (default all)")
    ap.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    logger.info("dLive Bridge web panel on http://%s:%d", args.bind, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
