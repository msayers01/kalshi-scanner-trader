# ADR 0002 — Integer fixed-point money in YES-price space

**Status:** accepted (2026-10-18)

## Context

Kalshi moved to fixed-point strings, with up to 4 dp for prices and 2 dp for
quantities, plus fractional contracts and per-market sub-cent tick grids.
Float accumulation errors in P&L and order prices are unacceptable. V2 orders
quote everything on the YES leg (`bid`/`ask`).

## Decision

- `Price` is an int in units of $0.0001. `Qty` is an int in units of 0.01
  contract. `Micros` is an int in units of $0.000001, and `Price × Qty`
  equals `Micros` exactly.
- Every order, book, and position is in YES-price space. A position is a
  signed `Qty` (+YES / −NO).
- Floats are allowed only for probabilities and model math. Prices are
  converted back through `PriceGrid.snap_down/snap_up`.

## Consequences

Exact accounting and one book representation. The NO leg is derived by
`1 − p`. Every parse and format happens in `core/fixedpoint.py`.
