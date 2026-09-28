"""Combustion Engine control + probe core readings (MicroPython).

The Engine's state (set point, App Mode, lid) and nearby probes' virtual core
temperatures are read passively from BLE advertisements. A MeatNet UART link
to the Engine is opened only while a set point change is pending (plus a short
linger), so we don't tie up one of the Engine's connection slots.

A set point change follows the official app's pattern: send Set Engine
Temperature Set Point (0x71), re-send every 5 s, and confirm by watching the
Engine report the new set point. If the set point changes to something else
while we're trying, someone else changed it - that counts as done.
"""

import time

import protocol

RESEND_MS = 5000
COMMAND_TIMEOUT_MS = 45000   # includes connecting + service discovery
LINGER_MS = 15000            # keep the link briefly after a command
STALE_MS = 300000            # forget devices not heard for 5 minutes
FRESH_MS = 60000             # readings older than this aren't used
_TOL = 0.15                  # set point match tolerance (C)


def serial_str(serial10):
    """Engine serials are ASCII; fall back to hex if not printable."""
    s = bytes(serial10).rstrip(b"\x00")
    if s and all(0x20 < c < 0x7F for c in s):
        return s.decode()
    return "".join("%02X" % c for c in serial10)


class EngineController:
    def __init__(self, transport, log=print, pinned_serial=""):
        self.t = transport
        self.link = transport.engine_link
        self.log = log
        self.pinned = (pinned_serial or "").strip()
        self.serial = None        # 10 bytes of the selected Engine
        self.addr = None          # (addr_type, addr)
        self.rssi = None
        self.seen_at = None       # ticks of the last advert
        self.state = {}           # merged advert + 0x70 status
        self.sp_at = None         # ticks the set point was last observed
        self.status_at = None     # ticks of the last 0x70 status
        self.node_probes = {}     # serial4 -> (decoded, ticks) from 0x45
        self.cmd = None
        self.last_result = None   # (text, ticks)
        self._rx = b""
        self.seen_types = {}      # diagnostics: UART message type -> count
        # Gauge -> Engine feed (see configure_feed)
        self.feed_fn = None
        self.my_serial = None
        self.feed_allowed = True  # main loop: is our gauge "on" (advertising)
        self.feeding = False
        self.fed = 0
        self._feed_at = None
        self._ctrl_check_at = None
        self._adv_ticks = None
        self._done_at = None

    def set_enabled(self, enabled):
        self.t.set_engine_scan(enabled)
        if not enabled:
            self.link.enabled = False

    # -- selection / observation -------------------------------------------

    def _select(self, now):
        best = None
        for serial, rec in list(self.t.engines.items()):
            body, ticks, addr_type, addr, rssi = rec
            if time.ticks_diff(now, ticks) > STALE_MS:
                del self.t.engines[serial]
                continue
            if self.pinned and serial_str(serial) != self.pinned:
                continue
            # Prefer the current Engine, then the strongest signal.
            if serial == self.serial:
                best = (serial, rec)
                break
            if best is None or rssi > best[1][4]:
                best = (serial, rec)
        if best is None:
            return
        serial, (body, ticks, addr_type, addr, rssi) = best
        if serial != self.serial:
            self.serial = serial
            self.state = {}
            self.sp_at = None
            self.log("[engine] using Engine %s" % serial_str(serial))
        self.addr = (addr_type, addr)
        self.rssi = rssi
        self.seen_at = ticks
        if ticks != self._adv_ticks:
            self._adv_ticks = ticks
            adv = protocol.parse_engine_advert(body)
            if adv:
                self.state.update(adv)
                self.sp_at = ticks

    def _read_link(self, now):
        chunks = self.link.take_frames()
        if not chunks:
            return
        buf = self._rx + b"".join(chunks)
        frames, self._rx = protocol.split_frames(buf)
        if len(self._rx) > 512:
            self._rx = b""
        for is_resp, mtype, req_id, success, payload in frames:
            self.seen_types[(mtype | 0x80) if is_resp else mtype] = \
                self.seen_types.get((mtype | 0x80) if is_resp else mtype, 0) + 1
            if is_resp:
                if (mtype == protocol.MSG_SET_ENGINE_SETPOINT and self.cmd
                        and req_id == self.cmd["rid"]):
                    self.cmd["ack"] = success
                continue
            if mtype == protocol.MSG_ENGINE_STATUS:
                st = protocol.parse_engine_status(payload)
                if st and (self.serial is None or st["serial"] == self.serial):
                    self.state.update(st)
                    self.sp_at = now
                    self.status_at = now
            elif mtype == protocol.MSG_PROBE_STATUS:
                r = protocol.parse_node_probe_status(payload)
                if r:
                    self.node_probes[r[0]] = (r[1], now)

    # -- set point commands --------------------------------------------------

    def request_setpoint(self, celsius, why=""):
        """Start changing the Engine set point. Returns an error string, or
        None if the request was accepted (the result arrives later)."""
        if self.serial is None:
            return "no Engine found (is it on and in range?)"
        c = max(protocol.ENGINE_SETPOINT_MIN_C,
                min(protocol.ENGINE_SETPOINT_MAX_C, float(celsius)))
        now = time.ticks_ms()
        self.cmd = {"c": c, "t0": now, "sent": None, "tries": 0, "rid": None,
                    "ack": None, "prev": self.state.get("setpoint_c"),
                    "why": why}
        self.log("[engine] set point -> %.1f C%s" % (c, (" (" + why + ")") if why else ""))
        return None

    # -- gauge -> Engine feed ------------------------------------------------
    #
    # A real Gauge used as an Engine's control device connects OUT to the
    # Engine and pushes Gauge Status (0x60) over MeatNet UART; the Engine never
    # connects to the gauge. So when the Engine's control device is our
    # virtual gauge, hold a link open and stream our status every second.

    def configure_feed(self, my_serial, frame_fn):
        """Feed frame_fn() to the Engine while its control device (chosen in
        the Combustion app) is the gauge with serial my_serial."""
        self.my_serial = my_serial.encode()[:10] if isinstance(my_serial, str) \
            else bytes(my_serial[:10])
        self.feed_fn = frame_fn

    def ctrl_is_us(self):
        """True/False if the Engine's control device is our gauge, None if
        we haven't read an Engine Status yet."""
        s = self.state.get("ctrl_serial")
        if s is None:
            return None
        return (self.state.get("ctrl_type") == protocol.PRODUCT_TYPE_GAUGE
                and bytes(s[:10]) == self.my_serial)

    def _update_feed(self, now):
        want = False
        if self.serial is not None and self.feed_fn and self.feed_allowed:
            us = self.ctrl_is_us()
            want = bool(us)
            if not want and not self.link.enabled and not self.cmd:
                # Learn the Engine's control device (only in its 0x70 status,
                # so connect briefly). Check often while the Engine says its
                # control device isn't connected.
                every = (120000 if self.state.get("ctrl_connected") is False
                         else 1800000)
                stale = (self.status_at is None or
                         time.ticks_diff(now, self.status_at) > every)
                if (us is None or stale) and (
                        self._ctrl_check_at is None or
                        time.ticks_diff(now, self._ctrl_check_at) > every):
                    self._ctrl_check_at = now
                    self.peek()
        if want != self.feeding:
            self.feeding = want
            self._feed_at = None
            if want:
                self.log("[engine] Engine uses this gauge as its control device"
                         " - streaming gauge status to it")
            else:
                self.log("[engine] stopped streaming gauge status to the Engine")
                self._done_at = now      # linger, then drop the link

    def _service_feed(self, now):
        if self.addr and not self.link.ready and self.link.target != self.addr:
            self.link.set_target(self.addr[0], self.addr[1], self.serial)
        self.link.enabled = True
        if self.link.ready and (self._feed_at is None or
                                time.ticks_diff(now, self._feed_at) >= 1000):
            self._feed_at = now
            if self.link.write(self.feed_fn()):
                self.fed += 1

    def peek(self):
        """Connect briefly (the linger period) just to read Engine Status."""
        if not self.addr:
            return "no Engine found"
        if self.link.target != self.addr:
            self.link.set_target(self.addr[0], self.addr[1], self.serial)
        self.link.enabled = True
        self._done_at = time.ticks_ms() + 15000   # ~30 s total
        return None

    def _finish(self, result, now):
        cmd = self.cmd
        self.cmd = None
        self._done_at = now
        self.last_result = (result, now)
        self.log("[engine] set point %.1f C: %s" % (cmd["c"], result))

    def result_for(self, since):
        """The command result if it finished after `since` (ticks), else None."""
        if self.last_result and time.ticks_diff(self.last_result[1], since) >= 0:
            return self.last_result[0]
        return None

    def _service_cmd(self, now):
        cmd = self.cmd
        sp = self.state.get("setpoint_c")
        fresh = self.sp_at is not None and time.ticks_diff(now, self.sp_at) < 10000
        # Already at the requested value - nothing to send.
        if cmd["sent"] is None and fresh and sp is not None and abs(sp - cmd["c"]) < _TOL:
            self._finish("ok (already set)", now)
            return
        if cmd["sent"] is not None and self.sp_at is not None \
                and time.ticks_diff(self.sp_at, cmd["sent"]) >= 0 and sp is not None:
            if abs(sp - cmd["c"]) < _TOL:
                self._finish("ok", now)
                return
            prev = cmd["prev"]
            if prev is not None and abs(sp - prev) >= _TOL:
                self._finish("overridden (changed to %.1f C elsewhere)" % sp, now)
                return
        if time.ticks_diff(now, cmd["t0"]) > COMMAND_TIMEOUT_MS:
            if self.state.get("app_mode") is False:
                why = "Engine is not in App Mode"
            elif cmd["ack"] is False:
                why = "Engine rejected the command"
            elif not self.link.ready:
                why = "could not connect (%s)" % self.link.state
            else:
                why = "no confirmation"
            self._finish("failed: " + why, now)
            return
        # Connect on demand.
        if self.addr and not self.link.ready:
            if self.link.target != self.addr:
                self.link.set_target(self.addr[0], self.addr[1], self.serial)
            self.link.enabled = True
        if self.link.ready and (cmd["sent"] is None
                                or time.ticks_diff(now, cmd["sent"]) > RESEND_MS):
            rid = protocol.new_message_id()
            if self.link.write(protocol.build_set_engine_setpoint(
                    self.serial, cmd["c"], rid)):
                cmd["rid"] = rid
                cmd["sent"] = now
                cmd["tries"] += 1

    # -- main loop -----------------------------------------------------------

    def service(self):
        now = time.ticks_ms()
        self._select(now)
        self._read_link(now)
        self._update_feed(now)
        if self.feeding:
            self._service_feed(now)
        if self.cmd:
            self._service_cmd(now)
        elif self.feeding:
            pass
        elif self.link.enabled and (self._done_at is None or
                                    time.ticks_diff(now, self._done_at) > LINGER_MS):
            self.link.enabled = False
        for s in list(self.node_probes):
            if time.ticks_diff(now, self.node_probes[s][1]) > STALE_MS:
                del self.node_probes[s]

    # -- probe readings --------------------------------------------------------

    def probe_readings(self):
        """serial4 -> {"core_c", "surface_c", "ambient_c", "age_s", "src"},
        freshest source wins (Engine link, probe link, adverts)."""
        now = time.ticks_ms()
        out = {}

        def put(serial, d, ticks, src):
            age = time.ticks_diff(now, ticks)
            if age > STALE_MS:
                return
            cur = out.get(serial)
            if cur is None or age < cur["age_ms"]:
                out[serial] = {"core_c": d["core_c"], "surface_c": d["surface_c"],
                               "ambient_c": d["ambient_c"], "mode": d["mode"],
                               "age_ms": age, "src": src}

        for serial, (body, ticks, ptype) in list(self.t.probe_raw.items()):
            if time.ticks_diff(now, ticks) > STALE_MS:
                del self.t.probe_raw[serial]
                continue
            r = protocol.parse_probe_advert(body)
            if r:
                put(r[0], r[1], ticks, "advert" if ptype == 1 else "repeated")
        for serial, (d, ticks) in self.node_probes.items():
            put(serial, d, ticks, "engine")
        ps = self.t.latest_probe_status()
        if ps:
            s94 = ps[1]
            put(bytes(ps[0]), protocol.decode_probe(s94[8:21], s94[21], s94[22]),
                now, "probe link")
        return out

    def core_c(self, serial4):
        """Fresh virtual core temperature for a probe, or None."""
        r = self.probe_readings().get(serial4)
        if r and r["age_ms"] < FRESH_MS:
            return r["core_c"]
        return None

    # -- status for the UI -----------------------------------------------------

    def status(self):
        now = time.ticks_ms()
        st = self.state
        out = {
            "found": self.serial is not None,
            "serial": serial_str(self.serial) if self.serial else None,
            "age_s": (time.ticks_diff(now, self.seen_at) // 1000
                      if self.seen_at is not None else None),
            "rssi": self.rssi,
            "setpoint_c": st.get("setpoint_c"),
            "app_mode": st.get("app_mode"),
            "lid_open": st.get("lid_open"),
            "link": self.link.state if self.link.enabled or self.link.conn is not None else "off",
            "pending": self.cmd["c"] if self.cmd else None,
            "result": self.last_result[0] if self.last_result else None,
            # link diagnostics: writes sent, notifications received, MTU,
            # and the last command's ack (None = none seen)
            "diag": {"tx": self.link.sent, "rx": self.link.count,
                     "mtu": self.link.mtu, "mtu_err": self.link.mtu_err,
                     "nmax": self.link.nmax, "mtu_ev": self.t.mtu_events,
                     "types": ["%02X:%d" % kv for kv in self.seen_types.items()],
                     "ack": self.cmd["ack"] if self.cmd else None},
        }
        out["ctrl_connected"] = st.get("ctrl_connected")
        out["feeding"] = self.feeding
        out["ctrl_is_us"] = self.ctrl_is_us()
        out["fed"] = self.fed
        if "ctrl_serial" in st:
            out["ctrl_device"] = "type %d %s" % (
                st["ctrl_type"], bytes(st["ctrl_serial"]).rstrip(b"\x00"))
        eaddr = self.addr[1] if self.addr else None
        out["gauge_clients"] = [
            ":".join("%02X" % b for b in a) + (" (Engine)" if a == eaddr else "")
            for a in self.t.client_addr.values()]
        if eaddr:
            out["addr"] = ":".join("%02X" % b for b in eaddr)
        if self.status_at is not None and time.ticks_diff(now, self.status_at) < FRESH_MS:
            out["control_c"] = st.get("control_c")
            out["fan_duty"] = st.get("fan_duty")
            out["reached"] = st.get("reached_setpoint")
        return out
