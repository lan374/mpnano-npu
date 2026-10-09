"""Bounded refill and discovery scheduling; transport retains ordering and retry ownership."""
import time


class SenderLoop:
    def __init__(self, queue, session, transport, scan_interval, full_scan_seconds,
                 timings, clock=time.monotonic):
        self.queue, self.session, self.transport = queue, session, transport
        self.scan_interval, self.full_scan_seconds = scan_interval, full_scan_seconds
        self.timings, self.clock = timings, clock
        self.next_scan = self.next_full_check = float('-inf')
        self.progress = False
        self.scans = self.prepares = 0

    def advance(self):
        now = self.clock()
        # O(1) maintained count, no cache reads or directory walk for this decision.
        refill = self.queue.ready_count <= self.queue.max_ready // 2
        full_due = now >= self.next_full_check
        if now >= self.next_scan and (refill or not self.progress or full_due):
            stage = self.clock()
            self.queue.scan(self.session, ordered=True, full_scan_seconds=self.full_scan_seconds)
            self.timings['scan_ms'] += (self.clock()-stage)*1000
            self.scans += 1
            self.next_scan = self.clock()+self.scan_interval
            if full_due:
                self.next_full_check = self.clock()+self.full_scan_seconds
        if refill or not self.progress or self.transport is None:
            stage = self.clock()
            # Keep the existing bounded prepare batch and source-stability/retry checks.
            self.queue.prepare()
            self.timings['prepare_ms'] += (self.clock()-stage)*1000
            self.prepares += 1
        stage = self.clock()
        self.progress = bool(self.transport and self.transport.step(self.queue))
        self.timings['transport_ms'] += (self.clock()-stage)*1000
        # Failed ACK/NACK, no image and disconnected paths still yield: no hot polling.
        return 0.0 if self.progress else self.scan_interval
