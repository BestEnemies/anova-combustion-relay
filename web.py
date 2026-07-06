"""Tiny asyncio web UI for the Anova Oven to Combustion Relay (MicroPython).

Serves a single-page config/control panel on port 80 once WiFi is up:
  GET  /         -> HTML page
  GET  /status   -> JSON live state (polled by the page)
  POST /control  -> runtime changes (follow, manual temp, flags, alarms)
  POST /config   -> persist config.json (wifi/anova/serial); optional reboot

All handlers run on the main asyncio loop, so they must stay quick.
"""

import asyncio
import json
import time
import binascii

import machine

import network

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _sha1(data):
    try:
        import hashlib
        return hashlib.sha1(data).digest()
    except Exception:
        pass
    # Pure-python SHA-1 fallback (some builds lack hashlib.sha1).
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    ml = len(data) * 8
    data = data + b"\x80"
    while len(data) % 64 != 56:
        data += b"\x00"
    data += ml.to_bytes(8, "big")

    def rol(n, b):
        return ((n << b) | (n >> (32 - b))) & 0xFFFFFFFF

    for i in range(0, len(data), 64):
        w = [int.from_bytes(data[i + j * 4:i + j * 4 + 4], "big")
             for j in range(16)]
        for j in range(16, 80):
            w.append(rol(w[j - 3] ^ w[j - 8] ^ w[j - 14] ^ w[j - 16], 1))
        a, b, c, d, e = h
        for j in range(80):
            if j < 20:
                f = (b & c) | ((~b) & d); k = 0x5A827999
            elif j < 40:
                f = b ^ c ^ d; k = 0x6ED9EBA1
            elif j < 60:
                f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d; k = 0xCA62C1D6
            t = (rol(a, 5) + f + e + k + w[j]) & 0xFFFFFFFF
            e = d; d = c; c = rol(b, 30); b = a; a = t
        h = [(h[0] + a) & 0xFFFFFFFF, (h[1] + b) & 0xFFFFFFFF,
             (h[2] + c) & 0xFFFFFFFF, (h[3] + d) & 0xFFFFFFFF,
             (h[4] + e) & 0xFFFFFFFF]
    return b"".join(x.to_bytes(4, "big") for x in h)


def _ws_frame(text):
    """Build an unmasked server text frame."""
    payload = text.encode()
    n = len(payload)
    if n < 126:
        header = bytes((0x81, n))
    elif n < 65536:
        header = bytes((0x81, 126, (n >> 8) & 0xFF, n & 0xFF))
    else:
        header = bytes((0x81, 127)) + n.to_bytes(8, "big")
    return header + payload


