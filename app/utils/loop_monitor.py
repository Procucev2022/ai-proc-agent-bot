"""
Event-loop stall detector.

The inbound WhatsApp path runs entirely inside the uvicorn worker's event loop:
the webhook handler, the batch poller, the monitoring loop and every user's turn
share one thread. A single synchronous call in that thread -- a blocking HTTP
request, a sync database query, a CPU-bound parse -- freezes *all* of them, so a
symptom reported as "one user's reply was slow" can really be "another user's
outbound call blocked the loop for 30 seconds".

That failure mode is invisible in ordinary logs. Nothing reports its own
duration as long, because the stalled code is not running; it is the code that
never got scheduled. This monitor makes it visible by sleeping for a known
interval and measuring how much longer the sleep actually took. The overshoot is
the time the loop spent unable to run a ready callback.

It is deliberately tiny: one task, one ``asyncio.sleep`` per tick, and a log
line only when the overshoot crosses a threshold.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

TAG = "[LOOP]"

# How often to probe. Short enough to catch a stall inside one turn, long enough
# to cost nothing measurable.
DEFAULT_INTERVAL_SECONDS = 1.0

# Overshoot above which a stall is reported. Normal scheduling jitter under load
# is a few milliseconds; 250ms means real work blocked the thread.
DEFAULT_WARN_SECONDS = 0.25

# Overshoot that indicates the loop was blocked long enough to delay a user's
# reply or trip a gateway timeout.
DEFAULT_ERROR_SECONDS = 2.0


class EventLoopMonitor:
    """Samples event-loop scheduling delay and logs stalls."""

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        warn_threshold: float = DEFAULT_WARN_SECONDS,
        error_threshold: float = DEFAULT_ERROR_SECONDS,
    ) -> None:
        self.interval = interval
        self.warn_threshold = warn_threshold
        self.error_threshold = error_threshold
        self.worker_pid = os.getpid()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Running totals, exposed on /health-style endpoints so a stall that
        # happened minutes ago is still discoverable without log search.
        self.samples = 0
        self.stalls = 0
        self.worst_stall = 0.0
        self.total_stalled = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict:
        """Snapshot of what this monitor has seen, for health endpoints."""
        return {
            "worker_pid": self.worker_pid,
            "running": self._running,
            "samples": self.samples,
            "stalls": self.stalls,
            "worst_stall_seconds": round(self.worst_stall, 3),
            "total_stalled_seconds": round(self.total_stalled, 3),
            "warn_threshold_seconds": self.warn_threshold,
        }

    def _record(self, overshoot: float) -> None:
        """Update counters and log when the overshoot is worth reporting."""
        self.samples += 1
        if overshoot < self.warn_threshold:
            return

        self.stalls += 1
        self.total_stalled += overshoot
        if overshoot > self.worst_stall:
            self.worst_stall = overshoot

        detail = (
            f"{TAG}[WORKER-{self.worker_pid}] Event loop blocked for "
            f"{overshoot * 1000:.0f}ms (expected <{self.warn_threshold * 1000:.0f}ms). "
            f"Something synchronous ran on the loop thread; every other user's "
            f"turn, the batch poller and the webhook were all stalled for that "
            f"long. stalls={self.stalls} worst={self.worst_stall * 1000:.0f}ms"
        )
        if overshoot >= self.error_threshold:
            logger.error(detail)
        else:
            logger.warning(detail)

    async def run(self) -> None:
        """
        Probe the loop until stopped.

        Never raises into the caller: this is a diagnostic, and a diagnostic that
        can take down the worker it observes is worse than no diagnostic.
        """
        self._running = True
        logger.info(
            f"{TAG}[WORKER-{self.worker_pid}] Event loop monitor started "
            f"(interval={self.interval}s warn>{self.warn_threshold * 1000:.0f}ms)"
        )
        try:
            while self._running:
                before = time.perf_counter()
                await asyncio.sleep(self.interval)
                if not self._running:
                    break
                overshoot = (time.perf_counter() - before) - self.interval
                self._record(max(0.0, overshoot))
        except asyncio.CancelledError:
            logger.debug(f"{TAG}[WORKER-{self.worker_pid}] Event loop monitor cancelled")
            raise
        except Exception as exc:  # pragma: no cover - defensive only
            logger.error(f"{TAG} Event loop monitor stopped unexpectedly: {exc}", exc_info=True)
        finally:
            self._running = False

    def start(self) -> asyncio.Task:
        """Create the monitor task, reusing it when already running."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run(), name="event_loop_monitor")
        return self._task

    async def stop(self) -> None:
        """Signal the loop to exit and wait briefly for the task to finish."""
        self._running = False
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._task = None
            logger.info(f"{TAG}[WORKER-{self.worker_pid}] Event loop monitor stopped")


# Held in a dict rather than a module global so the accessor below does not have
# to open with a `global` statement, which compiles to no bytecode and would make
# the function permanently unreachable to the per-function coverage gate.
_singleton: Dict[str, Optional[EventLoopMonitor]] = {"monitor": None}


def get_loop_monitor() -> EventLoopMonitor:
    """Process-wide monitor, created on first use."""
    monitor = _singleton["monitor"]
    if monitor is None:
        monitor = EventLoopMonitor(
            interval=float(os.getenv("LOOP_MONITOR_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
            warn_threshold=float(os.getenv("LOOP_MONITOR_WARN_SECONDS", DEFAULT_WARN_SECONDS)),
            error_threshold=float(os.getenv("LOOP_MONITOR_ERROR_SECONDS", DEFAULT_ERROR_SECONDS)),
        )
        _singleton["monitor"] = monitor
    return monitor
