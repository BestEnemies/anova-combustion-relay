"""Gauge emulator state machine - MicroPython port (transport-agnostic)."""

import random
import time

import protocol
from protocol import AlarmStatus


class GaugeEmulator:
    def __init__(self, serial="RELAY00001", temperature_c=22.0,
                 sample_period_ms=5000):
        self.serial = serial
        self.temperature_c = temperature_c
        self.sensor_present = True
        self.sensor_overheating = False
        self.low_battery = False
        self.high_radio_power = False
        self.sample_period_ms = sample_period_ms
        self.high_alarm = AlarmStatus()
        self.low_alarm = AlarmStatus()
        self.hop_count = 1
        self.session_id = random.getrandbits(32)
        self.logs = []  # list of (sequence, temperature_c, sensor_present)
        self._last_log_ticks = None
        self._new_record = False

    def set_temperature(self, celsius):
        self.temperature_c = max(protocol.MIN_TEMP_C,
                                 min(protocol.MAX_TEMP_C, celsius))
        self._evaluate_alarms()

    def _evaluate_alarms(self):
        for alarm, is_high in ((self.high_alarm, True), (self.low_alarm, False)):
            if not alarm.set:
                continue
            if is_high:
                tripped = (self.sensor_present
                           and self.temperature_c >= alarm.temperature_c)
            else:
                tripped = (self.sensor_present
                           and self.temperature_c <= alarm.temperature_c)
            if tripped and not alarm.tripped:
                alarm.alarming = True
            alarm.tripped = tripped
            if not tripped:
                alarm.alarming = False

    def tick(self):
        now = time.ticks_ms()
        if (self._last_log_ticks is None or
                time.ticks_diff(now, self._last_log_ticks) >= self.sample_period_ms):
            self._last_log_ticks = now
            self.logs.append((
                len(self.logs),
                self.temperature_c if self.sensor_present else 0.0,
                self.sensor_present,
            ))
            # Cap memory on-device: keep the newest 2000 records
            if len(self.logs) > 2000:
                self.logs.pop(0)
            self._new_record = True
        self._evaluate_alarms()

    def advertisement(self, include_vendor_id=True):
        return protocol.build_gauge_advertisement(
            self.serial, self.temperature_c, self.sensor_present,
            self.sensor_overheating, self.low_battery,
            self.high_alarm, self.low_alarm,
            high_radio_power=self.high_radio_power,
            include_vendor_id=include_vendor_id)

    def status_notification(self):
        log_max = len(self.logs) - 1 if self.logs else 0
        log_min = self.logs[0][0] if self.logs else 0
        frame = protocol.build_gauge_status_notification(
            self.serial, self.session_id, self.sample_period_ms,
            self.temperature_c, self.sensor_present,
            self.sensor_overheating, self.low_battery,
            log_min, max(log_max, log_min),
            self.high_alarm, self.low_alarm,
            self._new_record, self.hop_count)
        self._new_record = False
        return frame

    def handle_uart_write(self, data):
        replies = []
        for message_type, request_id, payload in protocol.parse_requests(data):
            replies.extend(self._handle_request(message_type, request_id,
                                                payload))
        return replies

    def _handle_request(self, message_type, request_id, payload):
        if message_type == protocol.MSG_SET_GAUGE_HIGH_LOW_ALARM:
            _, high, low = protocol.parse_set_high_low_alarm(payload)
            high.tripped = False
            low.tripped = False
            self.high_alarm = high
            self.low_alarm = low
            self._evaluate_alarms()
            return [protocol.build_response(message_type, request_id, True,
                                            b"")]
        if message_type == 0x30:  # GET_FEATURE_FLAGS
            # 10-byte node serial + 4 flag bytes (bit 0 of first = WiFi: no)
            payload_out = protocol.encode_serial(self.serial) + b"\x00\x00\x00\x00"
            return [protocol.build_response(message_type, request_id, True,
                                            payload_out)]
        if message_type == protocol.MSG_READ_GAUGE_LOGS:
            _, start, end = protocol.parse_read_gauge_logs(payload)
            replies = []
            for sequence, temp, present in self.logs:
                if start <= sequence <= end:
                    replies.append(protocol.build_read_logs_response(
                        self.serial, request_id, sequence, temp, present))
            return replies
        return [protocol.build_response(message_type, request_id, False, b"")]

    def summary(self):
        f = self.temperature_c * 9 / 5 + 32
        return (
            "Serial:       %s\n"
            "Temperature:  %.1fC / %.1fF%s\n"
            "Flags:        sensor=%s overheat=%s lowbatt=%s\n"
            "High alarm:   %s\n"
            "Low alarm:    %s\n"
            "Logs:         %d records (period %d ms)\n"
            "Session:      0x%08X" % (
                self.serial, self.temperature_c, f,
                "" if self.sensor_present else "  (sensor disconnected)",
                self.sensor_present, self.sensor_overheating,
                self.low_battery, self.high_alarm, self.low_alarm,
                len(self.logs), self.sample_period_ms, self.session_id))
