"""Supervisor for long-running asyncio tasks.

Each task is a coroutine factory. Non-critical tasks are restarted with exponential backoff; when a
*critical* task fails the supervisor immediately invokes `on_critical_failure` (wired to
ControlService.kill by the composition root), then retries the worker with backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kst.core.logging import get_logger

log = get_logger(__name__)

CoroFactory = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class TaskSpec:
    name: str
    factory: CoroFactory
    critical: bool = False
    max_restarts: int = 20
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    restarts: int = 0
    last_error: str = ""
    running: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class Supervisor:
    def __init__(self, on_critical_failure: Callable[[str, str], Awaitable[None]] | None = None) -> None:
        self._specs: dict[str, TaskSpec] = {}
        self._on_critical_failure = on_critical_failure
        self._stopping = False

    def add(self, name: str, factory: CoroFactory, *, critical: bool = False, max_restarts: int = 20) -> None:
        if name in self._specs:
            raise ValueError(f"duplicate task {name}")
        self._specs[name] = TaskSpec(name=name, factory=factory, critical=critical, max_restarts=max_restarts)

    def start(self) -> None:
        if any(s.task is not None for s in self._specs.values()) or self._stopping:
            raise RuntimeError("Supervisor is already started or stopped")
        for spec in self._specs.values():
            spec.task = asyncio.create_task(self._run(spec), name=f"sup:{spec.name}")

    async def _run(self, spec: TaskSpec) -> None:
        delay = spec.backoff_initial_s
        while not self._stopping:
            spec.running = True
            try:
                await spec.factory()
                if self._stopping:
                    return
                log.warning("task_exited", task=spec.name)
                spec.last_error = "Worker returned unexpectedly"
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                spec.last_error = "Worker was cancelled unexpectedly"
            except Exception as exc:
                spec.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("task_crashed", task=spec.name, restarts=spec.restarts)
            finally:
                spec.running = False
            if not spec.last_error:
                spec.last_error = "Worker returned unexpectedly"
            if spec.critical and self._on_critical_failure is not None:
                try:
                    await self._on_critical_failure(spec.name, spec.last_error)
                except Exception:
                    log.exception("critical_failure_handler_failed", task=spec.name)
            spec.restarts += 1
            if spec.restarts > spec.max_restarts:
                log.error("task_gave_up", task=spec.name, critical=spec.critical)
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, spec.backoff_max_s)

    def health(self) -> dict[str, dict[str, object]]:
        return {
            s.name: {
                "running": s.running,
                "restarts": s.restarts,
                "critical": s.critical,
                "last_error": s.last_error,
            }
            for s in self._specs.values()
        }

    async def stop(self, grace_s: float = 10.0) -> None:
        self._stopping = True
        tasks = [s.task for s in self._specs.values() if s.task is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=grace_s)
            for task in done:
                if not task.cancelled():
                    with contextlib.suppress(Exception):
                        task.result()  # consume worker errors; don't leave unobserved task exceptions
            if pending:
                names = ", ".join(sorted(t.get_name() for t in pending))
                raise TimeoutError(f"Workers did not stop within {grace_s}s: {names}")
