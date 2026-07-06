"""Combustion Gauge BLE protocol (MicroPython) - Anova Oven to Combustion Relay.

Wire formats for the Combustion BLE protocol, written for MicroPython
(no dataclasses/typing/enum).
"""

import random
import struct

VENDOR_ID = 0x09C7
PRODUCT_TYPE_GAUGE = 3

UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
# Nordic Secure DFU service (0xFE59) as 128-bit little-endian bytes,
# advertised in the scan response - this is what the Combustion apps
# filter their scans on.
DFU_SERVICE_UUID_LE = bytes((
    0xFB, 0x34, 0x9B, 0x5F, 0x80, 0x00, 0x00, 0x80,
    0x00, 0x10, 0x00, 0x00, 0x59, 0xFE, 0x00, 0x00))

SYNC_BYTES = b"\xCA\xFE"
MSG_GAUGE_STATUS = 0x60
MSG_SET_GAUGE_HIGH_LOW_ALARM = 0x61
MSG_READ_GAUGE_LOGS = 0x62
# MeatNet node messages used by the #2 probe relay
MSG_READ_NODE_LIST = 0x42
MSG_READ_NETWORK_TOPOLOGY = 0x43
MSG_READ_PROBE_LIST = 0x44
MSG_PROBE_STATUS = 0x45
RESPONSE_FLAG = 0x80

MIN_TEMP_C = -20.0
MAX_TEMP_C = 799.0


def encode_raw_temperature(celsius):
    celsius = max(MIN_TEMP_C, min(MAX_TEMP_C, celsius))
    return round((celsius + 20.0) / 0.1) & 0x1FFF


def decode_raw_temperature(raw):
    return (raw & 0x1FFF) * 0.1 - 20.0


def encode_status_flags(sensor_present, sensor_overheating, low_battery):
    flags = 0
    if sensor_present:
        flags |= 0x01
    if sensor_overheating:
        flags |= 0x02
    if low_battery:
        flags |= 0x04
    return flags


class AlarmStatus:
    def __init__(self, set_=False, tripped=False, alarming=False,
                 temperature_c=0.0):
        self.set = set_
        self.tripped = tripped
        self.alarming = alarming
        self.temperature_c = temperature_c

    def encode(self):
        value = 0
        if self.set:
            value |= 0x0001
        if self.tripped:
            value |= 0x0002
        if self.alarming:
            value |= 0x0004
        value |= (encode_raw_temperature(self.temperature_c) & 0x1FFF) << 3
        return value

    @classmethod
    def decode(cls, value):
        return cls(
            set_=bool(value & 0x0001),
            tripped=bool(value & 0x0002),
            alarming=bool(value & 0x0004),
            temperature_c=decode_raw_temperature((value >> 3) & 0x1FFF),
        )

    def __str__(self):
        if not self.set:
            return "not set"
        state = ""
        if self.tripped:
            state += " TRIPPED"
        if self.alarming:
            state += " ALARMING"
        return "%.1fC%s" % (self.temperature_c, state or " armed")


def encode_high_low_alarm_status(high, low):
    return (high.encode() & 0xFFFF) | ((low.encode() & 0xFFFF) << 16)


def encode_network_information(hop_count=1):
    return (hop_count - 1) & 0x03


def encode_serial(serial):
    data = serial.encode()[:10]
    return data + b"\x00" * (10 - len(data))


def build_gauge_advertisement(serial, temperature_c, sensor_present,
                              sensor_overheating, low_battery,
                              high_alarm, low_alarm,
                              high_radio_power=False,
                              include_vendor_id=True):
    raw_temp = encode_raw_temperature(temperature_c) if sensor_present else 0
    body = struct.pack(
        "<B10sHBBIB3s",
        PRODUCT_TYPE_GAUGE,
        encode_serial(serial),
        raw_temp,
        encode_status_flags(sensor_present, sensor_overheating, low_battery),
        0,
        encode_high_low_alarm_status(high_alarm, low_alarm),
        0x01 if high_radio_power else 0x00,
        b"\x00\x00\x00",
    )
    if include_vendor_id:
        return struct.pack("<H", VENDOR_ID) + body
    return body


