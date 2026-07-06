"""ESP32 BLE transport: full Gauge advertising set + NUS GATT server.

Unlike the desktop OSes, the ESP32 lets us replicate the real Gauge's
advertising exactly:
  - connectable advertisement: flags + manufacturer data (0x09C7, type 3)
  - scan response: 128-bit Nordic DFU service UUID (0xFE59) - the UUID the
    official Combustion apps filter their scans on
"""

import bluetooth
import struct

import protocol

import time

_IRQ_CENTRAL_CONNECT = 1       # a central (the app) connected to us
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6
_IRQ_PERIPHERAL_CONNECT = 7    # we connected to a peripheral (the probe)
_IRQ_PERIPHERAL_DISCONNECT = 8
_IRQ_GATTC_SERVICE_RESULT = 9
_IRQ_GATTC_SERVICE_DONE = 10
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE = 12
_IRQ_GATTC_DESCRIPTOR_RESULT = 13
_IRQ_GATTC_DESCRIPTOR_DONE = 14
_IRQ_GATTC_NOTIFY = 18
_IRQ_MTU_EXCHANGED = 21

_ADV_INTERVAL_US = 200_000  # 200 ms, in the range real hardware uses

_FLAGS_AD = b"\x02\x01\x06"  # LE General Discoverable, BR/EDR unsupported

_VENDOR_LE = b"\xC7\x09"     # Combustion company ID, little-endian
_PRODUCT_TYPE_PROBE = 1
_PRODUCT_TYPE_NODE = 2       # MeatNet repeater node
_RELAY_EXPIRE_MS = 30000     # forget a probe not heard for this long

# The probe pushes live status on its own service (not the UART service).
_PROBE_STATUS_SVC = "00000100-CAAB-3792-3D44-97AE51C1407A"
_PROBE_STATUS_CHAR = "00000101-CAAB-3792-3D44-97AE51C1407A"


def _uuid128(s):
    return bluetooth.UUID(s)


