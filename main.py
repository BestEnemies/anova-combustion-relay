"""ESP32 Combustion Helper (MicroPython).

Starts automatically on boot. Connect a serial terminal (e.g.
`mpremote repl` or PuTTY at 115200) and type a temperature.
Commands: a number sets the temperature in Celsius (suffix f/c to
override), plus 'alarm high 150', 'sensor off', 'battery low',
'status', 'adv', 'help'.
"""

import asyncio
import sys
import time
import gc

import machine

import protocol
from gauge import GaugeEmulator

HELP = """Commands:
  <number>[f|c]     set temperature, e.g. 110  or  225f
  unit f | unit c   change default input unit
  alarm high <t>|off   / alarm low <t>|off
  sensor on|off     battery low|ok     overheat on|off
  oven on|off       follow the Anova oven temperature (or stop)
  status            adv               help"""


def load_config():
    try:
        import json
        with open("config.json") as f:
            return json.load(f)
    except Exception:
        return {}


# Rolling log buffer streamed to the web UI over WebSocket.
_log = {"seq": 0, "lines": []}  # lines: [(seq, text)]


def log(msg):
    print(msg)
    _log["seq"] += 1
    _log["lines"].append((_log["seq"], msg))
    if len(_log["lines"]) > 80:
        _log["lines"] = _log["lines"][-80:]


# Shared state between the Anova networking thread and the BLE loop.
oven = {
    "celsius": None,      # latest bulb temperature
    "mode": "dry",        # bulb mode: "dry" or "wet" (sous vide)
    "status": "off",      # human-readable bridge status
    "updated": 0,         # time.ticks_ms of last reading
    "follow": True,       # apply oven temp to the gauge
    "on": False,          # is the oven actively cooking
    "off_since": None,    # time.ticks_ms when the oven last turned off
    "broadcast_mode": "always",   # "always" or "oven"
    "broadcast_grace_min": 10,    # keep broadcasting this long after off
    "broadcasting": True,         # current BLE broadcast state (display)
    "relay_mode": "oven",         # probe relay: "off" / "on" / "oven"
}


def should_broadcast(now):
    """Decide whether the oven gate is open (oven on, or within grace)."""
    if oven["on"]:
        return True
    if oven["off_since"] is not None:
        grace = int(oven["broadcast_grace_min"]) * 60000
        return time.ticks_diff(now, oven["off_since"]) < grace
    return False


def should_emit_own(now):
    """Whether our own oven-gauge advertisement should go out."""
    if oven["broadcast_mode"] != "oven":
        return True
    return should_broadcast(now)


def should_relay(now):
    """Whether the probe relay should be active right now (tri-state)."""
    m = oven["relay_mode"]
    if m == "on":
        return True
    if m == "oven":
        return should_broadcast(now)
    return False


def start_anova_bridge(cfg):
    """Connect WiFi and stream oven temperature in a background thread."""
    import _thread
    import network
    import time

    ssid = cfg.get("wifi_ssid")
    if not ssid:
        oven["status"] = "no wifi configured"
        return
    oven["follow"] = cfg.get("follow_oven", True)

    def worker():
        # Set the mDNS/DHCP hostname BEFORE bringing the interface up so the
        # ESP32 mDNS responder advertises <hostname>.local.
        hostname = cfg.get("mdns_hostname") or "anovaRelay"
        try:
            network.hostname(hostname)
        except Exception:
            pass
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        try:
            wlan.config(hostname=hostname)
        except Exception:
            pass
        if not wlan.isconnected():
            oven["status"] = "wifi connecting"
            wlan.connect(ssid, cfg.get("wifi_password", ""))
            for _ in range(40):
                if wlan.isconnected():
                    break
                time.sleep(0.5)
        if not wlan.isconnected():
            oven["status"] = "wifi failed"
            return
        oven["status"] = "wifi ok, starting anova"
        # Wall-clock time lets a cook profile resume correctly after a reboot.
        try:
            import ntptime
            for _ in range(5):
                try:
                    ntptime.settime()
                    log("[time] clock synced via NTP")
                    break
                except Exception:
                    time.sleep(3)
        except ImportError:
            pass

        import anova

        def on_temp(celsius, mode, oven_on):
            oven["celsius"] = celsius
            oven["mode"] = mode
            was_on = oven["on"]
            oven["on"] = oven_on
            if oven_on:
                oven["off_since"] = None
            elif was_on:
                oven["off_since"] = time.ticks_ms()
            oven["updated"] = time.ticks_ms()
            oven["status"] = "streaming"

        def on_status(msg):
            oven["status"] = msg
            log("[anova] " + msg)

        try:
            anova.run_forever(cfg["anova_pat"], on_temp, on_status)
        except Exception as exc:
            import sys
            oven["status"] = "thread died: " + str(exc)
            print("[anova] THREAD DIED:")
            sys.print_exception(exc)

    _thread.start_new_thread(worker, ())


def default_serial():
    # Real Gauge serials are EXACTLY 10 alphanumeric chars; shorter serials
    # crash the Combustion app (it copies 10 bytes from the DIS string).
    uid = machine.unique_id()
    return "".join("%02X" % b for b in uid)[-10:]  # 10 hex chars from MAC


