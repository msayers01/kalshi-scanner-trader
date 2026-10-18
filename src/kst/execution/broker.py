"""Broker port. Implementations: PaperBroker (simulated), LiveBroker (Kalshi demo/prod).

Brokers report asynchronous results through the BrokerListener bound by the OMS. An ambiguous create
must raise OrderSubmissionUnknownError so the OMS retains exposure until exchange reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kst.core.fixedpoint import Price, Qty
from kst.core.types import OrderStatus
from kst.domain.orders import Fill, Order


class OrderSubmissionUnknownError(RuntimeError):
    """The exchange may have accepted a create; never release risk or automatically replace it."""


class BrokerListener(Protocol):
    def on_broker_order(
        self, coid: str, status: OrderStatus, *, exchange_id: str | None = None, reason: str = ""
    ) -> None: ...

    def on_broker_fill(self, fill: Fill) -> None: ...


@dataclass(slots=True)
class BrokerSnapshot:
    balance_cents: int | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)  # parse_position() rows
    open_orders: list[Order] = field(default_factory=list)


class Broker(Protocol):
    name: str

    def bind(self, listener: BrokerListener) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def place(self, order: Order) -> None: ...

    async def cancel(self, order: Order) -> None: ...

    async def amend(self, order: Order, price: Price, qty: Qty, expiration_ts: int | None = None) -> bool: ...

    async def cancel_all(self) -> int: ...

    async def snapshot(self) -> BrokerSnapshot: ...
