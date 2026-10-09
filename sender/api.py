"""Pipeline-facing lifecycle API; the tested sender CLI runs in an isolated process."""
import copy
from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from .queue import ImageQueue


@dataclass(frozen=True)
class SenderConfig:
    port: int = 8765
    mode: str = 'stable'
    scan_interval: float = .2
    stable_seconds: float = .2
    longest_edge: int = 640
    jpeg_quality: int = 75
    max_ready: int = 16
    max_cache_mib: float = 128
    max_retries: int = 3
    retry_seconds: float = 1
    full_scan_seconds: float = 30
    gap_seconds: float = 30
    network_timeout: float = 10
    heartbeat_seconds: float = 5
    rate_kib: float = 128
    log_level: str = 'DEBUG'

    def __post_init__(self):
        integers = ('port', 'longest_edge', 'jpeg_quality', 'max_ready', 'max_retries')
        for key, value in asdict(self).items():
            if key in ('mode', 'log_level'):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Invalid numeric setting: ' + key)
            if key in integers and not isinstance(value, int):
                raise ValueError('Integer required: ' + key)
            if value < 0 or (value == 0 and key not in ('max_retries', 'stable_seconds')):
                raise ValueError('Out of range: ' + key)
        if not 1 <= self.port <= 65535 or not 1 <= self.jpeg_quality <= 95:
            raise ValueError('Invalid port or JPEG quality')
        if self.scan_interval < .02 or self.longest_edge > 4096 or self.max_cache_mib * 1048576 < 1:
            raise ValueError('Invalid scan interval, image size or cache budget')
        if self.mode not in ('stable', 'atomic') or self.log_level not in ('DEBUG', 'INFO', 'WARNING'):
            raise ValueError('Invalid mode or log level')

    @classmethod
    def from_json(cls, path):
        with Path(path).open(encoding='utf-8-sig') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError('Configuration must be a JSON object')
        return cls(**value)  # Unknown fields are errors, never silently ignored.


class Sender:
    """One flight per object. New flights allocate a directory; resume uses an exact old one.

    The module must also be installed for python_executable. start waits for local
    initialization, not a ground connection. stop is cooperative and never kills
    a process on timeout; inspect its return value before discarding the handle.
    """
    def __init__(self, watch_dir, state_dir, ground_host, *, config=None, resume=False,
                 python_executable=None):
        if not isinstance(ground_host, str) or not ground_host.strip():
            raise ValueError('ground_host is required')
        self.config = config or SenderConfig()
        if not isinstance(self.config, SenderConfig):
            raise TypeError('config must be SenderConfig')
        self.watch_dir = str(Path(watch_dir).resolve())
        base = Path(state_dir).resolve()
        ImageQueue.validate_paths(self.watch_dir, base)
        if resume and not (base / 'queue.sqlite3').is_file():
            raise ValueError('Resume requires an existing flight state directory')
        self.session_id = None if resume else uuid.uuid4().hex
        self.state_dir = str(base if resume else base / ('flight-' + self.session_id))
        self.ground_host = ground_host
        self.python_executable = str(python_executable or sys.executable)
        self._process = None
        self._reader = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._state = {'lifecycle': 'created', 'state_dir': self.state_dir}
        self._tail = deque(maxlen=20)

    def start(self, timeout=10):
        if self._process is not None:
            raise RuntimeError('Sender objects are single-use; create a new object to resume')
        command = [self.python_executable, '-u', '-m', 'sender', '--watch-dir', self.watch_dir,
                   '--state-dir', self.state_dir, '--ground-host', self.ground_host, '--control-stdin']
        if self.session_id:
            command += ['--session-id', self.session_id]
        for key, value in asdict(self.config).items():
            command += ['--' + key.replace('_', '-'), str(value)]
        with self._lock:
            self._state['lifecycle'] = 'starting'
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        except Exception:
            with self._lock:
                self._state['lifecycle'] = 'failed'
            raise
        self._reader = threading.Thread(target=self._read, name='mpnano-sender-status', daemon=True)
        self._reader.start()
        if not self._ready.wait(timeout):
            self.stop(timeout=0)
            raise TimeoutError('Sender initialization timed out; stop was requested, retain handle and call stop again')
        snapshot = self.status()
        if 'session_id' not in snapshot:
            raise RuntimeError('Sender initialization failed: ' + '\n'.join(snapshot['diagnostic_tail']))
        return self

    def _read(self):
        try:
            for line in self._process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    with self._lock:
                        self._tail.append(line.rstrip()[-2048:])
                    continue
                with self._lock:
                    if 'session_id' in value:
                        self._state.update(value)
                        self._state['lifecycle'] = 'running'
                        self._ready.set()
                    elif 'counts' in value:
                        self._state['queue'] = value
        finally:
            code = self._process.wait()
            self._process.stdout.close()
            self._process.stdin.close()
            with self._lock:
                self._state.update(lifecycle='stopped' if code == 0 else 'failed', exit_code=code)
            self._ready.set()

    def status(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            result['diagnostic_tail'] = list(self._tail)
            return result

    def stop(self, timeout=15):
        if self._process is None:
            return True
        if self._process.poll() is None:
            with self._lock:
                self._state['lifecycle'] = 'stopping'
            try:
                self._process.stdin.write('stop\n')
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        if self._reader:
            self._reader.join(timeout=1)
        return True

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        if not self.stop():
            raise TimeoutError('Sender is still stopping; retain its handle and call stop again')