class Console:
    def __init__(self, gauge, transport):
        self.gauge = gauge
        self.transport = transport
        self.unit = "c"

    def parse_temp(self, text):
        text = text.strip().lower()
        unit = self.unit
        if text and text[-1] in "fc":
            unit = text[-1]
            text = text[:-1]
        value = float(text)
        return (value - 32.0) * 5.0 / 9.0 if unit == "f" else value

    def fmt(self, celsius):
        if self.unit == "f":
            return "%.1fF" % (celsius * 9 / 5 + 32)
        return "%.1fC" % celsius

    def handle(self, line):
        line = line.strip()
        if not line:
            return
        lower = line.lower()
        g = self.gauge
        if lower in ("help", "?"):
            print(HELP)
        elif lower == "status":
            print(g.summary())
            print("BLE:          %d connection(s), advertising" %
                  len(self.transport.connections))
            oc = oven["celsius"]
            print("Oven bridge:  %s | temp=%s | mode=%s | follow=%s" % (
                oven["status"],
                "%.2fC" % oc if oc is not None else "--",
                oven["mode"], oven["follow"]))
        elif lower.startswith("oven"):
            parts = lower.split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                oven["follow"] = (parts[1] == "on")
                print("Oven follow " + ("ON" if oven["follow"] else "OFF"))
            else:
                oc = oven["celsius"]
                print("Oven bridge: %s | temp=%s | follow=%s" % (
                    oven["status"],
                    "%.2fC" % oc if oc is not None else "--", oven["follow"]))
        elif lower == "adv":
            adv = g.advertisement()
            print("Manufacturer data (%d bytes): %s" %
                  (len(adv), " ".join("%02x" % b for b in adv)))
        elif lower.startswith("unit"):
            parts = lower.split()
            if len(parts) == 2 and parts[1] in ("f", "c"):
                self.unit = parts[1]
                print("Default unit set to " + self.unit.upper())
            else:
                print("Usage: unit f | unit c")
        elif lower.startswith("sensor"):
            g.sensor_present = lower.endswith("on")
            print("Sensor " + ("connected" if g.sensor_present else "disconnected"))
        elif lower.startswith("battery"):
            g.low_battery = lower.endswith("low")
            print("Battery " + ("LOW" if g.low_battery else "OK"))
        elif lower.startswith("overheat"):
            g.sensor_overheating = lower.endswith("on")
            print("Overheating flag " + ("set" if g.sensor_overheating else "cleared"))
        elif lower.startswith("alarm"):
            self._handle_alarm(lower)
        else:
            try:
                g.set_temperature(self.parse_temp(line))
            except ValueError:
                print("Unrecognized command: %r (type 'help')" % line)
                return
            print("Temperature set to %s (%.1fC)" %
                  (self.fmt(g.temperature_c), g.temperature_c))
            self._report_alarms()

    def _handle_alarm(self, lower):
        parts = lower.split()
        if len(parts) != 3 or parts[1] not in ("high", "low"):
            print("Usage: alarm high <temp>|off / alarm low <temp>|off")
            return
        alarm = (self.gauge.high_alarm if parts[1] == "high"
                 else self.gauge.low_alarm)
        if parts[2] == "off":
            alarm.set = False
            alarm.tripped = False
            alarm.alarming = False
            print(parts[1] + " alarm disabled")
            return
        try:
            alarm.temperature_c = self.parse_temp(parts[2])
        except ValueError:
            print("Bad temperature: " + parts[2])
            return
        alarm.set = True
        self.gauge._evaluate_alarms()
        print("%s alarm set at %s" % (parts[1], self.fmt(alarm.temperature_c)))
        self._report_alarms()

    def _report_alarms(self):
        for name, alarm in (("HIGH", self.gauge.high_alarm),
                            ("LOW", self.gauge.low_alarm)):
            if alarm.set and alarm.tripped:
                print("  ** %s ALARM TRIPPED at %s **" %
                      (name, self.fmt(alarm.temperature_c)))


TRACE = True


def _hex(b):
    return " ".join("%02x" % x for x in b)


async def ble_loop(transport, engine):
    """Fast loop for BLE central work: each link step (connect, discover,
    write) takes one service() call, so run it at 10 Hz."""
    last = {}
    while True:
        transport.service()
        if engine:
            # Only feed the Engine while our gauge is "on" (advertising).
            engine.feed_allowed = transport.emit_own
            try:
                engine.service()
            except Exception as exc:
                log("[engine] error: %s" % exc)
        for link in transport.links:
            if last.get(link.name) != link.state:
                last[link.name] = link.state
                if link.name != "engine" or link.enabled or link.state != "idle":
                    log("[%s] central link: %s" % (link.name, link.state))
        await asyncio.sleep_ms(100)