class BleTransport:
    def __init__(self, gauge):
        self.gauge = gauge
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        try:
            self.ble.config(mtu=185)  # 0x60 status frame is 44 bytes
        except Exception:
            pass
        self.ble.irq(self._irq)

        uart = (
            _uuid128(protocol.UART_SERVICE_UUID),
            (
                (_uuid128(protocol.UART_TX_CHAR_UUID),
                 bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY),
                (_uuid128(protocol.UART_RX_CHAR_UUID),
                 bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
            ),
        )
        # Device Information Service - the Combustion app reads these right
        # after connecting; the model string format identifies a Gauge.
        dis = (
            bluetooth.UUID(0x180A),
            (
                (bluetooth.UUID(0x2A25), bluetooth.FLAG_READ),  # serial
                (bluetooth.UUID(0x2A26), bluetooth.FLAG_READ),  # firmware
                (bluetooth.UUID(0x2A27), bluetooth.FLAG_READ),  # hardware
                (bluetooth.UUID(0x2A24), bluetooth.FLAG_READ),  # model
                (bluetooth.UUID(0x2A29), bluetooth.FLAG_READ),  # manufacturer
            ),
        )
        ((self._tx_handle, self._rx_handle),
         (h_serial, h_fw, h_hw, h_model, h_mfr)) = \
            self.ble.gatts_register_services((uart, dis))
        self.ble.gatts_write(h_serial, gauge.serial.encode())
        self.ble.gatts_write(h_fw, b"v1.2.3")
        self.ble.gatts_write(h_hw, b"1.0")
        self.ble.gatts_write(h_model, b"Gauge REL-00001")
        self.ble.gatts_write(h_mfr, b"Combustion Inc.")
        # Queue up to several writes between polls
        self.ble.gatts_set_buffer(self._rx_handle, 256, True)

        self.connections = set()
        self.conn_mtu = {}  # conn_handle -> negotiated ATT MTU (default 23)
        self.rx_queue = []  # written payloads, drained by the main loop
        self._last_adv = None
        # Deferred work: BLE stack calls must NOT run inside the IRQ handler
        # (doing so panics the interrupt watchdog under WiFi coexistence).
        self._need_advertise = False
        self._pending_mtu = []
        # emit_own gates OUR OWN oven-gauge advertisement (the oven transmit
        # option). _advertising tracks whether the radio is advertising at
        # all. The relay features below are independent of emit_own.
        self.emit_own = True
        self._advertising = True
        # MeatNet advertisement relay: re-broadcast nearby probes' data.
        self.relay_enabled = False
        self.repeated = {}   # probe serial bytes -> (manuf body, ticks_ms)
        self._probe_addr = {}  # serial bytes -> (addr_type, addr bytes)
        self._adv_index = 0
        self._scanning = False
        # Central role (connect to a probe) - the #2 connection proxy.
        self.connect_enabled = False
        self.central_state = "idle"   # idle/connecting/discovering/ready
        self._central_conn = None
        self._central_action = None
        self._central_svc = None      # (start, end) handle range
        self._probe_tx = None         # notify (TX) value handle on the probe
        self._probe_rx = None         # write (RX) value handle on the probe
        self._probe_cccd = None
        self._central_target = None   # (addr_type, addr) to connect to
        self._central_serial = None   # serial bytes of the connected probe
        self._connect_started = None  # ticks_ms when the connect attempt began
        self.probe_frames = []        # frames received from the probe
        self.probe_frame_count = 0
        self._advertise()

    # -- central role (connect to a probe) -----------------------------------

    def set_connect(self, enabled):
        self.connect_enabled = enabled
        self._update_scan()   # scanning finds the probe's address
        if not enabled and self._central_conn is not None:
            try:
                self.ble.gap_disconnect(self._central_conn)
            except OSError:
                pass

    def _maybe_connect_probe(self):
        """If we know a probe's address and aren't connected, start connecting."""
        if (not self.connect_enabled or self._central_conn is not None
                or self.central_state != "idle" or self._central_action):
            return
        for serial, target in self._probe_addr.items():
            self._central_target = target
            self._central_serial = serial
            self._central_action = "connect"
            return

    # -- MeatNet relay -------------------------------------------------------

    def _update_scan(self):
        # Scan whenever relaying or proxying - independent of advertising.
        if self.relay_enabled or self.connect_enabled:
            self._start_scan()
        else:
            self._stop_scan()

    def set_relay(self, enabled):
        if enabled == self.relay_enabled:
            return
        self.relay_enabled = enabled
        if not enabled:
            self.repeated = {}
        self._update_scan()

    def _start_scan(self):
        if self._scanning:
            return
        try:
            # Continuous passive scan at ~50% duty so advertising, the GATT
            # connection and WiFi still get radio time.
            self.ble.gap_scan(0, 60000, 30000, False)
            self._scanning = True
        except OSError:
            pass

    def _stop_scan(self):
        if not self._scanning:
            return
        try:
            self.ble.gap_scan(None)
        except OSError:
            pass
        self._scanning = False

    def _scan_ingest(self, payload, addr_type=None, addr=None):
        """Parse a scanned advertisement; store direct-probe (type 1) data."""
        i = 0
        n = len(payload)
        while i + 1 < n:
            length = payload[i]
            if length == 0:
                break
            ad_type = payload[i + 1]
            if ad_type == 0xFF and length >= 4:
                ad = payload[i + 2:i + 1 + length]
                if ad[:2] == _VENDOR_LE:
                    body = ad[2:]
                    if body and body[0] == _PRODUCT_TYPE_PROBE and len(body) >= 5:
                        serial = bytes(body[1:5])
                        self.repeated[serial] = (bytes(body), time.ticks_ms())
                        if addr is not None:
                            self._probe_addr[serial] = (addr_type, bytes(addr))
            i += 1 + length

    def _repeat_payload(self, body):
        """Turn a direct-probe body into a repeated-node advertisement."""
        b = bytearray(body)
        b[0] = _PRODUCT_TYPE_NODE       # mark as repeated so app reads hop count
        if len(b) >= 21:
            b[20] = 0x00                # network info: 1 hop (value 0)
        return _VENDOR_LE + bytes(b)

    def relayed_serials(self):
        out = []
        for s in self.repeated:
            # serial is little-endian uint32; render as the app does (hex)
            out.append("%08X" % int.from_bytes(s, "little"))
        return out

    def _go_silent(self):
        """Stop advertising and drop any connected central."""
        try:
            self.ble.gap_advertise(None)
        except OSError:
            pass
        for conn_handle in list(self.connections):
            try:
                self.ble.gap_disconnect(conn_handle)
            except OSError:
                pass
        self._advertising = False
        self._last_adv = None

    # -- advertising ---------------------------------------------------------

    def _set_adv(self, msd):
        """Advertise the given manufacturer-specific data (with company ID)."""
        adv = _FLAGS_AD + bytes((len(msd) + 1, 0xFF)) + msd
        # Scan response: 128-bit DFU UUID (18 B) + complete local name. Every
        # advertisement (incl. relayed probes) needs the DFU UUID so it passes
        # the Combustion app's scan filter.
        name = self.gauge.serial.encode()[:11]
        resp = (bytes((17, 0x07)) + protocol.DFU_SERVICE_UUID_LE
                + bytes((len(name) + 1, 0x09)) + name)
        self.ble.gap_advertise(_ADV_INTERVAL_US, adv_data=adv,
                               resp_data=resp, connectable=True)
        self._last_adv = msd

    def _advertise(self):
        self._set_adv(self.gauge.advertisement(include_vendor_id=True))

    def rotate_advertisement(self):
        """Advertise our own gauge data (when emit_own), interleaved with any
        relayed probes.

        emit_own is gated by the oven transmit option; the relayed probes are
        NOT - so #1 relay keeps working when the oven is off. When there is
        nothing to advertise at all, the radio goes silent.
        """
        own = self.gauge.advertisement(include_vendor_id=True)
        payloads = []
        if self.emit_own:
            payloads.append(own)
        if self.relay_enabled:
            now = time.ticks_ms()
            for serial in list(self.repeated):
                body, ts = self.repeated[serial]
                if time.ticks_diff(now, ts) > _RELAY_EXPIRE_MS:
                    del self.repeated[serial]
                    continue
                payloads.append(self._repeat_payload(body))
        if not payloads:
            if self._advertising:
                self._go_silent()
            return
        self._advertising = True
        try:
            if len(payloads) == 1:
                if payloads[0] != self._last_adv:
                    self._set_adv(payloads[0])
            else:
                self._adv_index = (self._adv_index + 1) % len(payloads)
                self._set_adv(payloads[self._adv_index])
        except OSError:
            self._last_adv = payloads[0]  # can't re-advertise while connected

    # Backwards-compatible alias
    def refresh_advertisement(self):
        self.rotate_advertisement()

    # -- events ----------------------------------------------------------------

    def _irq(self, event, data):
        # Keep this handler minimal: record state and defer all BLE stack
        # calls to service() on the main loop. Calling gap/gatts APIs here
        # panics the interrupt watchdog when WiFi is also running.
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self.connections.add(conn_handle)
            self.conn_mtu[conn_handle] = 23
            self._pending_mtu.append(conn_handle)
            self._need_advertise = True
        elif event == _IRQ_SCAN_RESULT:
            # (addr_type, addr, adv_type, rssi, adv_data). Parse bytes only -
            # no BLE stack calls in the IRQ.
            addr_type, addr, _, _, adv_data = data
            self._scan_ingest(bytes(adv_data), addr_type, bytes(addr))
        elif event == _IRQ_SCAN_DONE:
            # The scan stopped (often because gap_connect pre-empted it).
            # service() re-arms it if relaying/proxying is still enabled.
            self._scanning = False
        elif event == _IRQ_PERIPHERAL_CONNECT:
            conn_handle, _, _ = data
            self._central_conn = conn_handle
            self._connect_started = None
            self.central_state = "discovering"
            self._central_action = "discover_svc"
        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            conn_handle, _, _ = data
            if conn_handle == self._central_conn:
                self._central_conn = None
                self.central_state = "idle"
                self._probe_tx = self._probe_rx = self._probe_cccd = None
        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start, end, uuid = data
            if conn_handle == self._central_conn and \
                    uuid == _uuid128(_PROBE_STATUS_SVC):
                self._central_svc = (start, end)
        elif event == _IRQ_GATTC_SERVICE_DONE:
            if self._central_svc:
                self._central_action = "discover_char"
        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, end_handle, value_handle, properties, uuid = data
            if conn_handle == self._central_conn:
                if uuid == _uuid128(_PROBE_STATUS_CHAR):
                    self._probe_tx = value_handle  # notify source
        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            if self._probe_tx is not None:
                self._central_action = "discover_desc"
        elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
            conn_handle, dsc_handle, uuid = data
            # CCCD (0x2902) above the TX characteristic value handle.
            if (conn_handle == self._central_conn and self._probe_cccd is None
                    and self._probe_tx is not None
                    and dsc_handle > self._probe_tx
                    and uuid == bluetooth.UUID(0x2902)):
                self._probe_cccd = dsc_handle
        elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
            self._central_action = "enable_notify"
        elif event == _IRQ_GATTC_NOTIFY:
            conn_handle, value_handle, notify_data = data
            if conn_handle == self._central_conn:
                self.probe_frames.append(bytes(notify_data))
                self.probe_frame_count += 1
                if len(self.probe_frames) > 8:
                    self.probe_frames.pop(0)
        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data
            self.conn_mtu[conn_handle] = mtu
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self.connections.discard(conn_handle)
            self.conn_mtu.pop(conn_handle, None)
            self._need_advertise = True
        elif event == _IRQ_GATTS_WRITE:
            conn_handle, attr_handle = data
            if attr_handle == self._rx_handle:
                self.rx_queue.append(self.ble.gatts_read(self._rx_handle))

    def service(self):
        """Run deferred BLE control work. Call from the main asyncio loop."""
        while self._pending_mtu:
            conn_handle = self._pending_mtu.pop()
            # Real gauges initiate the ATT MTU exchange; the Combustion app
            # never does, so without this the 44-byte status frames truncate.
            try:
                self.ble.gattc_exchange_mtu(conn_handle)
            except OSError:
                pass
        if self._need_advertise:
            self._need_advertise = False
            # Re-establish advertising after a connect/disconnect.
            self.rotate_advertisement()
        # Re-arm the scan if it should be running but was stopped (e.g. a
        # gap_connect pre-empted it). Not during an active connect attempt.
        if ((self.relay_enabled or self.connect_enabled) and not self._scanning
                and self.central_state != "connecting"):
            self._start_scan()
        self._service_central()

    def _service_central(self):
        """Drive the deferred central-role (connect-to-probe) state machine."""
        # Abandon a connection attempt that never completes (e.g. the probe
        # is still holding a stale link) and retry.
        if (self.central_state == "connecting" and self._connect_started and
                time.ticks_diff(time.ticks_ms(), self._connect_started) > 12000):
            try:
                self.ble.gap_connect(None)  # cancel outstanding attempt
            except OSError:
                pass
            self.central_state = "idle"
            self._central_conn = None
            self._connect_started = None
        if self.connect_enabled:
            self._maybe_connect_probe()
        action = self._central_action
        if not action:
            return
        self._central_action = None
        try:
            if action == "connect":
                addr_type, addr = self._central_target
                self.central_state = "connecting"
                self._connect_started = time.ticks_ms()
                self.ble.gap_connect(addr_type, addr)
            elif action == "discover_svc":
                self.ble.gattc_discover_services(
                    self._central_conn, _uuid128(_PROBE_STATUS_SVC))
            elif action == "discover_char":
                start, end = self._central_svc
                self.ble.gattc_discover_characteristics(
                    self._central_conn, start, end)
            elif action == "discover_desc":
                start, end = self._central_svc
                self.ble.gattc_discover_descriptors(
                    self._central_conn, self._probe_tx, end)
            elif action == "enable_notify":
                if self._probe_cccd is not None:
                    self.ble.gattc_write(self._central_conn, self._probe_cccd,
                                         b"\x01\x00", 1)
                self.central_state = "ready"
        except OSError as exc:
            self.central_state = "error: %s" % exc

    def probe_send(self, frame):
        """Write a UART frame to the connected probe (RX characteristic)."""
        if self._central_conn is None or self._probe_rx is None:
            return False
        try:
            self.ble.gattc_write(self._central_conn, self._probe_rx, frame, 1)
            return True
        except OSError:
            return False

    def poll_probe_frames(self):
        frames, self.probe_frames = self.probe_frames, []
        return frames

    def connected_probe_serials(self):
        """Serials (4 bytes LE) of probes we have a live central link to."""
        if self.central_state == "ready" and self._central_serial:
            return [self._central_serial]
        return []

    def latest_probe_status(self):
        """(serial_bytes, 94-byte status) for the connected probe, or None."""
        if (self.central_state == "ready" and self._central_serial
                and self.probe_frames and len(self.probe_frames[-1]) == 94):
            return self._central_serial, self.probe_frames[-1]
        return None

    # -- I/O ----------------------------------------------------------------

    def poll_rx(self):
        """Drain queued RX writes; returns list of byte strings."""
        queue, self.rx_queue = self.rx_queue, []
        return queue

    def notify(self, frame):
        """Send a frame on TX to centrals whose MTU can carry it whole.

        Sending into a too-small MTU would truncate the frame and the
        Combustion app rejects (and used to choke on) partial frames.
        """
        sent = False
        for conn_handle in list(self.connections):
            if self.conn_mtu.get(conn_handle, 23) < len(frame) + 3:
                continue
            try:
                self.ble.gatts_notify(conn_handle, self._tx_handle, frame)
                sent = True
            except OSError:
                self.connections.discard(conn_handle)
        return sent
