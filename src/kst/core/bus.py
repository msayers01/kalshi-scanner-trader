"""In-process event bus.

* `subscribe(EventType, handler)` — synchronous handler, called inline on publish. Must be fast and
  non-blocking (schedule tasks for slow work). Exceptions are logged, never propagated to the publisher.
* `stream(*types)` — bounded async queue for slow consumers (web hub, Discord, DB writer). On overflow
  the oldest item is dropped and counted; size queues for critical consumers generously.

Dispatch walks the event's MRO, so subscribing to `Event` receives everything.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from kst.core.events import Event
from kst.core.logging import get_logger

log = get_logger(__name__)

E = TypeVar("E", bound=Event)
Handler = Callable[[Any], None]


class Subscription:
    """Async iterator over published events of the selected types."""

    def __init__(self, bus: EventBus, types: tuple[type[Event], ...], maxsize: int) -> None:
        self._bus = bus
        self._types = types
        self._buf: deque[Event] = deque()
        self._maxsize = maxsize
        self._waiter: asyncio.Future[None] | None = None
        self.dropped = 0
        self.closed = False

    def _push(self, evt: Event) -> None:
        if self.closed:
            return
        if len(self._buf) >= self._maxsize:
            self._buf.popleft()
            self.dropped += 1
            if self.dropped in (1, 100) or self.dropped % 1000 == 0:
                log.warning(
                    "bus_subscription_overflow", dropped=self.dropped, types=[t.__name__ for t in self._types]
                )
        self._buf.append(evt)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def get_nowait(self) -> Event | None:
        return self._buf.popleft() if self._buf else None

    async def get(self) -> Event:
        while not self._buf:
            if self.closed:
                raise StopAsyncIteration
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._buf.popleft()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def close(self) -> None:
        self.closed = True
        self._bus._remove_stream(self)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._streams: dict[type[Event], list[Subscription]] = defaultdict(list)
        self.published = 0

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)

        return unsubscribe

    def stream(self, *types: type[Event], maxsize: int = 10_000) -> Subscription:
        sub = Subscription(self, types or (Event,), maxsize)
        for t in sub._types:
            self._streams[t].append(sub)
        return sub

    def _remove_stream(self, sub: Subscription) -> None:
        for t in sub._types:
            if sub in self._streams[t]:
                self._streams[t].remove(sub)

    def publish(self, evt: Event) -> None:
        self.published += 1
        for cls in type(evt).__mro__:
            if cls is object:
                break
            for h in tuple(self._handlers.get(cls, ())):
                try:
                    h(evt)
                except Exception:
                    log.exception(
                        "bus_handler_error",
                        event_type=type(evt).__name__,
                        handler=getattr(h, "__qualname__", repr(h)),
                    )
            for s in tuple(self._streams.get(cls, ())):
                s._push(evt)
