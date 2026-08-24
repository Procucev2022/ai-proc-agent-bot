"""
Turn-scoped tracing for the inbound WhatsApp pipeline.

A *turn* is one inbound user message plus the reply it produces. The reply is
built across several detached async tasks -- a FastAPI ``BackgroundTask``, then a
batch-poller task, then the send wrapper -- so the request id created by
``app.context.middleware`` does not survive the hop and every log line for a
text turn was emitted with no correlation id at all.

This module carries a short turn id and a stage timeline across those hops in a
:class:`~contextvars.ContextVar`, which ``asyncio`` copies into each task it
creates. Each stage records its own duration and the turn closes with a single
summary line whose parts add up to the latency the user actually felt.

It also records *gateway lag*: the delay between the timestamp the WhatsApp
gateway stamped on the message and the moment the webhook reached this process.
Without it, a log can show that a reply took 46 seconds but not that 43 of those
elapsed before the application was even running -- which is exactly the
distinction needed to tell a cold start apart from slow application code.

Usage::

    trace = start_turn("919876543210", source="webhook",
                       gateway_timestamp="2026-08-22 12:53:54")
    async with stage("intent_classify"):
        ...
    trace.finish(outcome="sent")

Every helper is defensive: tracing must never be the reason a message fails to
get a reply, so a missing or broken trace degrades to a no-op rather than
raising into the caller.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Log tag used by every line this module emits, so a whole turn can be pulled
# out of Log Analytics with a single `| where Log_s contains "[TURN]"`.
TAG = "[TURN]"

# Stages slower than this are surfaced at WARNING even when the turn as a whole
# succeeded, because a stage that quietly costs seconds is the thing worth
# finding in a latency investigation.
SLOW_STAGE_SECONDS = 2.0

# A turn slower than this is the user-visible complaint threshold.
SLOW_TURN_SECONDS = 10.0

# The ICS/WhatsApp gateway stamps its own local time (IST) on each message while
# containers log in UTC. Kept as a module constant rather than a setting because
# it describes the upstream gateway, not this deployment.
GATEWAY_UTC_OFFSET_MINUTES = 330

_GATEWAY_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def _utcnow() -> datetime:
    """Naive UTC now, without the deprecated ``datetime.utcnow``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

_current_turn: ContextVar[Optional["TurnTrace"]] = ContextVar("current_turn", default=None)

# Keys used to smuggle the turn identity through the webhook payload dict. A text
# message is serialised into Redis between the webhook and the code that answers
# it, so a ContextVar alone cannot bridge the gap; these travel with the payload
# and let the far side resume the same turn instead of starting a fresh one.
# Both are underscore-prefixed so they cannot collide with a gateway field.
TURN_ID_FIELD = "_turn_id"
RECEIVED_AT_FIELD = "_received_at"


