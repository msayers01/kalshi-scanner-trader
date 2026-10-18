# ADR 0005 — Paper and replay share the live code path

**Status:** accepted (2026-10-18)

## Context

Profit claims must be validated before real capital is at risk. Separate
backtest code drifts from production code.

## Decision

`PaperBroker` simulates fills against live (or recorded) books and public
trades:

- Takers walk the book.
- Makers fill FIFO, with queue-ahead initialized from the visible size at the
  price.
- Fills also happen when the market trades through our price.

Replay feeds recorded data through the *same* `MarketDataService`,
strategies, risk, OMS, and portfolio code, with a `ManualClock`. Each mode
has its own DB file.

## Consequences

Results are comparable across paper, replay, and live. The paper fill model
is optimistic for makers in thin books; this is documented, and the queue
model is conservative by default.
