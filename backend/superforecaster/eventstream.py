"""A bounded, replayable event stream with per-subscriber fan-out.

One of these backs each live run. It knows nothing about forecasting — it is a ring
buffer, a sequence counter, and a set of queues — which is why it is testable without
starting an agent.

Three behaviours are worth knowing about before reading the code:

- **Publishing never blocks.** A slow or dead subscriber costs that one connection, not
  the event: the put is non-blocking and a full queue is dropped. The client reconnects
  and replays the gap with `?from_seq=`.
- **The buffer is bounded**, so a long run cannot exhaust memory. `replay` reports the
  hole rather than silently serving a timeline with its middle missing.
- **Deltas coalesce.** Token narration arrives one frame per token otherwise — thousands
  of frames each carrying three bytes of payload inside ninety bytes of envelope.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Generic, Iterable, TypeVar

T = TypeVar("T")

SUBSCRIBER_QUEUE_SIZE = 512
"""Per-connection backlog. A client that falls this far behind is dropped and resumes
from the buffer, rather than being allowed to stall the producer."""

COALESCE_SECONDS = 0.08

_EVERY_KEY: Any = object()
"""Sentinel for `flush`: every key, not just one. A distinct object rather than `None`,
because `None` is itself a valid key — it is what un-keyed events use."""


class EventStream(Generic[T]):
    """Sequenced events, buffered for replay and fanned out to live subscribers."""

    def __init__(self, buffer: int, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self.seq = 0
        self.dropped = 0
        self.events: deque[T] = deque(maxlen=buffer)
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[T]] = set()
        self._pending: dict[Any, tuple[str, float]] = {}

    # ---------- publishing ----------

    def publish(self, build: Callable[[int], T], key: Any = None) -> T:
        """Assign the next sequence number, buffer the event, and fan it out.

        `build` receives that number so the caller owns the event's shape — this class
        never needs to know what a forecast event looks like. Pending deltas for `key`
        are flushed first, so narration can never arrive after the event it preceded.
        """
        self.flush(key)
        return self._append(build)

    def publish_delta(
        self, delta: str, key: Any, build: Callable[[int, str, Any], T]
    ) -> None:
        """Buffer a token delta, emitting at most one frame per `COALESCE_SECONDS`.

        One buffer per key rather than one for the stream. When a stage fans out several
        agents narrate at once, and a single buffer would interleave their half-written
        sentences into one unreadable string.
        """
        self._build_delta = build
        now = time.monotonic()
        text, deadline = self._pending.get(key, ("", 0.0))
        if not text:
            deadline = now + COALESCE_SECONDS
        self._pending[key] = (text + delta, deadline)
        if now >= deadline:
            self.flush(key)

    def flush(self, key: Any = _EVERY_KEY) -> None:
        """Emit whatever `publish_delta` buffered, for one key or for all of them.

        A stage boundary passes no key at all — a barrier is a real barrier, and nothing
        is still narrating on the far side of it. An event *within* a key flushes only
        that key, or one column's half-written sentence gets spliced in front of
        another's tool call.
        """
        if not self._pending:
            return
        keys = (
            list(self._pending)
            if key is _EVERY_KEY
            else ([key] if key in self._pending else [])
        )
        for k in keys:
            text, _ = self._pending.pop(k, ("", 0.0))
            if text:
                self._append(lambda seq, t=text, kk=k: self._build_delta(seq, t, kk))

    def _append(self, build: Callable[[int], T]) -> T:
        self.seq += 1
        if len(self.events) == self.events.maxlen:
            self.dropped += 1
        event = build(self.seq)
        self.events.append(event)

        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)
        return event

    # ---------- subscribing ----------

    def subscribe(self) -> asyncio.Queue[T]:
        q: asyncio.Queue[T] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[T]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        """How many connections are currently attached.

        Read by `runs` to decide whether anyone is still watching. Counting live queues
        rather than tracking connects and disconnects separately means a subscriber
        dropped for falling behind — see `_append` — is counted as gone, which it is.
        """
        return len(self._subscribers)

    def replay(self, from_seq: int, seq_of: Callable[[T], int]) -> Iterable[T]:
        """Buffered events at or after `from_seq`.

        The caller is responsible for noticing a hole: `dropped` is non-zero and
        `oldest_seq` is past what was asked for.
        """
        return [e for e in self.events if seq_of(e) >= from_seq]

    def oldest_seq(self, seq_of: Callable[[T], int]) -> int:
        return seq_of(self.events[0]) if self.events else 0
