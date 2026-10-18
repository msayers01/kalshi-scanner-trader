# ADR 0003 — Strategies emit declarative targets; the OMS reconciles

**Status:** accepted (2026-10-18)

## Context

Imperative "place this, cancel that" strategy code is fragile across
reconnects, partial fills, rejections, and rate limiting.

## Decision

Each strategy evaluation returns a `Decision`: a set of desired resting quotes
(`QuoteTarget`s keyed by `(strategy, ticker, slot)`) and immediate IOC
`TakeIntent`s. The OMS diffs the desired quotes against live orders. It keeps
an order if it is within tolerance and amends or replaces it otherwise. It
cancels orders that are no longer wanted. Takes go through risk checks and are
sent once, idempotently, using `client_order_id`.

## Consequences

- (+) Self-healing. After any disruption, the next evaluation converges to the
  desired state.
- (+) Strategies are pure functions of state, so they are easy to test and
  replay.
- (−) Diff tolerances (price ticks, size %) and minimum quote life must be
  tuned to avoid churn and rate-limit burn.
