"""Bounded, best-effort sky diagnostics. Never carry image bytes or block TCP on disk."""
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import sys
import threading
import time
import uuid


class SkyDebugLog(logging.Handler):
    def __init__(self, directory, session, level="DEBUG"):
        super().__init__(getattr(logging, level))
        self.directory = Path(directory)
        self.session, self.run_id = session, uuid.uuid4().hex
        self.pending = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.dropped = 0
        self.worker = threading.Thread(target=self._work, name="sky-debug-log", daemon=True)
        self.worker.start()

    def emit(self, record):
        try:
            data = {"utc": datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).isoformat(),
                    "side": "sky", "run_id": self.run_id, "session": self.session,
                    "level": record.levelname, "event": getattr(record, "event", "message"),
                    "message": record.getMessage()[:8192]}
            for key in ("seq", "sha256", "peer", "bytes", "retry_seconds", "code", "elapsed_ms", "timing"):
                if hasattr(record, key):
                    data[key] = getattr(record, key)
            if record.exc_info:
                data["exception"] = logging.Formatter().formatException(record.exc_info)[-8192:]
            data["diagnostic_dropped_total"] = self.dropped
            self.pending.put_nowait(data)
        except Exception:
            self.dropped += 1

    def _work(self):
        sink, last_warning = None, 0.0
        try:
            while not self.stopping.is_set() or not self.pending.empty():
                try:
                    data = self.pending.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if sink is None:
                        self.directory.mkdir(parents=True, exist_ok=True)
                        sink = RotatingFileHandler(str(self.directory / "sky.debug.jsonl"),
                                                   maxBytes=4*1024*1024, backupCount=3, encoding="utf-8")
                    # Write directly so I/O errors reach our bounded warning path.
                    line = json.dumps(data, ensure_ascii=False) + "\n"
                    if sink.stream.tell() + len(line.encode("utf-8")) > sink.maxBytes:
                        sink.doRollover()
                    sink.stream.write(line)
                    sink.flush()
                except Exception as error:
                    self.dropped += 1
                    if sink is not None:
                        try:
                            sink.close()
                        except Exception:
                            pass
                        sink = None
                    if time.monotonic() - last_warning >= 30:
                        last_warning = time.monotonic()
                        try:
                            print("Sky debug log unavailable: {}".format(error), file=sys.stderr)
                        except Exception:
                            pass
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass

    def close(self):
        self.stopping.set()
        self.worker.join(timeout=1)
        super().close()
