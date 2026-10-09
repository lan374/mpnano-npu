import argparse
import json
import logging
from pathlib import Path
import time
import uuid
import sys
import threading

from .queue import ImageQueue
from .transport import TcpSender
from .debuglog import SkyDebugLog
from .loop import SenderLoop


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Preview queue; add --ground-host to enable P2 TCP transport")
    parser.add_argument("--watch-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--log-dir", help="Sky-only debug directory; default STATE/logs")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING"), default="DEBUG")
    parser.add_argument("--session-id")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--mode", choices=("stable", "atomic"), default="stable")
    parser.add_argument("--scan-interval", type=float, default=0.2)
    parser.add_argument("--stable-seconds", type=float, default=0.2)
    parser.add_argument("--longest-edge", type=int, default=640)
    parser.add_argument("--jpeg-quality", type=int, default=75)
    parser.add_argument("--max-ready", type=int, default=16)
    parser.add_argument("--max-cache-mib", type=float, default=128)
    parser.add_argument("--cycles", type=int, default=0, help="0 means run until Ctrl+C")
    parser.add_argument("--max-retries", type=int, default=3, help="Retries after the first failed attempt")
    parser.add_argument("--retry-seconds", type=float, default=1)
    parser.add_argument("--full-scan-seconds", type=float, default=30)
    parser.add_argument("--gap-seconds", type=float, default=30, help="Wait for a missing index only when higher indices exist")
    parser.add_argument("--ground-host", help="Windows receiver address; omit for P1 queue-only mode")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--network-timeout", type=float, default=10)
    parser.add_argument("--heartbeat-seconds", type=float, default=5)
    parser.add_argument("--rate-kib", type=float, default=128, help="Application frame KiB/s, excluding TCP/radio overhead")
    parser.add_argument('--control-stdin', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    stopping = threading.Event()
    if args.control_stdin:
        def read_control():
            for line in sys.stdin:
                if line.strip() == 'stop':
                    break
            stopping.set()  # Parent exit/pipe EOF also requests a cooperative stop.
        threading.Thread(target=read_control, name='sender-control', daemon=True).start()
    if args.scan_interval < 0.02 or args.cycles < 0 or args.full_scan_seconds <= 0 or (args.new_session and args.session_id):
        parser.error("Invalid scan interval/cycles/session options")
    transport = TcpSender(args.ground_host, args.port, args.network_timeout, args.rate_kib,
                          heartbeat_seconds=args.heartbeat_seconds) if args.ground_host else None
    # Validate before creating any lock, database, log or cache in the source tree.
    ImageQueue.validate_paths(args.watch_dir, args.state_dir)
    if args.new_session:
        args.session_id = uuid.uuid4().hex
        args.state_dir = str(Path(args.state_dir) / ("flight-" + args.session_id))
    log_dir = args.log_dir or str(Path(args.state_dir) / "logs")
    ImageQueue.validate_paths(args.watch_dir, log_dir)
    with ImageQueue(args.state_dir, mode=args.mode, stable_seconds=args.stable_seconds,
                    longest_edge=args.longest_edge, jpeg_quality=args.jpeg_quality,
                    watch_dir=args.watch_dir, max_retries=args.max_retries, retry_seconds=args.retry_seconds,
                    gap_seconds=args.gap_seconds,
                    max_ready=args.max_ready, max_cache_bytes=int(args.max_cache_mib*1024*1024)) as queue:
        sid = queue.register(args.watch_dir, args.session_id)
        logger = logging.getLogger("mpnano")
        logger.setLevel(getattr(logging, args.log_level))
        handler = SkyDebugLog(log_dir, sid, args.log_level)
        logger.addHandler(handler)
        logger.info("Sky sender started; watch=%s state=%s peer=%s:%s rate_kib=%s", args.watch_dir,
                    args.state_dir, args.ground_host, args.port, args.rate_kib, extra={"event": "sender_started"})
        print(json.dumps({"session_id": sid, "state_dir": str(queue.root), "phase": "P2" if transport else "P1",
                          "transport": "TCP" if transport else "disabled", "log_dir": log_dir, "run_id": handler.run_id}), flush=True)
        cycle, last_log = 0, 0.0
        timings = {name: 0.0 for name in ("scan_ms", "prepare_ms", "transport_ms", "loop_sleep_ms")}
        driver = SenderLoop(queue, sid, transport, args.scan_interval, args.full_scan_seconds, timings)
        try:
            while not stopping.is_set():
                delay = driver.advance()
                cycle += 1
                if time.monotonic() - last_log >= 1 or (args.cycles and cycle >= args.cycles):
                    summary = queue.summary()
                    if transport:
                        transport.send_status(sid, summary)
                        summary["tcp"] = transport.stats
                    message = json.dumps(summary, ensure_ascii=False)
                    logger.info(message, extra={"event": "queue_status"})
                    print(message, flush=True)
                    last_log = time.monotonic()
                if args.cycles and cycle >= args.cycles:
                    break
                stage = time.monotonic()
                if delay:
                    stopping.wait(delay)
                timings["loop_sleep_ms"] += (time.monotonic() - stage) * 1000
        except KeyboardInterrupt:
            print("Stopped; queue state preserved.", flush=True)
        except Exception:
            logger.exception("Sender failed", extra={"event": "sender_failed"})
            raise
        finally:
            summary = queue.summary()
            if transport:
                summary['tcp'] = dict(transport.stats)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            timings["preview_read_encode_ms"] = getattr(queue, "preview_seconds", 0.0) * 1000
            timings["cycles"] = cycle
            timings["scan_calls"] = driver.scans
            timings["prepare_calls"] = driver.prepares
            logger.info("Cumulative loop timing; preview is a subset of prepare", extra={
                "event": "loop_timing", "timing": {k: round(v, 3) for k, v in timings.items()}})
            if transport:
                transport.close()
            logger.info("Sky sender stopped", extra={"event": "sender_stopped"})
            logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    main()
