"""Single in-flight image, persistent receiver ACK, bounded reconnect backoff."""
import logging
import socket
import time
import uuid

from .protocol import ACK, IMAGE, NACK, STATUS, PING, PONG, ProtocolError, RateLimiter, encode_frame, recv_frame
from .queue import sha256


class TcpSender:
    def __init__(self, host, port=8765, timeout=10, rate_kib=128, reconnect_max=10, heartbeat_seconds=5):
        if not host or not 1 <= port <= 65535 or min(timeout, rate_kib, reconnect_max, heartbeat_seconds) <= 0:
            raise ValueError("Invalid TCP configuration")
        self.host, self.port, self.timeout = host, port, timeout
        self.reconnect_max = reconnect_max
        self.limiter = RateLimiter(rate_kib * 1024)
        self.sock = None
        self.heartbeat_seconds = heartbeat_seconds
        self.last_response = 0.0
        self.next_connect, self.backoff = 0.0, min(0.5, reconnect_max)
        self.stats = {"connected": False, "attempted": 0, "acked": 0, "nacked": 0,
                      "disconnects": 0, "frame_bytes": 0, "last_error": None, "heartbeats": 0}

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None
        self.stats["connected"] = False

    def _disconnect(self, error):
        self.close()
        self.stats["disconnects"] += 1
        self.stats["last_error"] = str(error)
        self.next_connect = time.monotonic() + self.backoff
        self.backoff = min(self.reconnect_max, self.backoff * 2)
        logging.getLogger("mpnano").warning("TCP disconnected; image retained: %s", error,
            extra={"event": "disconnected", "peer": "%s:%s" % (self.host, self.port),
                   "retry_seconds": max(0, self.next_connect-time.monotonic())})

    def connect(self):
        if self.sock:
            return True
        if time.monotonic() < self.next_connect:
            return False
        try:
            logging.getLogger("mpnano").debug("Connecting", extra={"event": "connect_attempt", "peer": "%s:%s" % (self.host, self.port)})
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.stats["connected"] = True
            self.last_response = 0.0
            logging.getLogger("mpnano").info("TCP connected", extra={"event": "connected", "peer": "%s:%s" % (self.host, self.port)})
            return True
        except OSError as error:
            self._disconnect(error)
            return False

    def step(self, queue):
        row = queue.next_ready()
        if not self.connect():
            return False
        if row is None:
            self.heartbeat()
            return False
        try:
            payload = queue.preview_path(row["session"], row["seq"]).read_bytes()
        except OSError:
            queue._rebuild(row, time.time())
            return False
        if sha256(payload) != row["preview_hash"]:
            # Re-enter the queue's cache reconstruction path on the next iteration.
            return False
        meta = {"session": row["session"], "seq": row["seq"], "name": row["name"],
                "format": "jpeg", "width": row["width"], "height": row["height"],
                "sha256": row["preview_hash"], "source_sha256": row["source_hash"]}
        try:
            frame = encode_frame(IMAGE, meta, payload)
        except ProtocolError as error:
            queue.record_failure(row["session"], row["seq"], str(error))
            return False
        try:
            self.stats["attempted"] += 1
            started = time.monotonic()
            identity = {"event": "image_send", "seq": row["seq"], "sha256": row["preview_hash"], "bytes": len(frame)}
            logging.getLogger("mpnano").debug("Sending image", extra=identity)
            timing = self.limiter.send(self.sock, frame, timeout=self.timeout)
            self.stats["frame_bytes"] += len(frame)
            ack_started = time.monotonic()
            kind, reply, _ = recv_frame(self.sock, timeout=self.timeout)
            timing["ack_wait_ms"] = (time.monotonic() - ack_started) * 1000
            if (reply.get("session"), reply.get("seq"), reply.get("sha256")) != (row["session"], row["seq"], row["preview_hash"]):
                raise ProtocolError("Response identity mismatch")
            if kind not in (ACK, NACK):
                raise ProtocolError("Expected ACK/NACK")
            code = reply.get("code")
            if kind == NACK and code == "STORAGE_ERROR":
                raise ConnectionError("Receiver storage unavailable; queue retained")
            if kind == NACK and code not in ("BAD_IMAGE", "HASH_MISMATCH", "CONFLICT", "BAD_METADATA", "RECORD_ERROR"):
                raise ProtocolError("Unknown receiver rejection code")
        except (OSError, EOFError, ProtocolError) as error:
            self._disconnect(error)
            return False
        # Local persistence failures must propagate; they are not link outages.
        self.last_response = time.monotonic()
        identity.update(event="image_ack" if kind == ACK else "image_nack", code=reply.get("code"),
                        elapsed_ms=round((time.monotonic()-started)*1000, 2))
        logging.getLogger("mpnano").debug("Receiver response", extra=identity)
        if kind == ACK:
            persist_started = time.monotonic()
            queue.acknowledge(row["session"], row["seq"], row["preview_hash"])
            timing["ack_persist_ms"] = (time.monotonic() - persist_started) * 1000
            logging.getLogger("mpnano").debug("Image stage timing", extra={
                "event": "image_timing", "seq": row["seq"], "sha256": row["preview_hash"],
                "timing": {k: round(v, 3) for k, v in timing.items()}})
            self.stats["acked"] += 1
            self.stats["last_error"] = None
            self.backoff = min(0.5, self.reconnect_max)
            return True
        self.stats["nacked"] += 1
        queue.record_failure(row["session"], row["seq"], "{}: {}".format(code, reply.get("detail", "")))
        return False

    def heartbeat(self):
        if not self.sock or time.monotonic() - self.last_response < self.heartbeat_seconds:
            return
        nonce = uuid.uuid4().hex
        try:
            frame = encode_frame(PING, {"nonce": nonce})
            self.limiter.send(self.sock, frame, timeout=self.timeout)
            self.stats["frame_bytes"] += len(frame)
            kind, reply, _ = recv_frame(self.sock, timeout=self.timeout)
            if kind != PONG or reply.get("nonce") != nonce:
                raise ProtocolError("Heartbeat response mismatch")
            self.last_response = time.monotonic()
            self.stats["heartbeats"] += 1
            logging.getLogger("mpnano").debug("Heartbeat response", extra={"event": "pong"})
            self.stats["last_error"] = None
            self.backoff = min(0.5, self.reconnect_max)
        except (OSError, EOFError, ProtocolError) as error:
            self._disconnect(error)

    def send_status(self, sid, summary):
        if not self.sock:
            return
        summary = dict(summary)
        summary["issues"] = [dict(issue, detail=str(issue.get("detail", ""))[:200]) for issue in summary.get("issues", [])[:10]]
        try:
            self.limiter.send(self.sock, encode_frame(STATUS, {"session": sid, "queue": summary}), timeout=self.timeout)
        except (OSError, ProtocolError) as error:
            self._disconnect(error)