def _url_decode(s):
    s = s.replace("+", " ")
    out = ""
    i = 0
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s):
            try:
                out += chr(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out += c
        i += 1
    return out


def _parse_form(body):
    form = {}
    for pair in body.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        form[_url_decode(k)] = _url_decode(v)
    return form


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anova Oven to Combustion Relay</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.4 system-ui,sans-serif;background:#14161a;color:#e8eaed}
.wrap{max-width:560px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:.2em 0}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:#9aa0a6;margin:1.4em 0 .5em;border-bottom:1px solid #2a2e35;padding-bottom:4px}
.card{background:#1c1f25;border:1px solid #2a2e35;border-radius:10px;padding:14px;margin-bottom:14px}
.big{font-size:34px;font-weight:600;margin:.1em 0}
.grid{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:14px}
.grid b{color:#9aa0a6;font-weight:400}
label{display:block;margin:.6em 0 .2em;color:#c8ccd2;font-size:13px}
input[type=text],input[type=password],input[type=number]{width:100%;padding:9px;border-radius:7px;border:1px solid #333a44;background:#0f1114;color:#e8eaed;font-size:15px}
.row{display:flex;gap:10px;align-items:center}
.row input[type=number]{width:auto;flex:0 0 110px}
.chk{display:flex;align-items:center;gap:8px;margin:.5em 0}
button{background:#2b6cff;color:#fff;border:0;border-radius:8px;padding:11px 16px;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px}
button.sec{background:#333a44}
button.warn{background:#a33}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
.on{background:#183d1e;color:#5fd873}.off{background:#3d1818;color:#e06666}
small{color:#9aa0a6}
.ut{color:#9aa0a6;text-decoration:none;padding:2px 9px;border-radius:6px;border:1px solid #333a44;font-size:13px}
.ut.active{background:#2b6cff;color:#fff;border-color:#2b6cff}
</style></head><body><div class="wrap">
<h1>Anova Oven &rarr; Combustion Relay</h1>
<small>http://__MDNS__.local/</small>
<div class="card">
  <div>Gauge temperature
    <span style="float:right">
      <a href="#" id="uc" class="ut active" onclick="setUnit('c');return false">&deg;C</a>
      <a href="#" id="uf" class="ut" onclick="setUnit('f');return false">&deg;F</a>
    </span></div>
  <div class="big"><span id="gtemp">--</span> <span id="gunit">&deg;C</span> <small id="gtempf"></small></div>
  <div class="grid" style="margin-top:8px">
    <b>Oven</b><span id="oven">--</span>
    <b>Bulb mode</b><span id="mode">--</span>
    <b>Bridge</b><span id="bstatus">--</span>
    <b>BLE clients</b><span id="ble">--</span>
    <b>Broadcasting</b><span id="bcast">--</span>
    <b>Oven on</b><span id="ovon">--</span>
    <b>Relayed probes</b><span id="relayed">--</span>
    <b>Probe link (#2)</b><span id="plink">--</span>
    <b>Serial</b><span id="serial">--</span>
    <b>IP</b><span id="ip">--</span>
    <b>Free heap</b><span id="heap">--</span>
    <b>Uptime</b><span id="uptime">--</span>
  </div>
</div>

<form method="POST" action="/control">
<h2>Control</h2>
<div class="card">
  <div class="chk"><input type="checkbox" id="follow" name="follow" __FOLLOW__>
    <label for="follow" style="margin:0">Follow oven temperature</label></div>
  <label>Manual temperature <span class="u">&deg;C</span> <small>(used when not following)</small></label>
  <div class="row"><input type="number" step="0.1" name="temp" value="__TEMP__">
    <button type="submit" name="do" value="temp" class="sec">Set</button></div>
  <div class="chk"><input type="checkbox" id="sensor" name="sensor" __SENSOR__>
    <label for="sensor" style="margin:0">Sensor present</label></div>
  <div class="chk"><input type="checkbox" id="batt" name="batt" __BATT__>
    <label for="batt" style="margin:0">Low battery</label></div>
  <div class="chk"><input type="checkbox" id="over" name="over" __OVER__>
    <label for="over" style="margin:0">Sensor overheating</label></div>
  <label>High alarm <span class="u">&deg;C</span> <small>(blank = off)</small></label>
  <input type="number" step="0.1" name="high" value="__HIGH__">
  <label>Low alarm <span class="u">&deg;C</span> <small>(blank = off)</small></label>
  <input type="number" step="0.1" name="low" value="__LOW__">
  <label>BLE broadcast</label>
  <div class="chk"><input type="radio" name="bmode" value="always" id="bA" __BMA__>
    <label for="bA" style="margin:0">Always broadcast</label></div>
  <div class="chk"><input type="radio" name="bmode" value="oven" id="bO" __BMO__>
    <label for="bO" style="margin:0">Only while oven is on (+ grace)</label></div>
  <label>Grace period after oven off <small>(minutes)</small></label>
  <input type="number" name="grace" value="__GRACE__">
  <div class="chk"><input type="checkbox" id="relay" name="relay" __RELAY__>
    <label for="relay" style="margin:0">MeatNet relay: repeat nearby probe advertisements</label></div>
  <div class="chk"><input type="checkbox" id="connect" name="connect" __CONNECT__>
    <label for="connect" style="margin:0">MeatNet connect proxy: connect to a probe (experimental)</label></div>
  <button type="submit" name="do" value="all">Apply control</button>
</div></form>

<form method="POST" action="/config">
<h2>Configuration <small>(saved to device; reboot to apply)</small></h2>
<div class="card">
  <label>WiFi SSID</label><input type="text" name="wifi_ssid" value="__SSID__">
  <label>WiFi password</label><input type="password" name="wifi_password" value="__PASS__">
  <label>mDNS hostname <small>(reachable as name.local)</small></label>
  <input type="text" name="mdns_hostname" value="__MDNS__">
  <label>Gauge serial <small>(exactly 10 chars)</small></label>
  <input type="text" name="serial" maxlength="10" value="__GSERIAL__">
  <label>Anova Personal Access Token</label>
  <input type="text" name="anova_pat" value="__APAT__">
  <div class="chk"><input type="checkbox" id="fdef" name="follow_oven" __FDEF__>
    <label for="fdef" style="margin:0">Follow oven on boot</label></div>
  <button type="submit" name="do" value="save" class="sec">Save</button>
  <button type="submit" name="do" value="reboot" class="warn">Save &amp; reboot</button>
</div></form>
<h2>Live log <small>(<span id="conn">connecting</span>)</small></h2>
<div class="card"><pre id="log" style="max-height:240px;overflow:auto;margin:0;font-size:12px;line-height:1.35;white-space:pre-wrap">&nbsp;</pre></div>
<script>
var unit=localStorage.getItem('unit')||'c';
var lastGauge=null;
var boxC={};   // canonical Celsius values for the temp/high/low inputs
function r1(v){return Math.round(v*10)/10;}
function renderBig(){
  if(lastGauge==null)return;
  var v=unit=='f'?lastGauge*9/5+32:lastGauge;
  var o=unit=='f'?lastGauge:lastGauge*9/5+32;
  gtemp.textContent=v.toFixed(1);
  gunit.innerHTML=unit=='f'?'&deg;F':'&deg;C';
  gtempf.textContent='('+o.toFixed(1)+(unit=='f'?' C':' F')+')';
}
function renderBoxes(){
  ['temp','high','low'].forEach(function(n){
    var el=document.querySelector('[name="'+n+'"]');
    if(!el)return;
    el.value=(boxC[n]==null)?'':r1(unit=='f'?boxC[n]*9/5+32:boxC[n]);
  });
}
function setUnit(u){
  unit=u;localStorage.setItem('unit',u);
  uc.className='ut'+(u=='c'?' active':'');
  uf.className='ut'+(u=='f'?' active':'');
  var us=document.querySelectorAll('.u');
  for(var i=0;i<us.length;i++)us[i].innerHTML=(u=='f'?'&deg;F':'&deg;C');
  gunit.innerHTML=(u=='f'?'&deg;F':'&deg;C');
  renderBig();renderBoxes();
}
function initBoxes(){
  ['temp','high','low'].forEach(function(n){
    var el=document.querySelector('[name="'+n+'"]');
    if(!el)return;
    boxC[n]=el.value===''?null:parseFloat(el.value); // server values are Celsius
    el.addEventListener('input',function(){
      boxC[n]=el.value===''?null:(unit=='f'?(parseFloat(el.value)-32)*5/9:parseFloat(el.value));
    });
  });
  var f=document.querySelector('form[action="/control"]');
  if(f)f.addEventListener('submit',function(){
    ['temp','high','low'].forEach(function(n){
      var el=document.querySelector('[name="'+n+'"]');
      if(el)el.value=(boxC[n]==null)?'':r1(boxC[n]);  // post Celsius
    });
  });
}
function upd(s){
  lastGauge=s.gauge_temp;renderBig();
  oven.textContent=s.oven_temp==null?'--':s.oven_temp.toFixed(2)+' C';
  mode.textContent=s.mode;bstatus.textContent=s.bridge;
  ble.textContent=s.ble;serial.textContent=s.serial;ip.textContent=s.ip;
  bcast.textContent=s.broadcasting?('yes ('+s.broadcast_mode+')'):'no';
  ovon.textContent=s.oven_on?'yes':'no';
  relayed.textContent=s.relay?(s.relayed.length?s.relayed.join(', '):'none in range'):'off';
  plink.textContent=s.connect?(s.central_state+' ('+s.probe_frames+' frames)'):'off';
  heap.textContent=(s.heap/1024|0)+' KB';uptime.textContent=s.uptime+'s';
}
initBoxes();setUnit(unit);
function addlog(line){
  var el=document.getElementById('log');
  var NL=String.fromCharCode(10);
  var lines=el.textContent.trim().split(NL);
  lines.push(line);
  el.textContent=lines.slice(-200).join(NL);
  el.scrollTop=el.scrollHeight;
}
var wsok=false;
function connectWS(){
  try{
    var ws=new WebSocket('ws://'+location.host+'/ws');
    ws.onopen=function(){wsok=true;conn.textContent='live';};
    ws.onmessage=function(ev){var m=JSON.parse(ev.data);
      if(m.t=='status')upd(m.d); else if(m.t=='log')addlog(m.d);};
    ws.onclose=function(){wsok=false;conn.textContent='reconnecting';setTimeout(connectWS,2000);};
    ws.onerror=function(){try{ws.close();}catch(e){}};
  }catch(e){conn.textContent='polling';setTimeout(connectWS,3000);}
}
connectWS();
async function poll(){ if(wsok)return; try{let r=await fetch('/status');upd(await r.json());}catch(e){} }
setInterval(poll,3000);
</script>
</div></body></html>"""


def _esc(v):
    return str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


class WebUI:
    def __init__(self, gauge, oven, transport, cfg, cfg_path="config.json",
                 log_state=None):
        self.gauge = gauge
        self.oven = oven
        self.transport = transport
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.log = log_state or {"seq": 0, "lines": []}
        self.ip = "0.0.0.0"
        self._t0 = time.ticks_ms()

    async def run(self):
        # Wait for WiFi to get an IP before binding.
        wlan = network.WLAN(network.STA_IF)
        while not wlan.isconnected():
            await asyncio.sleep(1)
        self.ip = wlan.ifconfig()[0]
        host = self.cfg.get("mdns_hostname") or "anovaRelay"
        print("[web] config UI at http://%s/  and  http://%s.local/"
              % (self.ip, host))
        await asyncio.start_server(self._handle, "0.0.0.0", 80)

    # -- rendering ----------------------------------------------------------

    def _render(self):
        g = self.gauge
        ha = g.high_alarm
        la = g.low_alarm
        html = PAGE
        repl = {
            "__FOLLOW__": "checked" if self.oven["follow"] else "",
            "__TEMP__": "%.1f" % g.temperature_c,
            "__SENSOR__": "checked" if g.sensor_present else "",
            "__BATT__": "checked" if g.low_battery else "",
            "__OVER__": "checked" if g.sensor_overheating else "",
            "__HIGH__": ("%.1f" % ha.temperature_c) if ha.set else "",
            "__LOW__": ("%.1f" % la.temperature_c) if la.set else "",
            "__BMA__": "checked" if self.oven["broadcast_mode"] != "oven" else "",
            "__BMO__": "checked" if self.oven["broadcast_mode"] == "oven" else "",
            "__GRACE__": str(self.oven["broadcast_grace_min"]),
            "__RELAY__": "checked" if self.transport.relay_enabled else "",
            "__CONNECT__": "checked" if self.transport.connect_enabled else "",
            "__SSID__": _esc(self.cfg.get("wifi_ssid", "")),
            "__PASS__": _esc(self.cfg.get("wifi_password", "")),
            "__MDNS__": _esc(self.cfg.get("mdns_hostname", "anovaRelay")),
            "__GSERIAL__": _esc(g.serial),
            "__APAT__": _esc(self.cfg.get("anova_pat", "")),
            "__FDEF__": "checked" if self.cfg.get("follow_oven", True) else "",
        }
        for k, v in repl.items():
            html = html.replace(k, v)
        return html

    def _status(self):
        return {
            "gauge_temp": self.gauge.temperature_c,
            "oven_temp": self.oven["celsius"],
            "mode": self.oven["mode"],
            "bridge": self.oven["status"],
            "ble": self.transport.subscriber_count if hasattr(
                self.transport, "subscriber_count") else len(
                self.transport.connections),
            "serial": self.gauge.serial,
            "ip": self.ip,
            "broadcasting": self.oven["broadcasting"],
            "broadcast_mode": self.oven["broadcast_mode"],
            "oven_on": self.oven["on"],
            "relay": self.transport.relay_enabled,
            "relayed": self.transport.relayed_serials(),
            "connect": self.transport.connect_enabled,
            "scanning": self.transport._scanning,
            "central_state": self.transport.central_state,
            "probe_frames": self.transport.probe_frame_count,
            "probe_handles": [self.transport._probe_tx, self.transport._probe_rx,
                              self.transport._probe_cccd],
            "probe_last_len": (len(self.transport.probe_frames[-1])
                               if self.transport.probe_frames else 0),
            "heap": _free_heap(),
            "uptime": time.ticks_diff(time.ticks_ms(), self._t0) // 1000,
        }

    # -- websocket ----------------------------------------------------------

    async def _ws_handler(self, reader, writer, key):
        accept = binascii.b2a_base64(
            _sha1((key + _WS_GUID).encode())).strip().decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        await writer.drain()
        last_seq = 0
        try:
            while True:
                writer.write(_ws_frame(json.dumps(
                    {"t": "status", "d": self._status()})))
                lines = self.log["lines"]
                if lines and lines[-1][0] > last_seq:
                    for seq, text in lines:
                        if seq > last_seq:
                            writer.write(_ws_frame(json.dumps(
                                {"t": "log", "d": text})))
                    last_seq = lines[-1][0]
                await writer.drain()
                await asyncio.sleep_ms(1000)
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # -- request handling ---------------------------------------------------

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            parts = line.decode().split(" ")
            method, path = parts[0], parts[1]
            clen = 0
            ws_key = None
            while True:
                h = await reader.readline()
                if h == b"\r\n" or not h:
                    break
                name, _, val = h.decode().partition(":")
                name = name.strip().lower()
                if name == "content-length":
                    clen = int(val.strip())
                elif name == "sec-websocket-key":
                    ws_key = val.strip()
            body = (await reader.readexactly(clen)).decode() if clen else ""

            if path == "/ws" and ws_key:
                await self._ws_handler(reader, writer, ws_key)
                return
            if path == "/status":
                self._send(writer, "200 OK", "application/json",
                           json.dumps(self._status()))
            elif method == "POST" and path == "/control":
                self._apply_control(_parse_form(body))
                self._redirect(writer)
            elif method == "POST" and path == "/config":
                reboot = self._apply_config(_parse_form(body))
                self._redirect(writer)
                await writer.drain()
                if reboot:
                    await asyncio.sleep(1)
                    machine.reset()
            elif path == "/" or path.startswith("/?"):
                self._send(writer, "200 OK", "text/html", self._render())
            else:
                self._send(writer, "404 Not Found", "text/plain", "not found")
            await writer.drain()
        except Exception as exc:
            print("[web] error:", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _send(self, writer, status, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        writer.write(b"HTTP/1.0 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % (status.encode(),
                     ctype.encode(), len(body)))
        writer.write(body)

    def _redirect(self, writer):
        writer.write(b"HTTP/1.0 303 See Other\r\nLocation: /\r\n"
                     b"Content-Length: 0\r\nConnection: close\r\n\r\n")

    # -- actions ------------------------------------------------------------

    def _apply_control(self, form):
        g = self.gauge
        self.oven["follow"] = "follow" in form
        if form.get("do") in ("temp", "all") and form.get("temp"):
            try:
                g.set_temperature(float(form["temp"]))
            except ValueError:
                pass
        if form.get("do") == "all":
            g.sensor_present = "sensor" in form
            g.low_battery = "batt" in form
            g.sensor_overheating = "over" in form
            self._set_alarm(g.high_alarm, form.get("high", ""))
            self._set_alarm(g.low_alarm, form.get("low", ""))
            g._evaluate_alarms()
            if "bmode" in form:
                mode = "oven" if form["bmode"] == "oven" else "always"
                self.oven["broadcast_mode"] = mode
                self.cfg["broadcast_mode"] = mode
            if form.get("grace"):
                try:
                    grace = int(float(form["grace"]))
                    self.oven["broadcast_grace_min"] = grace
                    self.cfg["broadcast_grace_min"] = grace
                except ValueError:
                    pass
            relay = "relay" in form
            self.transport.set_relay(relay)
            self.cfg["relay_probes"] = relay
            connect = "connect" in form
            self.transport.set_connect(connect)
            self.cfg["relay_connect"] = connect
            self._save_cfg()

    def _set_alarm(self, alarm, value):
        value = value.strip()
        if value == "":
            alarm.set = False
            alarm.tripped = False
            alarm.alarming = False
        else:
            try:
                alarm.temperature_c = float(value)
                alarm.set = True
            except ValueError:
                pass

    def _apply_config(self, form):
        for key in ("wifi_ssid", "wifi_password", "anova_pat",
                    "mdns_hostname"):
            if key in form:
                self.cfg[key] = form[key]
        if form.get("serial"):
            self.cfg["serial"] = form["serial"][:10]
        self.cfg["follow_oven"] = "follow_oven" in form
        self._save_cfg()
        return form.get("do") == "reboot"

    def _save_cfg(self):
        try:
            with open(self.cfg_path, "w") as f:
                json.dump(self.cfg, f)
        except Exception as exc:
            print("[web] config save failed:", exc)


def _free_heap():
    import gc
    return gc.mem_free()
