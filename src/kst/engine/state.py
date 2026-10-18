"""Engine state machine (RUNNING / PAUSED / KILLED), persisted so KILLED survives restarts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import EngineStateChanged
from kst.core.types import Actor, EngineState

_ALLOWED: dict[EngineState, set[EngineState]] = {
    EngineState.RUNNING: {EngineState.PAUSED, EngineState.KILLED},
    EngineState.PAUSED: {EngineState.RUNNING, EngineState.KILLED},
    EngineState.KILLED: {
        EngineState.RUNNING,
        EngineState.PAUSED,
    },  # resume from KILLED is explicit operator action
}


class EngineStateStore:
    KEY = "engine_state"

    def __init__(
        self, bus: EventBus, clock: Clock, persist: Callable[[str, Any], None] | None = None
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.persist = persist
        self.state = EngineState.RUNNING
        self.reason = ""
        self.since_ms = clock.now_ms()

    def restore(self, saved: dict[str, Any] | None) -> None:
        if saved and saved.get("state") in EngineState._value2member_map_:
            self.state = EngineState(saved["state"])
            self.reason = saved.get("reason", "")
            self.since_ms = int(saved.get("since_ms", self.since_ms))

    def get(self) -> EngineState:
        return self.state

    def set(self, new: EngineState, actor: Actor, reason: str = "") -> bool:
        old = self.state
        if new == old:
            return False
        if new not in _ALLOWED[old]:
            raise ValueError(f"illegal engine transition {old} → {new}")
        self.state, self.reason, self.since_ms = new, reason, self.clock.now_ms()
        if self.persist:
            self.persist(
                self.KEY,
                {"state": new.value, "reason": reason, "since_ms": self.since_ms, "actor": actor.value},
            )
        self.bus.publish(EngineStateChanged(self.since_ms, old, new, actor, reason))
        return True