async def background_loop(gauge, transport, runner=None):
    was_connected = 0
    last_applied = None
    ticks = 0
    while True:
        ticks += 1
        if ticks % 30 == 0:
            # Reclaim the per-loop garbage periodically so free heap stays in a
            # steady band instead of drifting down until GC is forced.
            gc.collect()
        if runner:
            try:
                runner.service()
            except Exception as exc:
                log("[profile] error: %s" % exc)
        # Apply the latest oven temperature to the gauge (if following).
        if oven["follow"] and oven["celsius"] is not None:
            if oven["celsius"] != last_applied:
                last_applied = oven["celsius"]
                gauge.set_temperature(oven["celsius"])
        gauge.tick()
        now = time.ticks_ms()
        # The oven transmit option gates our own gauge advertisement; the
        # relay follows its own tri-state (off / on / only-while-oven-on).
        transport.emit_own = should_emit_own(now)
        transport.set_relay(should_relay(now))
        transport.rotate_advertisement()
        oven["broadcasting"] = transport._advertising
        n = len(transport.connections)
        if n != was_connected:
            log("[ble] app connections: %d -> %d" % (was_connected, n))
            was_connected = n
        for data in transport.poll_rx():
            if TRACE:
                log("[rx %dB] %s" % (len(data), _hex(data[:12])))
            for mtype, req_id, payload in protocol.parse_requests(data):
                if mtype == protocol.MSG_READ_PROBE_LIST:
                    # App asks which probes we repeat -> report the one we're
                    # connected to (the #2 relay).
                    replies = [protocol.build_read_probe_list_response(
                        req_id, transport.connected_probe_serials())]
                    log("[relay] app read-probe-list -> %s" %
                        transport.relayed_serials())
                else:
                    replies = gauge._handle_request(mtype, req_id, payload)
                for frame in replies:
                    if TRACE:
                        log("[tx %dB] type=0x%02x" % (len(frame), frame[4]))
                    transport.notify(frame)
        if transport.connections:
            transport.notify(gauge.status_notification())
            # Forward the connected probe's status to the app (#2 relay).
            ps = transport.latest_probe_status()
            if ps:
                transport.notify(protocol.build_node_probe_status(ps[0], ps[1]))
        await asyncio.sleep_ms(1000)


async def console_loop(console):
    reader = asyncio.StreamReader(sys.stdin)
    while True:
        sys.stdout.write("gauge> ")
        line = await reader.readline()
        if not line:
            await asyncio.sleep_ms(200)
            continue
        try:
            console.handle(line.decode() if isinstance(line, bytes) else line)
        except Exception as exc:
            print("Error:", exc)


async def amain():
    cfg = load_config()
    oven["broadcast_mode"] = cfg.get("broadcast_mode", "always")
    oven["broadcast_grace_min"] = cfg.get("broadcast_grace_min", 10)
    serial = cfg.get("serial") or default_serial()
    gauge = GaugeEmulator(serial=serial)
    from ble import BleTransport
    transport = BleTransport(gauge)
    rp = cfg.get("relay_probes", "oven")
    if isinstance(rp, bool):          # migrate old boolean config
        rp = "on" if rp else "off"
    if rp not in ("off", "on", "oven"):
        rp = "oven"
    oven["relay_mode"] = rp
    print("MeatNet relay mode:", rp)
    if cfg.get("relay_connect"):
        transport.set_connect(True)
        print("MeatNet connect proxy: will connect to a probe")
    engine = runner = store = None
    if cfg.get("engine_enabled", True):
        try:
            from engine import EngineController
            from profiles import ProfileStore, ProfileRunner
            engine = EngineController(transport, log, cfg.get("engine_serial", ""))
            engine.set_enabled(True)
            # Stream our gauge status to the Engine when it uses us as its
            # control device (a real Gauge pushes 0x60 to the Engine).
            engine.configure_feed(gauge.serial, gauge.status_notification)
            store = ProfileStore()
            runner = ProfileRunner(engine, log, cfg.get("profile_probe", ""))
            print("Engine control: on (%d cook profiles)" % len(store.profiles))
        except Exception as exc:
            sys.print_exception(exc)
            print("Engine control failed to start:", exc)
    print("\nESP32 Combustion Helper")
    print("Serial: %s  |  advertising 0x09C7 + DFU FE59 scan response" %
          gauge.serial)
    if cfg.get("wifi_ssid"):
        print("Anova bridge: connecting to WiFi '%s'..." % cfg["wifi_ssid"])
        try:
            start_anova_bridge(cfg)
        except Exception as exc:
            print("Anova bridge failed to start:", exc)
    else:
        print("Anova bridge: no WiFi in config.json (manual temps only)")
    print("Type a temperature in C (e.g. 110) or 'help'.\n")
    console = Console(gauge, transport)
    tasks = [ble_loop(transport, engine),
             background_loop(gauge, transport, runner), console_loop(console)]
    if cfg.get("wifi_ssid"):
        try:
            from web import WebUI
            tasks.append(WebUI(gauge, oven, transport, cfg, log_state=_log,
                               engine=engine, runner=runner, store=store).run())
        except Exception as exc:
            print("Web UI failed to start:", exc)
    await asyncio.gather(*tasks)


def run():
    try:
        asyncio.run(amain())
    finally:
        asyncio.new_event_loop()


run()
