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
import json
import logging
import re
import shutil
import subprocess
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

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
</style></head><body>
<header><span class="dot" id="svcDot"></span><h1>dLive Bridge</h1><span class="hint" id="ver"></span></header>
<nav>
 <button data-t="dash" class="active">Dashboard</button>
 <button data-t="settings">Settings</button>
 <button data-t="monitor">MIDI Monitor</button>
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

 <section id="network" hidden>
  <div class="card"><b>This device</b><div id="net" class="hint"></div></div>
  <div class="card">
   <div class="row"><b>WiFi</b><button class="act ghost" style="margin-left:auto" onclick="scanWifi()">Scan</button></div>
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
 ['dash','settings','monitor','network'].forEach(id=>$('#'+id).hidden=(id!==b.dataset.t));
 if(b.dataset.t==='monitor')startLog(); else stopLog();
 if(b.dataset.t==='settings')loadCfg();
 if(b.dataset.t==='network')loadNet();
});
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
async function loadNet(){const d=await api('/api/status');const n=d.network||{};$('#net').innerHTML='Hostname: <b>'+(n.hostname||'?')+'</b><br>'+(n.interfaces||[]).map(i=>`${i.iface}: <b>${i.ip}</b>`).join('<br>');}
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
