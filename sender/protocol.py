"""MPN2 v1 framing. All lengths use network byte order; metadata is UTF-8 JSON."""
import json
import struct
import time

MAGIC = b"MPN2"
VERSION = 1
IMAGE, ACK, NACK, STATUS = 1, 2, 3, 4
PING, PONG = 5, 6
HEADER = struct.Struct("!4sBBHIQ")
MAX_META = 16384
MAX_PAYLOAD = 2 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def recv_exact(sock, length, deadline=None):
    result = bytearray()
    while len(result) < length:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Frame receive deadline exceeded")
            sock.settimeout(remaining)
        block = sock.recv(min(length - len(result), 65536))
        if not block:
            raise EOFError("Connection closed within a frame")
        result.extend(block)
    return bytes(result)


def recv_frame(sock, timeout=None):
    original = sock.gettimeout()
    try:
        return _recv_frame(sock, None if timeout is None else time.monotonic() + timeout)
    finally:
        sock.settimeout(original)


def _recv_frame(sock, deadline):
    magic, version, kind, flags, meta_length, payload_length = HEADER.unpack(recv_exact(sock, HEADER.size, deadline))
    if (magic != MAGIC or version != VERSION or flags or kind not in (IMAGE, ACK, NACK, STATUS, PING, PONG)
            or not 0 < meta_length <= MAX_META or payload_length > MAX_PAYLOAD
            or (kind != IMAGE and payload_length)):
        raise ProtocolError("Invalid frame header or unsupported protocol")
    try:
        meta = json.loads(recv_exact(sock, meta_length, deadline).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProtocolError("Invalid JSON metadata") from error
    if not isinstance(meta, dict):
        raise ProtocolError("Metadata must be an object")
    pending = [(meta, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > 16:
            raise ProtocolError("Metadata nesting exceeds limit")
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return kind, meta, recv_exact(sock, payload_length, deadline)


def encode_frame(kind, meta, payload=b""):
    metadata = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if (kind not in (IMAGE, ACK, NACK, STATUS, PING, PONG) or not 0 < len(metadata) <= MAX_META
            or len(payload) > MAX_PAYLOAD or (kind != IMAGE and payload)):
        raise ProtocolError("Frame exceeds protocol limits")
    return HEADER.pack(MAGIC, VERSION, kind, 0, len(metadata), len(payload)) + metadata + payload


class RateLimiter:
    """Paces application frame bytes, not Ethernet/radio bytes. No accumulated burst credit."""
    def __init__(self, bytes_per_second):
        if bytes_per_second <= 0:
            raise ValueError("Rate must be positive")
        self.rate = bytes_per_second
        self.next_send = 0.0

    def send(self, sock, data, timeout=None):
        original = sock.gettimeout() if timeout is not None else None
        # Allow configured pacing time plus one bounded transport stall budget.
        deadline = time.monotonic() + len(data) / self.rate + timeout if timeout is not None else None
        try:
            return self._send(sock, data, deadline, original)
        finally:
            if timeout is not None:
                sock.settimeout(original)

    def _send(self, sock, data, deadline, original):
        wait_seconds, send_seconds = 0.0, 0.0
        chunk_size = max(1, min(16384, int(self.rate * 0.1)))
        for offset in range(0, len(data), chunk_size):
            block = data[offset:offset + chunk_size]
            now = time.monotonic()
            if self.next_send > now:
                delay = self.next_send - now
                if deadline is not None and now + delay >= deadline:
                    raise TimeoutError("Frame send deadline exceeded")
                wait_started = time.monotonic()
                time.sleep(delay)
                wait_seconds += time.monotonic() - wait_started
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Frame send deadline exceeded")
                sock.settimeout(min(original, remaining) if original is not None else remaining)
            send_started = time.monotonic()
            sock.sendall(block)
            send_seconds += time.monotonic() - send_started
            self.next_send = time.monotonic() + len(block) / self.rate
        return {"rate_wait_ms": wait_seconds * 1000, "socket_send_ms": send_seconds * 1000}
