"""Reusable BLE central-role link (MicroPython).

A CentralLink owns one outbound GATT connection to a peripheral:

    connect -> discover service -> find notify (+ optional write) char
            -> find CCCD -> enable notifications -> exchange MTU -> ready

The on_* methods are called from the BLE IRQ and only record state; every
BLE stack call happens in service() on the main loop (calling gap/gattc APIs
from the IRQ panics the interrupt watchdog under WiFi coexistence).
"""

import time

import bluetooth

_CCCD = bluetooth.UUID(0x2902)
CONNECT_TIMEOUT_MS = 12000   # abandon a connect that never completes
ERROR_BACKOFF_MS = 10000     # wait this long after an error before retrying
WRITE_TIMEOUT_MS = 3000      # give up waiting for a write acknowledgement


class CentralLink:
    def __init__(self, ble, name, svc_uuid, notify_uuid, write_uuid=None,
                 keep=8, write_mode=1):
        self.ble = ble
        self.name = name
        # 1 = write with response (paced by the ack), 0 = without response
        # (what Combustion's own apps use for the MeatNet UART).
        self.write_mode = write_mode
        self.sent = 0
        self.mtu_err = None
        self.nmax = 0            # diagnostics: largest notification seen
        self.svc_uuid = bluetooth.UUID(svc_uuid)
        self.notify_uuid = bluetooth.UUID(notify_uuid)
        self.write_uuid = bluetooth.UUID(write_uuid) if write_uuid else None
        self.keep = keep
        self.enabled = False
        self.target = None       # (addr_type, addr bytes)
        self.key = None          # what we're connected to (e.g. a serial)
        self.frames = []         # recent notifications (bounded)
        self.count = 0
        self.state = "idle"
        self.mtu = 23
        self._err_at = None
        self._reset()

    def _reset(self):
        self.conn = None
        self.svc = None
        self.notify_h = None
        self.write_h = None
        self.cccd = None
        self.action = None
        self.started = None
        self.tx_queue = []
        self.tx_busy = False
        self.tx_at = None
        self.disconnect_sent = False

    @property
    def ready(self):
        return self.state == "ready"

    def set_target(self, addr_type, addr, key=None):
        self.target = (addr_type, bytes(addr))
        self.key = key

    def _fail(self, why):
        # Keep the "error" state through the disconnect so the back-off applies.
        self.state = "error: " + why
        self._err_at = time.ticks_ms()
        self.action = ("disconnect" if self.conn is not None
                       and not self.disconnect_sent else None)

    # -- IRQ side: record state only ----------------------------------------

    def on_connect(self, conn):
        self.conn = conn
        self.started = None
        self.state = "discovering"
        self.action = "discover_svc"

    def on_disconnect(self):
        self._reset()
        self.mtu = 23
        if not self.state.startswith("error"):
            self.state = "idle"

    def on_service(self, start, end, uuid):
        if uuid == self.svc_uuid:
            self.svc = (start, end)

    def on_service_done(self):
        if self.svc:
            self.action = "discover_char"
        else:
            self._fail("service not found")

    def on_char(self, value_handle, uuid):
        if uuid == self.notify_uuid:
            self.notify_h = value_handle
        elif self.write_uuid is not None and uuid == self.write_uuid:
            self.write_h = value_handle

    def on_char_done(self):
        if self.notify_h is None:
            self._fail("notify characteristic not found")
        elif self.write_uuid is not None and self.write_h is None:
            self._fail("write characteristic not found")
        else:
            self.action = "discover_desc"

    def on_desc(self, handle, uuid):
        if (self.cccd is None and self.notify_h is not None
                and handle > self.notify_h and uuid == _CCCD):
            self.cccd = handle

    def on_desc_done(self):
        self.action = "enable_notify"

    def on_notify(self, data):
        if len(data) > self.nmax:
            self.nmax = len(data)
        self.frames.append(bytes(data))
        self.count += 1
        if len(self.frames) > self.keep:
            self.frames.pop(0)

    def on_write_done(self):
        self.tx_busy = False

    def on_mtu(self, mtu):
        self.mtu = mtu
        self.tx_busy = False

    # -- main loop ----------------------------------------------------------

    def take_frames(self):
        frames, self.frames = self.frames, []
        return frames

    def write(self, data):
        """Queue data for the write characteristic. It is chunked to fit the
        MTU when sent, one chunk per service() call."""
        if not self.ready or self.write_h is None:
            return False
        self.tx_queue.append(bytes(data))
        return True

    def service(self, may_connect):
        now = time.ticks_ms()
        # A connect that never completes (e.g. a stale link on the peer).
        if (self.state == "connecting" and self.started
                and time.ticks_diff(now, self.started) > CONNECT_TIMEOUT_MS):
            try:
                self.ble.gap_connect(None)
            except OSError:
                pass
            self._reset()
            self.state = "idle"
        # Recover from an error after a back-off.
        if (self.state.startswith("error") and self.conn is None
                and self._err_at is not None
                and time.ticks_diff(now, self._err_at) > ERROR_BACKOFF_MS):
            self.state = "idle"
        # Stop when disabled; otherwise start connecting when allowed.
        if not self.enabled:
            if self.state == "connecting":
                try:
                    self.ble.gap_connect(None)
                except OSError:
                    pass
                self._reset()
                self.state = "idle"
            elif self.conn is not None and not self.disconnect_sent:
                self.action = "disconnect"
        elif (self.state == "idle" and self.target and may_connect
              and self.action is None):
            self.action = "connect"
        # Drain queued writes, one outstanding at a time.
        if self.ready and self.tx_queue:
            if self.tx_busy and time.ticks_diff(now, self.tx_at) > WRITE_TIMEOUT_MS:
                self.tx_busy = False
            if not self.tx_busy:
                data = self.tx_queue[0]
                n = max(20, self.mtu - 3)
                try:
                    self.ble.gattc_write(self.conn, self.write_h, data[:n],
                                         self.write_mode)
                    self.sent += 1
                    if self.write_mode:
                        self.tx_busy = True
                        self.tx_at = now
                    if len(data) > n:
                        self.tx_queue[0] = data[n:]
                    else:
                        self.tx_queue.pop(0)
                except OSError:
                    pass       # retry next tick
        act = self.action
        if not act:
            return
        if act == "mtu" and self.tx_busy:
            # Let the CCCD write finish first (one ATT request at a time).
            if time.ticks_diff(now, self.tx_at) <= WRITE_TIMEOUT_MS:
                return
            self.tx_busy = False
        self.action = None
        try:
            if act == "connect":
                self.state = "connecting"
                self.started = now
                self.ble.gap_connect(self.target[0], self.target[1])
            elif act == "discover_svc":
                self.ble.gattc_discover_services(self.conn, self.svc_uuid)
            elif act == "discover_char":
                self.ble.gattc_discover_characteristics(
                    self.conn, self.svc[0], self.svc[1])
            elif act == "discover_desc":
                self.ble.gattc_discover_descriptors(
                    self.conn, self.notify_h, self.svc[1])
            elif act == "enable_notify":
                if self.cccd is None:
                    self._fail("no CCCD for notifications")
                    return
                self.ble.gattc_write(self.conn, self.cccd, b"\x01\x00", 1)
                self.tx_busy = True
                self.tx_at = now
                self.action = "mtu"
            elif act == "mtu":
                # Exchange the MTU before reporting ready so the first frame
                # isn't split into 20-byte writes.
                try:
                    self.ble.gattc_exchange_mtu(self.conn)
                    self.tx_busy = True     # hold writes until it completes
                    self.tx_at = now
                    self.mtu_err = None
                except OSError as exc:
                    self.mtu_err = str(exc)  # peer may have exchanged already
                self.state = "ready"
            elif act == "disconnect":
                if self.conn is not None and not self.disconnect_sent:
                    self.disconnect_sent = True
                    if not self.state.startswith("error"):
                        self.state = "disconnecting"
                    self.ble.gap_disconnect(self.conn)
        except OSError as exc:
            self._fail(str(exc))
