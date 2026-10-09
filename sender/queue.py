"""Bounded image processing, numeric ordering and crash-recoverable SQLite state."""
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
import warnings

from PIL import Image, ImageOps


NAME = re.compile(r"(?P<seq>[0-9]+)\.(?:jpg|jpeg|png)", re.IGNORECASE)
MAX_SEQUENCE = 2**63 - 2


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def signature(stat):
    # Python/Windows stat and fstat disagree about ctime (creation/change time).
    # Linux ctime is useful; Windows still checks size, mtime and file identity.
    changed = stat.st_ctime_ns if os.name != "nt" else 0
    return (stat.st_size, stat.st_mtime_ns, changed, stat.st_ino)


class InstanceLock:
    """One queue owner per state directory; released by the OS on process death."""

    def __init__(self, path):
        self.file = open(str(path), "a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.file.close()
            raise RuntimeError("Another process owns this state directory")

    def close(self):
        if not self.file.closed:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


class ImageQueue:
    def __init__(self, state_dir, *, mode="stable", stable_seconds=0.2,
                 longest_edge=640, jpeg_quality=75, max_cache_bytes=128*1024*1024,
                 max_ready=16, max_source_bytes=32*1024*1024,
                 max_pixels=16*1024*1024, retry_seconds=1.0, max_retries=3,
                 watch_dir=None, gap_seconds=30):
        if mode not in ("atomic", "stable"):
            raise ValueError("mode must be atomic or stable")
        if not 1 <= jpeg_quality <= 95 or longest_edge < 1:
            raise ValueError("Invalid preview dimensions or quality")
        if min(max_cache_bytes, max_ready, max_source_bytes, max_pixels) < 1:
            raise ValueError("Resource limits must be positive")
        if stable_seconds < 0 or retry_seconds <= 0:
            raise ValueError("Invalid waiting interval")
        if max_retries < 0 or gap_seconds <= 0:
            raise ValueError("max_retries must be nonnegative")
        self.root = Path(state_dir).resolve()
        if watch_dir is not None:
            self.validate_paths(watch_dir, self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = InstanceLock(self.root / "queue.lock")
        self.mode, self.stable_seconds = mode, stable_seconds
        self.longest_edge, self.quality = longest_edge, jpeg_quality
        self.max_cache_bytes, self.max_ready = max_cache_bytes, max_ready
        self.max_source_bytes, self.max_pixels = max_source_bytes, max_pixels
        self.retry_seconds = retry_seconds
        self.max_retries = max_retries
        self.gap_seconds = gap_seconds
        self._last_full_scan = {}
        self.db = sqlite3.connect(str(self.root / "queue.sqlite3"))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        # SQLite bounds its page cache; discovered files remain on disk, not a list.
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            position INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, watch TEXT NOT NULL, next_seq INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS images(
            session TEXT NOT NULL, seq INTEGER NOT NULL, name TEXT NOT NULL,
            signature TEXT NOT NULL, stable_since REAL NOT NULL, observations INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'discovered',
            source_hash TEXT, preview_hash TEXT, preview_bytes INTEGER,
            width INTEGER, height INTEGER, retry_after REAL NOT NULL DEFAULT 0,
            last_error TEXT, PRIMARY KEY(session, seq));
        CREATE TABLE IF NOT EXISTS issues(
            session TEXT NOT NULL, code TEXT NOT NULL, seq INTEGER NOT NULL,
            detail TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            count INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(session, code, seq));
        """)
        if "attempts" not in {r[1] for r in self.db.execute("PRAGMA table_info(images)")}:
            self.db.execute("ALTER TABLE images ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        for column in ("preview_edge", "preview_quality"):
            if column not in {r[1] for r in self.db.execute("PRAGMA table_info(images)")}:
                self.db.execute("ALTER TABLE images ADD COLUMN " + column + " INTEGER")
        self.db.executescript("""
        CREATE INDEX IF NOT EXISTS images_status ON images(status,session,seq);
        CREATE INDEX IF NOT EXISTS issues_active ON issues(active,session,code,seq);
        CREATE TABLE IF NOT EXISTS totals(status TEXT PRIMARY KEY, n INTEGER NOT NULL, bytes INTEGER NOT NULL);
        DELETE FROM totals;
        INSERT INTO totals SELECT status,COUNT(*),COALESCE(SUM(preview_bytes),0) FROM images GROUP BY status;
        CREATE TRIGGER IF NOT EXISTS totals_insert AFTER INSERT ON images BEGIN
          INSERT INTO totals VALUES(new.status,1,COALESCE(new.preview_bytes,0))
          ON CONFLICT(status) DO UPDATE SET n=n+1,bytes=bytes+COALESCE(new.preview_bytes,0);
        END;
        CREATE TRIGGER IF NOT EXISTS totals_update AFTER UPDATE OF status,preview_bytes ON images BEGIN
          UPDATE totals SET n=n-1,bytes=bytes-COALESCE(old.preview_bytes,0) WHERE status=old.status;
          INSERT INTO totals VALUES(new.status,1,COALESCE(new.preview_bytes,0))
          ON CONFLICT(status) DO UPDATE SET n=n+1,bytes=bytes+COALESCE(new.preview_bytes,0);
        END;
        """)
        try:
            if self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] > 1:
                raise ValueError("Legacy multi-flight state is unsupported; preserve it and use a new state directory")
            self._recover()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def register(self, watch_dir, session_id=None):
        watch = self.validate_paths(watch_dir, self.root)
        existing = self.db.execute("SELECT id,watch FROM sessions ORDER BY position").fetchall()
        if existing:
            if len(existing) != 1 or existing[0]["watch"] != str(watch) or (session_id and session_id != existing[0]["id"]):
                raise ValueError("One flight per state directory; use a new state directory for a new flight")
            return existing[0]["id"]
        if session_id is None:
            session_id = uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
            raise ValueError("Invalid session_id")
        with self.db:
            self.db.execute("INSERT INTO sessions(id,watch) VALUES(?,?)", (session_id, str(watch)))
        (self.root / "spool" / session_id).mkdir(parents=True, exist_ok=True)
        return session_id

    @staticmethod
    def validate_paths(watch_dir, state_dir):
        watch = Path(watch_dir).resolve(strict=True)
        root = Path(state_dir).resolve()
        if not watch.is_dir():
            raise ValueError("watch_dir must be a directory")
        # State and source trees must be disjoint.
        if watch == root or watch in root.parents or root in watch.parents:
            raise ValueError("State directory must be separate from the source tree")
        return watch

    def preview_path(self, session_id, seq):
        # IDs originate from validated session records; filenames are never used as paths here.
        return self.root / "spool" / session_id / (str(seq) + ".jpg")

    def _issue(self, sid, code, seq, detail, now):
        self.db.execute("""INSERT INTO issues VALUES(?,?,?,?,?,?,1,1)
          ON CONFLICT(session,code,seq) DO UPDATE SET detail=excluded.detail,
          last_seen=excluded.last_seen,count=count+1,active=1""",
                        (sid, code, seq, detail[:1000], now, now))

    def _resolve(self, sid, code, seq):
        self.db.execute("UPDATE issues SET active=0 WHERE session=? AND code=? AND seq=?", (sid, code, seq))

    def scan(self, sid, now=None, ordered=False, full_scan_seconds=30):
        now = time.time() if now is None else now
        session = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise ValueError("Unknown session")
        if ordered and now - self._last_full_scan.get(sid, float('-inf')) < full_scan_seconds:
            # Bounded direct probes: no directory walk in the normal polling loop.
            start = session["next_seq"]
            with self.db:
                try:
                    for seq in range(start, min(start + 32, MAX_SEQUENCE + 1)):
                        names = {str(seq) + "." + ext for ext in ("jpg", "jpeg", "png")}
                        known = self.db.execute("SELECT name FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
                        if known:
                            names.add(known[0])
                        for name in sorted(names):
                            path = Path(session["watch"]) / name
                            if not path.is_file() or path.is_symlink():
                                continue
                            sig = json.dumps(signature(path.stat()))
                            self._observe(sid, seq, path.name, sig, now)
                except OSError as error:
                    self._issue(sid, "watch_unavailable", -1, str(error), now)
            return
        self._last_full_scan[sid] = now
        with self.db:
            # Re-evaluate conflicts on each complete scan, rather than permanently blocking a repaired name.
            self.db.execute("UPDATE issues SET active=0 WHERE session=? AND code='duplicate_sequence'", (sid,))
            try:
                with os.scandir(session["watch"]) as entries:
                    for entry in entries:
                        match = NAME.fullmatch(entry.name)
                        if not match:
                            continue
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            self._issue(sid, "non_regular_source", -1, entry.name, now)
                            continue
                        seq = int(match["seq"])
                        if seq > MAX_SEQUENCE:
                            self._issue(sid, "invalid_sequence", -1, entry.name, now)
                            continue
                        try:
                            # Windows DirEntry.stat() reports inode 0; os.stat() and
                            # fstat() provide matching identities on both platforms.
                            sig = json.dumps(signature(os.stat(entry.path, follow_symlinks=False)))
                        except FileNotFoundError:
                            continue
                        self._observe(sid, seq, entry.name, sig, now)
                self._resolve(sid, "watch_unavailable", -1)
            except OSError as error:
                self._issue(sid, "watch_unavailable", -1, str(error), now)

    def _observe(self, sid, seq, name, sig, now):
        row = self.db.execute("SELECT * FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
        if row is None:
            cursor = self.db.execute("SELECT next_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            if seq < cursor:
                self._issue(sid, "late_image", seq, "Image arrived after its missing range was skipped", now)
                return
            self.db.execute("""INSERT INTO images(session,seq,name,signature,stable_since,observations)
                VALUES(?,?,?,?,?,1)""", (sid, seq, name, sig, now))
        elif row["name"] != name:
            self._issue(sid, "duplicate_sequence", seq, row["name"] + " / " + name, now)
        elif row["signature"] != sig:
            if row["status"] in ("ready", "acked", "failed"):
                self._issue(sid, "source_changed", seq, name, now)
            else:
                self.db.execute("""UPDATE images SET signature=?,stable_since=?,observations=1,
                    retry_after=0,last_error=NULL WHERE session=? AND seq=?""", (sig, now, sid, seq))
        elif row["observations"] < 3:
            self.db.execute("""UPDATE images SET observations=MIN(observations+1,3),
                stable_since=CASE WHEN observations=0 THEN ? ELSE stable_since END
                WHERE session=? AND seq=?""", (now, sid, seq))

    def _read_preview(self, path, expected_signature, edge=None, quality=None):
        edge = self.longest_edge if edge is None else edge
        quality = self.quality if quality is None else quality
        if path.is_symlink():
            raise ValueError("Symlink source is not allowed")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(str(path), flags), "rb") as source:
            before = signature(os.fstat(source.fileno()))
            if json.dumps(before) != expected_signature:
                raise ValueError("Source changed before read")
            if before[0] > self.max_source_bytes:
                raise ValueError("Source exceeds byte limit")
            # Python 3.8 may allocate the requested buffer even for a tiny file.
            # Read the observed length (+1 detects growth), not the global 32 MiB cap.
            data = source.read(before[0] + 1)
            if len(data) != before[0] or signature(os.fstat(source.fileno())) != before:
                raise ValueError("Source changed during read")
        if path.is_symlink() or signature(path.stat()) != before:
            raise ValueError("Source replaced during read")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as check:
                if check.width * check.height > self.max_pixels:
                    raise ValueError("Image exceeds pixel limit")
                check.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()  # Truncated-file tolerance is deliberately not enabled.
                with ImageOps.exif_transpose(decoded) as oriented:
                    with oriented.convert("RGB") as rgb:
                        rgb.thumbnail((edge, edge), Image.Resampling.LANCZOS
                                      if hasattr(Image, "Resampling") else Image.LANCZOS)
                        output = io.BytesIO()
                        rgb.save(output, format="JPEG", quality=quality)
                        return output.getvalue(), sha256(data), rgb.size

    def _spool_usage(self):
        row = self.db.execute("SELECT n,bytes FROM totals WHERE status='ready'").fetchone()
        return (row[0], row[1]) if row else (0, 0)

    @property
    def ready_count(self):
        return self._spool_usage()[0]

    def prepare(self, max_items=4, now=None):
        """Process only the next numeric file per session; keep all backlog on disk."""
        if max_items < 1:
            raise ValueError("max_items must be positive")
        now = time.time() if now is None else now
        produced = 0
        # Session cursor and DB rows are streamed; at most one source image is decoded at once.
        for session in self.db.execute("SELECT * FROM sessions ORDER BY position"):
            sid = session["id"]
            if not Path(session["watch"]).is_dir():
                with self.db:
                    self._issue(sid, "watch_unavailable", -1, "Source directory unavailable; processing paused", now)
                return produced
            seq = session["next_seq"]
            while produced < max_items:
                row = self.db.execute("SELECT * FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
                if row is not None and row["status"] in ("ready", "acked", "failed"):
                    seq += 1
                    with self.db:
                        self.db.execute("UPDATE sessions SET next_seq=? WHERE id=?", (seq, sid))
                    continue
                if row is None:
                    later = self.db.execute("SELECT seq FROM images WHERE session=? AND seq>? ORDER BY seq LIMIT 1", (sid, seq)).fetchone()
                    with self.db:
                        if later:
                            self._issue(sid, "sequence_gap", seq, "Waiting for sequence {}".format(seq), now)
                            first = self.db.execute("SELECT first_seen FROM issues WHERE session=? AND code='sequence_gap' AND seq=?", (sid, seq)).fetchone()[0]
                            if now - first >= self.gap_seconds:
                                detail = "Missing sequence range {}..{} skipped after {} seconds".format(seq, later[0]-1, self.gap_seconds)
                                self._issue(sid, "gap_skipped", seq, detail, now)
                                self._resolve(sid, "sequence_gap", seq)
                                self.db.execute("UPDATE sessions SET next_seq=? WHERE id=?", (later[0], sid))
                                logging.getLogger("mpnano").warning("session=%s %s", sid, detail)
                                seq = later[0]
                                continue
                    break
                with self.db:
                    self._resolve(sid, "sequence_gap", seq)
                conflict = self.db.execute("SELECT 1 FROM issues WHERE session=? AND seq=? AND code='duplicate_sequence' AND active=1",
                                           (sid, seq)).fetchone()
                if row["retry_after"] > now:
                    break
                if conflict:
                    if self.record_failure(sid, seq, "Duplicate numeric filenames", now=now):
                        seq += 1
                        continue
                    break
                if self.mode == "stable" and (Path(session["watch"]) / row["name"]).exists() and (row["observations"] < 2 or now - row["stable_since"] < self.stable_seconds):
                    break
                count, used = self._spool_usage()
                # Restoring an already-admitted head must not wait for later ready
                # images to drain: strict ordering prevents those images from sending.
                rebuilding = bool(row["preview_hash"])
                if count >= self.max_ready and not rebuilding:
                    with self.db:
                        self._issue(sid, "cache_full", seq, "Ready-count limit reached", now)
                    return produced
                try:
                    edge = row["preview_edge"] or self.longest_edge
                    quality = row["preview_quality"] or self.quality
                    preview_started = time.monotonic()
                    try:
                        preview, source_hash, size = self._read_preview(Path(session["watch"]) / row["name"], row["signature"], edge, quality)
                    finally:
                        self.preview_seconds = getattr(self, "preview_seconds", 0.0) + time.monotonic() - preview_started
                    if row["source_hash"] and source_hash != row["source_hash"]:
                        raise ValueError("Original changed; cannot reconstruct the pending preview")
                    if row["preview_hash"] and sha256(preview) != row["preview_hash"]:
                        raise ValueError("Preview identity changed; original encoding cannot be reproduced")
                    if len(preview) > self.max_cache_bytes:
                        raise ValueError("Single preview exceeds the entire cache byte limit")
                    if used + len(preview) > self.max_cache_bytes and not rebuilding:
                        with self.db:
                            self._issue(sid, "cache_full", seq, "Preview byte limit reached", now)
                            # Do not repeatedly decode a large source at every poll while full.
                            self.db.execute("UPDATE images SET retry_after=? WHERE session=? AND seq=?",
                                            (now + self.retry_seconds, sid, seq))
                        return produced
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
                        Image.DecompressionBombWarning) as error:
                    if self.record_failure(sid, seq, str(error), now=now):
                        seq += 1
                        continue
                    break
                # Spool write / database failures are global faults, not bad-image failures.
                target = self.preview_path(sid, seq)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".part")
                with open(str(temporary), "wb") as stream:
                    stream.write(preview)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary), str(target))
                with self.db:
                    self.db.execute("""UPDATE images SET status='ready',source_hash=?,preview_hash=?,
                        preview_bytes=?,width=?,height=?,preview_edge=?,preview_quality=?,last_error=NULL,retry_after=0 WHERE session=? AND seq=?""",
                        (source_hash, sha256(preview), len(preview), size[0], size[1], edge, quality, sid, seq))
                    self.db.execute("UPDATE sessions SET next_seq=? WHERE id=?", (seq+1, sid))
                    self._resolve(sid, "image_error", seq)
                    self._resolve(sid, "cache_full", seq)
                    self._resolve(sid, "cache_rebuild", seq)
                produced += 1
                seq += 1
            if produced >= max_items:
                break
        return produced

    def record_failure(self, sid, seq, reason, *, now=None, connection_available=True):
        """P2 must pass False for link outages: these never consume image retries."""
        if not connection_available:
            return False
        now = time.time() if now is None else now
        row = self.db.execute("SELECT * FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
        if row is None or row["status"] not in ("discovered", "ready"):
            raise ValueError("Failure must refer to a pending image")
        attempts = row["attempts"] + 1
        failed = attempts >= self.max_retries + 1
        with self.db:
            self.db.execute("UPDATE images SET attempts=?,last_error=?,retry_after=?,status=? WHERE session=? AND seq=?",
                            (attempts, reason[:1000], now+self.retry_seconds, "failed" if failed else row["status"], sid, seq))
            self._issue(sid, "image_skipped" if failed else "image_error", seq, reason, now)
            if failed:
                self._resolve(sid, "image_error", seq)
                self._resolve(sid, "duplicate_sequence", seq)
                self._resolve(sid, "cache_rebuild", seq)
                self.db.execute("UPDATE sessions SET next_seq=next_seq+1 WHERE id=? AND next_seq=?", (sid, seq))
        if failed:
            logging.getLogger("mpnano").warning("Image skipped: session=%s seq=%s attempts=%s reason=%s", sid, seq, attempts, reason)
            path = self.preview_path(sid, seq)
            if path.exists():
                path.unlink()
        return failed

    def next_ready(self, now=None):
        now = time.time() if now is None else now
        # Each query seeks the status index instead of sorting all pending/history rows.
        heads = [self.db.execute("SELECT * FROM images WHERE status=? ORDER BY session,seq LIMIT 1", (status,)).fetchone()
                 for status in ("ready", "discovered")]
        row = min((r for r in heads if r is not None), key=lambda r: r["seq"], default=None)
        if not row or row["status"] != "ready" or row["retry_after"] > now:
            return None
        if not self._cache_valid(row):
            self._rebuild(row, now)
            return None
        return dict(row)

    def _cache_valid(self, row):
        path = self.preview_path(row["session"], row["seq"])
        try:
            return (not path.is_symlink() and path.stat().st_size == row["preview_bytes"]
                    and sha256(path.read_bytes()) == row["preview_hash"])
        except OSError:
            return False

    def _rebuild(self, row, now):
        with self.db:
            self.db.execute("UPDATE images SET status='discovered',observations=0,retry_after=0 WHERE session=? AND seq=?",
                            (row["session"], row["seq"]))
            self.db.execute("UPDATE sessions SET next_seq=MIN(next_seq,?) WHERE id=?", (row["seq"], row["session"]))
            self._issue(row["session"], "cache_rebuild", row["seq"], "Missing/corrupt preview; rebuild from original", now)

    def acknowledge(self, sid, seq, digest):
        """P2 receiver-ACK boundary; in P1 only the explicit test sink calls this."""
        row = self.db.execute("SELECT * FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
        if row is None or row["status"] not in ("ready", "acked") or row["preview_hash"] != digest:
            raise ValueError("ACK does not match a ready preview")
        with self.db:
            self.db.execute("UPDATE images SET status='acked' WHERE session=? AND seq=?", (sid, seq))
        path = self.preview_path(sid, seq)
        if path.exists():
            path.unlink()  # Only this project's own acknowledged cache file.

    def _recover(self):
        # Crash before a DB commit may leave an orphan; remove only known spool filenames.
        for session in self.db.execute("SELECT id FROM sessions"):
            sid = session[0]
            directory = self.root / "spool" / sid
            if directory.exists():
                for path in directory.iterdir():
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeError("Unexpected entry in private spool: " + str(path))
                    if re.fullmatch(r"[0-9]+\.part", path.name):
                        path.unlink()
                    elif re.fullmatch(r"[0-9]+\.jpg", path.name):
                        seq = int(path.stem)
                        row = self.db.execute("SELECT status FROM images WHERE session=? AND seq=?", (sid, seq)).fetchone()
                        if row is None or row[0] != "ready":
                            path.unlink()
            for row in self.db.execute("SELECT * FROM images WHERE session=? AND status='ready'", (sid,)).fetchall():
                if not self._cache_valid(row):
                    self._rebuild(row, time.time())

    def summary(self):
        counts = {r[0]: r[1] for r in self.db.execute("SELECT status,n FROM totals WHERE n>0")}
        count, size = self._spool_usage()
        issues = [dict(r) for r in self.db.execute("SELECT session,code,seq,detail,count FROM issues WHERE active=1 LIMIT 20")]
        return {"counts": counts, "ready_count": count, "cache_bytes": size, "issues": issues,
                "active_issue_count": self.db.execute("SELECT COUNT(*) FROM issues WHERE active=1").fetchone()[0]}
