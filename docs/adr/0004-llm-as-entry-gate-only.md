# ADR 0004 — The LLM is an entry gate only, and is token-budgeted

**Status:** accepted (2026-10-18)

## Context

The operator wants LLM judgment (local Qwen or a cloud model) on which
markets to enter, without burning tokens. LLMs are poorly calibrated
probability estimators and slow. Quant models beat them on crypto and weather
pricing.

## Decision

The LLM returns ENTER or SKIP (plus an optional side bias and confidence) for
batches of pre-filtered candidates. By default only markets *without* a quant
model need a verdict. Verdicts are cached by state bucket, and calls are
batched, rate-limited, and capped by daily budgets. Pricing, sizing, strategy
choice, and exits are never delegated to the LLM.

## Consequences

Token use is bounded and small (under 65k tokens/day by default). The LLM
can veto but never increase risk. Advisory mode allows A/B evaluation from
the persisted decisions.