def parse_gateway_timestamp(raw: Any) -> Optional[datetime]:
    """
    Return the gateway's timestamp as a naive UTC ``datetime``, or ``None``.

    The gateway sends a local-time string such as ``'2026-08-22 12:53:54'`` with
    no offset, so the offset has to be applied here to make it comparable with
    the UTC clock the container logs in. Unparseable input returns ``None``
    instead of raising: a malformed timestamp is worth losing the lag metric
    over, not the message.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(raw).strip()
    if not text:
        return None
    for fmt in _GATEWAY_TIMESTAMP_FORMATS:
        try:
            local = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return local - timedelta(minutes=GATEWAY_UTC_OFFSET_MINUTES)
    return None


def gateway_lag_seconds(raw: Any, now: Optional[datetime] = None) -> Optional[float]:
    """
    Seconds between the gateway stamping the message and this process seeing it.

    A large value means the delay happened upstream of the application -- a cold
    start, a scaled-to-zero replica or a gateway-side queue -- and no amount of
    application tuning will move it.
    """
    parsed = parse_gateway_timestamp(raw)
    if parsed is None:
        return None
    reference = now or _utcnow()
    return (reference - parsed).total_seconds()


@dataclass
class TurnTrace:
    """Timeline for a single inbound message and the reply it produces."""

    turn_id: str
    user_phone: str
    source: str
    started_at: float = field(default_factory=time.time)
    started_perf: float = field(default_factory=time.perf_counter)
    gateway_lag: Optional[float] = None
    message_preview: str = ""
    stages: List[Tuple[str, float]] = field(default_factory=list)
    marks: List[Tuple[str, float]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    finished: bool = False

    @property
    def elapsed(self) -> float:
        """Seconds since the turn started, measured on a monotonic clock."""
        return time.perf_counter() - self.started_perf

    @property
    def user_perceived(self) -> Optional[float]:
        """
        Total latency from the user's point of view.

        The gateway lag is included because the user starts waiting when they
        press send, not when the webhook arrives.
        """
        if self.gateway_lag is None:
            return None
        return self.gateway_lag + self.elapsed

    def record(self, name: str, duration: float) -> None:
        """Append a completed stage to the timeline."""
        self.stages.append((name, duration))

    def mark(self, name: str, **fields: Any) -> None:
        """
        Record an instantaneous checkpoint at the current elapsed offset.

        Used where there is no block to wrap -- a queue hand-off, a cache hit, a
        branch being taken -- so the summary still shows when it happened.
        """
        offset = self.elapsed
        self.marks.append((name, offset))
        detail = _format_fields(fields)
        logger.debug(
            f"{TAG}[{self.turn_id}] mark={name} at +{offset:.3f}s{detail}"
        )

    def annotate(self, **fields: Any) -> None:
        """Attach key/value context that should appear on the summary line."""
        self.metadata.update(fields)

    def timeline(self) -> str:
        """Render the stage timeline as a compact, greppable string."""
        parts = [f"{name}={duration * 1000:.0f}ms" for name, duration in self.stages]
        return " ".join(parts) if parts else "no-stages"

    def finish(self, outcome: str = "complete", **fields: Any) -> None:
        """
        Emit the one-line summary for this turn.

        Idempotent: several exit paths converge on the same turn (a suppressed
        send, an early return, an exception handler) and a turn must not be
        summarised twice.
        """
        if self.finished:
            return
        self.finished = True
        if fields:
            self.metadata.update(fields)

        total = self.elapsed
        perceived = self.user_perceived
        lag_text = "unknown" if self.gateway_lag is None else f"{self.gateway_lag:.3f}s"
        perceived_text = "unknown" if perceived is None else f"{perceived:.3f}s"
        detail = _format_fields(self.metadata)

        message = (
            f"{TAG}[{self.turn_id}] SUMMARY outcome={outcome} phone={self.user_phone} "
            f"source={self.source} gateway_lag={lag_text} in_app={total:.3f}s "
            f"user_perceived={perceived_text} | {self.timeline()}{detail}"
        )

        if outcome in ("error", "dropped", "no_reply"):
            logger.error(message)
        elif perceived is not None and perceived >= SLOW_TURN_SECONDS:
            logger.warning(f"{message} | SLOW_TURN")
        elif total >= SLOW_TURN_SECONDS:
            logger.warning(f"{message} | SLOW_TURN")
        else:
            logger.info(message)


def _format_fields(fields: Dict[str, Any]) -> str:
    """Render metadata as ` key=value` pairs, or an empty string when absent."""
    if not fields:
        return ""
    return " " + " ".join(f"{key}={value}" for key, value in fields.items())


def new_turn_id() -> str:
    """Short, human-quotable correlation id. Eight hex chars is enough to grep."""
    return uuid.uuid4().hex[:8]


def start_turn(
    user_phone: str,
    *,
    source: str,
    gateway_timestamp: Any = None,
    message_preview: str = "",
    turn_id: Optional[str] = None,
) -> TurnTrace:
    """
    Begin a turn and make it the current one for this async context.

    The returned trace is also stored in a ContextVar, so code further down the
    call stack can reach it through :func:`current_turn` without threading an
    argument through every signature.
    """
    trace = TurnTrace(
        turn_id=turn_id or new_turn_id(),
        user_phone=str(user_phone or "unknown"),
        source=source,
        gateway_lag=gateway_lag_seconds(gateway_timestamp),
        message_preview=message_preview[:80],
    )
    _current_turn.set(trace)

    lag_text = "unknown" if trace.gateway_lag is None else f"{trace.gateway_lag:.3f}s"
    logger.info(
        f"{TAG}[{trace.turn_id}] START phone={trace.user_phone} source={source} "
        f"gateway_lag={lag_text} preview={trace.message_preview!r}"
    )
    if trace.gateway_lag is not None and trace.gateway_lag >= SLOW_TURN_SECONDS:
        # Worth its own line: this delay is upstream of the application, so it
        # points at replica cold start or gateway queueing rather than at code.
        logger.warning(
            f"{TAG}[{trace.turn_id}] HIGH_GATEWAY_LAG {trace.gateway_lag:.3f}s for "
            f"{trace.user_phone} - the message was already this old when it "
            f"reached this process (cold start or upstream gateway delay)"
        )
    return trace


def stamp_turn(payload: Dict[str, Any], trace: Optional[TurnTrace] = None) -> str:
    """
    Write the turn identity into ``payload`` so it survives serialisation.

    Returns the turn id that was stamped. A payload that already carries one
    keeps it, so a webhook retried by the gateway stays on the same turn id
    rather than fragmenting into several.
    """
    trace = trace or _current_turn.get()
    existing = payload.get(TURN_ID_FIELD)
    turn_id = str(existing) if existing else (trace.turn_id if trace else new_turn_id())
    payload[TURN_ID_FIELD] = turn_id
    if RECEIVED_AT_FIELD not in payload:
        payload[RECEIVED_AT_FIELD] = trace.started_at if trace else time.time()
    return turn_id


def resume_turn(payload: Dict[str, Any], *, source: str, user_phone: str = "") -> TurnTrace:
    """
    Rebuild the turn a stamped payload belongs to and make it current.

    ``started_perf`` is rewound by however long the payload has been in flight,
    so ``elapsed`` keeps measuring from the moment the webhook arrived rather
    than restarting at zero on the far side of the queue. That is what makes the
    summary line's ``in_app`` figure the real in-application latency instead of
    just the final leg.
    """
    turn_id = str(payload.get(TURN_ID_FIELD) or new_turn_id())
    received_at = payload.get(RECEIVED_AT_FIELD)
    phone = str(user_phone or payload.get("from") or "unknown").lstrip("+")

    trace = TurnTrace(
        turn_id=turn_id,
        user_phone=phone,
        source=source,
        gateway_lag=gateway_lag_seconds(payload.get("timestamp")),
        message_preview=str(payload.get("content", ""))[:80],
    )
    if isinstance(received_at, (int, float)):
        in_flight = max(0.0, time.time() - float(received_at))
        trace.started_at = float(received_at)
        trace.started_perf = time.perf_counter() - in_flight
    _current_turn.set(trace)
    return trace


def current_turn() -> Optional[TurnTrace]:
    """Return the turn attached to this async context, if any."""
    return _current_turn.get()


def current_turn_id() -> str:
    """Turn id for log prefixes, or ``'-'`` when no turn is active."""
    trace = _current_turn.get()
    return trace.turn_id if trace else "-"


def set_current_turn(trace: Optional[TurnTrace]) -> Token:
    """Attach ``trace`` to this context and return a token for restoring it."""
    return _current_turn.set(trace)


def reset_current_turn(token: Token) -> None:
    """Restore whatever trace was current before :func:`set_current_turn`."""
    try:
        _current_turn.reset(token)
    except ValueError:
        # The token belongs to a different context, which happens when a turn is
        # started in one task and reset in another. Clearing is the safe result.
        _current_turn.set(None)


@contextmanager
def bind_turn(trace: Optional["TurnTrace"]) -> Iterator[Optional["TurnTrace"]]:
    """
    Re-attach an existing trace, for code that crosses a task boundary.

    ``asyncio.create_task`` copies the context at creation time, so a trace
    created after the task started -- or one rebuilt from Redis in another
    worker -- has to be bound explicitly.
    """
    token = set_current_turn(trace)
    try:
        yield trace
    finally:
        reset_current_turn(token)


@contextmanager
def stage(name: str, **fields: Any) -> Iterator[None]:
    """
    Time a block and add it to the current turn's timeline.

    Works for both sync and async bodies: it only measures wall time around the
    ``with`` block, so ``async with`` is unnecessary and ``await`` inside the
    block is timed correctly.

    A stage that raises is still recorded, tagged as failed, and the exception is
    re-raised untouched -- the timeline must show where a turn died, and tracing
    must not change control flow.
    """
    trace = _current_turn.get()
    turn_id = trace.turn_id if trace else "-"
    detail = _format_fields(fields)
    logger.debug(f"{TAG}[{turn_id}] -> {name}{detail}")
    started = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        duration = time.perf_counter() - started
        if trace:
            trace.record(f"{name}!", duration)
        logger.error(
            f"{TAG}[{turn_id}] xx {name} failed after {duration * 1000:.0f}ms: "
            f"{type(exc).__name__}: {exc}{detail}"
        )
        raise
    else:
        duration = time.perf_counter() - started
        if trace:
            trace.record(name, duration)
        if duration >= SLOW_STAGE_SECONDS:
            logger.warning(
                f"{TAG}[{turn_id}] <- {name} SLOW {duration * 1000:.0f}ms{detail}"
            )
        else:
            logger.debug(
                f"{TAG}[{turn_id}] <- {name} {duration * 1000:.0f}ms{detail}"
            )


def mark(name: str, **fields: Any) -> None:
    """Record a checkpoint on the current turn, or do nothing without one."""
    trace = _current_turn.get()
    if trace:
        trace.mark(name, **fields)
    else:
        logger.debug(f"{TAG}[-] mark={name}{_format_fields(fields)}")


def annotate(**fields: Any) -> None:
    """Attach context to the current turn's summary line, if a turn is active."""
    trace = _current_turn.get()
    if trace:
        trace.annotate(**fields)


def finish_turn(outcome: str = "complete", **fields: Any) -> None:
    """Close the current turn, if one is active."""
    trace = _current_turn.get()
    if trace:
        trace.finish(outcome, **fields)
