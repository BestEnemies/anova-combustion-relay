"""Anova Precision Oven cloud client for MicroPython (ESP32).

Uses the official Anova Developer API: a long-lived Personal Access Token
(PAT) authenticates directly on the WebSocket - no Firebase token refresh.

Runs in its own thread (blocking TLS) so the BLE gauge keeps running on the
main asyncio loop. Flow:
  1. Open a WebSocket to wss://devices.anovaculinary.io with the PAT as the
     `token` query parameter.
  2. Read EVENT_APO_STATE messages; extract the wet bulb (sous-vide) or dry
     bulb temperature. Never the food probe.
  3. Call on_temp(celsius, mode, oven_on) for each reading. Reconnect on error.

To keep RAM low we don't json-parse the multi-KB state message - we
string-search for the temperature field.
"""

import socket
import ssl
import time
import binascii
import gc

try:
    import urandom as random
except ImportError:
    import random

WS_HOST = "devices.anovaculinary.io"


def _tls_connect(host, port=443, timeout=20):
    # Free as much contiguous heap as possible: the TLS/RSA handshake needs a
    # big allocation and fails (MPI_ALLOC_FAILED) when BLE + WiFi are both up.
    gc.collect()
    ai = socket.getaddrinfo(host, port)[0][-1]
    sock = socket.socket()
    sock.settimeout(timeout)
    sock.connect(ai)
    try:
        return ssl.wrap_socket(sock, server_hostname=host)
    except TypeError:
        # Older API without SNI kwarg
        return ssl.wrap_socket(sock)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.read(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return bytes(buf)


def _ws_handshake(sock, path):
    key = binascii.b2a_base64(bytes(random.getrandbits(8) for _ in range(16)))
    key = key.strip().decode()
    req = (
        "GET " + path + " HTTP/1.1\r\n"
        "Host: " + WS_HOST + "\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: " + key + "\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: ANOVA_V2\r\n\r\n")
    sock.write(req.encode())
    # Read headers up to blank line
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.read(1)
        if not chunk:
            raise OSError("ws handshake closed")
        data += chunk
    if b" 101 " not in data[:20] + data:
        raise OSError("ws upgrade failed: " + str(bytes(data[:64])))


def _ws_send(sock, opcode, payload=b""):
    # Client frames must be masked (RFC 6455).
    b1 = 0x80 | opcode
    length = len(payload)
    header = bytearray([b1])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header += bytes([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
    else:
        header.append(0x80 | 127)
        header += bytes((length >> (8 * i)) & 0xFF for i in range(7, -1, -1))
    mask = bytes(random.getrandbits(8) for _ in range(4))
    header += mask
    masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
    sock.write(bytes(header) + masked)


def _ws_recv(sock):
    """Read one full (possibly fragmented) text message; return bytes or None
    for a control frame handled internally."""
    message = bytearray()
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(_recv_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(_recv_exact(sock, 8), "big")
        payload = _recv_exact(sock, length) if length else b""
        if opcode == 0x8:  # close
            raise OSError("ws closed by server")
        if opcode == 0x9:  # ping -> pong
            _ws_send(sock, 0xA, payload)
            continue
        if opcode == 0xA:  # pong
            continue
        message += payload
        if fin:
            return bytes(message)


def _bulb_celsius(text, i, key):
    """Read temperatureBulbs.<key>.current.celsius, searching from index i."""
    j = text.find(key, i)
    if j < 0:
        return None
    k = text.find(b'"current"', j)
    m = text.find(b'"celsius"', k)
    if m < 0:
        return None
    start = m + len(b'"celsius"')
    while start < len(text) and text[start] in b': ':
        start += 1
    end = start
    while end < len(text) and text[end] in b'-0123456789.':
        end += 1
    try:
        return float(text[start:end])
    except ValueError:
        return None


def _oven_on(text):
    """Is the oven actively running? state.state.mode != 'idle'.
    The operating mode is the "mode" just before "processedCommandIds"."""
    i = text.find(b'"processedCommandIds"')
    if i < 0:
        i = text.find(b'"temperatureUnit"')
    if i < 0:
        return None
    m = text.rfind(b'"mode"', 0, i)
    if m < 0:
        return None
    c = text.find(b':', m)
    q1 = text.find(b'"', c + 1)
    q2 = text.find(b'"', q1 + 1)
    if q1 < 0 or q2 < 0:
        return None
    return text[q1 + 1:q2] != b'idle'


def extract_celsius(text):
    """Find the bulb temperature to show on the gauge in a raw APO_STATE.

    Uses the wet bulb in sous-vide (wet) mode, else the dry bulb. Never the
    food probe. Returns (celsius, bulb_mode, oven_on).
    """
    oven_on = _oven_on(text)
    if oven_on is None:
        oven_on = False
    i = text.find(b'"temperatureBulbs"')
    if i < 0:
        return None, "dry", oven_on
    # The temperatureBulbs object carries its own "mode" ("dry"/"wet"); it is
    # the first "mode" after the object start (the dry/wet sub-objects have none).
    m = text.find(b'"mode"', i)
    mode = "wet" if (0 <= m and b'wet' in text[m:m + 16]) else "dry"
    key = b'"wet"' if mode == "wet" else b'"dry"'
    return _bulb_celsius(text, i, key), mode, oven_on


def run_forever(pat, on_temp, on_status=None, supported="APO"):
    """Connect and stream oven temperatures forever, reconnecting on error.

    pat is the Anova Personal Access Token (the long-lived "anova-..." string).
    on_temp(celsius, bulb_mode, oven_on) is called for each state message.
    on_status(text) receives human-readable status/errors (optional).
    """
    def status(msg):
        if on_status:
            on_status(msg)

    path = ("/?token=" + pat + "&supportedAccessories=" + supported +
            "&platform=android")
    while True:
        try:
            gc.collect()
            status("connecting (heap %d)" % gc.mem_free())
            # 90 s read timeout: the idle oven emits state every 30 s.
            sock = _tls_connect(WS_HOST, timeout=90)
            try:
                _ws_handshake(sock, path)
                status("connected")
                while True:
                    msg = _ws_recv(sock)
                    if b"EVENT_APO_STATE" in msg:
                        celsius, mode, oven_on = extract_celsius(msg)
                        if celsius is not None:
                            on_temp(celsius, mode, oven_on)
            finally:
                sock.close()
        except Exception as exc:
            status("error: " + str(exc))
            time.sleep(10)
