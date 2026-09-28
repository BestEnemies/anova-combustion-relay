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
from central import CentralLink

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
_IRQ_GATTC_WRITE_DONE = 17
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
        self.client_addr = {}  # conn_handle -> peer address (diagnostics)
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
        # Outbound (central-role) links. The probe link is the #2 connect
        # proxy (Probe Status service); the engine link talks MeatNet UART to
        # a Combustion Engine to change its set point.
        self.connect_enabled = False
        self.probe_link = CentralLink(self.ble, "probe", _PROBE_STATUS_SVC,
                                      _PROBE_STATUS_CHAR, keep=8)
        self.engine_link = CentralLink(self.ble, "engine",
                                       protocol.UART_SERVICE_UUID,
                                       protocol.UART_TX_CHAR_UUID,
                                       protocol.UART_RX_CHAR_UUID, keep=64,
                                       write_mode=0)
        self.links = (self.probe_link, self.engine_link)
        # Passive-scan observations, stored raw in the IRQ, decoded on demand.
        self.scan_for_engine = False
        self.engines = {}     # serial10 -> (body, ticks, addr_type, addr, rssi)
        self.probe_raw = {}   # serial4 -> (body, ticks, product type)
        self.mtu_events = []  # diagnostics: (conn, mtu, link name)
        self._advertise()

    # -- central links ------------------------------------------------------

    @property
    def central_state(self):
        return self.probe_link.state

    @property
    def probe_frame_count(self):
        return self.probe_link.count

    @property
    def probe_frames(self):
        return self.probe_link.frames

    def set_connect(self, enabled):
        self.connect_enabled = enabled
        self.probe_link.enabled = enabled
        self._update_scan()   # scanning finds the probe's address

    def set_engine_scan(self, enabled):
        self.scan_for_engine = enabled
        self._update_scan()

    def _link_for_conn(self, conn):
        for link in self.links:
            if link.conn == conn:
                return link
        return None

    def _link_connecting(self, addr):
        for link in self.links:
            if (link.state == "connecting" and link.target
                    and link.target[1] == addr):
                return link
        for link in self.links:
            if link.state == "connecting":
                return link
        return None

    # -- MeatNet relay -------------------------------------------------------

    def _scan_wanted(self):
        return self.relay_enabled or self.connect_enabled or self.scan_for_engine

    def _update_scan(self):
        # Scan whenever relaying, proxying or managing an Engine - independent
        # of advertising.
        if self._scan_wanted():
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

    def _scan_ingest(self, payload, addr_type=None, addr=None, rssi=0):
        """Parse a scanned advertisement and record Combustion devices.

        Runs in the IRQ, so it only slices bytes and stores them; decoding
        happens later on the main loop.
        """
        i = 0
        n = len(payload)
        while i + 1 < n:
            length = payload[i]
            if length == 0:
                break
            ad_type = payload[i + 1]
            if ad_type == 0xFF and length >= 4:
                ad = payload[i + 2:i + 1 + length]
                if ad[:2] == _VENDOR_LE and len(ad) > 2:
                    body = bytes(ad[2:])
                    ptype = body[0]
                    now = time.ticks_ms()
                    if ptype == _PRODUCT_TYPE_PROBE and len(body) >= 5:
                        serial = body[1:5]
                        self.repeated[serial] = (body, now)
                        if addr is not None:
                            self._probe_addr[serial] = (addr_type, bytes(addr))
                    # Probe readings, direct (1) or repeated by a node (2).
                    if ptype in (1, 2) and len(body) >= 20:
                        serial = body[1:5]
                        if serial != b"\x00\x00\x00\x00":
                            self.probe_raw[serial] = (body, now, ptype)
                    elif ptype == protocol.PRODUCT_TYPE_ENGINE and len(body) >= 15 \
                            and addr is not None:
                        self.engines[body[1:11]] = (body, now, addr_type,
                                                    bytes(addr), rssi)
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
            conn_handle, _, addr = data
            self.connections.add(conn_handle)
            self.client_addr[conn_handle] = bytes(addr)
            self.conn_mtu[conn_handle] = 23
            self._pending_mtu.append(conn_handle)
            self._need_advertise = True
        elif event == _IRQ_SCAN_RESULT:
            # (addr_type, addr, adv_type, rssi, adv_data). Parse bytes only -
            # no BLE stack calls in the IRQ.
            addr_type, addr, _, rssi, adv_data = data
            self._scan_ingest(bytes(adv_data), addr_type, bytes(addr), rssi)
        elif event == _IRQ_SCAN_DONE:
            # The scan stopped (often because gap_connect pre-empted it).
            # service() re-arms it if it is still wanted.
            self._scanning = False
        elif event == _IRQ_PERIPHERAL_CONNECT:
            conn_handle, _, addr = data
            link = self._link_connecting(bytes(addr))
            if link:
                link.on_connect(conn_handle)
                # The peer often starts the MTU exchange straight away, and
                # its event can arrive before this one - apply it now.
                mtu = self.conn_mtu.pop(conn_handle, None)
                if mtu:
                    link.on_mtu(mtu)
        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            self.conn_mtu.pop(data[0], None)
            link = self._link_for_conn(data[0])
            if link:
                link.on_disconnect()
        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start, end, uuid = data
            link = self._link_for_conn(conn_handle)
            if link:
                link.on_service(start, end, uuid)
        elif event == _IRQ_GATTC_SERVICE_DONE:
            link = self._link_for_conn(data[0])
            if link:
                link.on_service_done()
        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, _, value_handle, _, uuid = data
            link = self._link_for_conn(conn_handle)
            if link:
                link.on_char(value_handle, uuid)
        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            link = self._link_for_conn(data[0])
            if link:
                link.on_char_done()
        elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
            conn_handle, dsc_handle, uuid = data
            link = self._link_for_conn(conn_handle)
            if link:
                link.on_desc(dsc_handle, uuid)
        elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
            link = self._link_for_conn(data[0])
            if link:
                link.on_desc_done()
        elif event == _IRQ_GATTC_WRITE_DONE:
            link = self._link_for_conn(data[0])
            if link:
                link.on_write_done()
        elif event == _IRQ_GATTC_NOTIFY:
            conn_handle, _, notify_data = data
            link = self._link_for_conn(conn_handle)
            if link:
                link.on_notify(notify_data)
        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data
            link = self._link_for_conn(conn_handle)
            self.mtu_events.append((conn_handle, mtu, link.name if link else None))
            if len(self.mtu_events) > 8:
                self.mtu_events.pop(0)
            if link:
                link.on_mtu(mtu)
            else:
                self.conn_mtu[conn_handle] = mtu
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self.connections.discard(conn_handle)
            self.client_addr.pop(conn_handle, None)
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
        # #2 connect proxy: pick a probe we've heard advertising directly.
        link = self.probe_link
        if (self.connect_enabled and link.state == "idle" and link.conn is None
                and link.action is None):
            for serial, (addr_type, addr) in self._probe_addr.items():
                if link.target != (addr_type, addr):
                    link.set_target(addr_type, addr, serial)
                break
        # Only one outstanding gap_connect at a time across all links.
        may_connect = not any(l.state == "connecting" for l in self.links)
        for link in self.links:
            link.service(may_connect)
            if link.state == "connecting":
                may_connect = False
        # Re-arm the scan if it should be running but was stopped (e.g. a
        # gap_connect pre-empted it). Not during an active connect attempt.
        if self._scan_wanted() and not self._scanning and may_connect:
            self._start_scan()

    def connected_probe_serials(self):
        """Serials (4 bytes LE) of probes we have a live central link to."""
        if self.probe_link.ready and self.probe_link.key:
            return [self.probe_link.key]
        return []

    def latest_probe_status(self):
        """(serial_bytes, 94-byte status) for the connected probe, or None."""
        link = self.probe_link
        if (link.ready and link.key and link.frames
                and len(link.frames[-1]) == 94):
            return link.key, link.frames[-1]
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