def crc16_ccitt(data, initial=0xFFFF):
    crc = initial
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def new_message_id():
    return random.getrandbits(32)


def build_request(message_type, payload, request_id=None):
    if request_id is None:
        request_id = new_message_id()
    crc_region = struct.pack("<BIB", message_type & 0x7F, request_id,
                             len(payload)) + payload
    return SYNC_BYTES + struct.pack("<H", crc16_ccitt(crc_region)) + crc_region


def build_response(message_type, request_id, success, payload,
                   response_id=None):
    if response_id is None:
        response_id = new_message_id()
    crc_region = struct.pack(
        "<BIIBB", (message_type & 0x7F) | RESPONSE_FLAG, request_id,
        response_id, 1 if success else 0, len(payload)) + payload
    return SYNC_BYTES + struct.pack("<H", crc16_ccitt(crc_region)) + crc_region


def parse_requests(data):
    """Parse request frames -> list of (message_type, request_id, payload)."""
    requests = []
    i = 0
    while i + 10 <= len(data):
        if data[i:i + 2] != SYNC_BYTES:
            i += 1
            continue
        crc = struct.unpack_from("<H", data, i + 2)[0]
        message_type = data[i + 4]
        if message_type & RESPONSE_FLAG:
            if i + 15 > len(data):
                break
            i += 15 + data[i + 14]
            continue
        request_id = struct.unpack_from("<I", data, i + 5)[0]
        length = data[i + 9]
        end = i + 10 + length
        if end > len(data):
            break
        payload = bytes(data[i + 10:end])
        if crc16_ccitt(bytes(data[i + 4:end])) == crc:
            requests.append((message_type, request_id, payload))
        i = end
    return requests


def build_gauge_status_notification(serial, session_id, sample_period_ms,
                                    temperature_c, sensor_present,
                                    sensor_overheating, low_battery,
                                    log_min, log_max, high_alarm, low_alarm,
                                    new_record, hop_count=1):
    raw_temp = encode_raw_temperature(temperature_c) if sensor_present else 0
    payload = struct.pack(
        "<10sIHHBIIBIBB",
        encode_serial(serial),
        session_id,
        sample_period_ms,
        raw_temp,
        encode_status_flags(sensor_present, sensor_overheating, low_battery),
        log_min,
        log_max,
        0,
        encode_high_low_alarm_status(high_alarm, low_alarm),
        1 if new_record else 0,
        encode_network_information(hop_count),
    )
    return build_request(MSG_GAUGE_STATUS, payload)


def parse_set_high_low_alarm(payload):
    serial, high_raw, low_raw = struct.unpack("<10sHH", payload[:14])
    return serial, AlarmStatus.decode(high_raw), AlarmStatus.decode(low_raw)


def parse_read_gauge_logs(payload):
    serial, start, end = struct.unpack("<10sII", payload[:18])
    return serial, start, end


def build_node_probe_status(serial_bytes, status94, network_info=0):
    """Wrap a 94-byte probe Probe-Status characteristic value into a MeatNet
    Node Probe Status (0x45) notification.

    Node 0x45 payload = Serial(4) + [LogRange..FoodSafeStatus (48)] +
    NetworkInfo(1) + [Overheating..LowAlarm (46)] = 99 bytes.
    """
    payload = (serial_bytes[:4] + status94[0:48] + bytes([network_info])
               + status94[48:94])
    return build_request(MSG_PROBE_STATUS, payload)


def build_read_probe_list_response(request_id, probe_serials):
    """Response to Read Probe List (0x44): 10 (device#, serial) entries."""
    payload = b""
    for i in range(10):
        serial = probe_serials[i] if i < len(probe_serials) else b"\x00\x00\x00\x00"
        payload += bytes([i + 1]) + serial[:4]
    return build_response(MSG_READ_PROBE_LIST, request_id, True, payload)


def build_read_logs_response(serial, request_id, sequence, temperature_c,
                             sensor_present):
    payload = struct.pack(
        "<10sIHB",
        encode_serial(serial),
        sequence,
        encode_raw_temperature(temperature_c) if sensor_present else 0,
        1 if sensor_present else 0,
    )
    return build_response(MSG_READ_GAUGE_LOGS, request_id, True, payload)
