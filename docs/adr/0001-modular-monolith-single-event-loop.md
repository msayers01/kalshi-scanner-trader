# ADR 0001 — Modular monolith on a single asyncio event loop

**Status:** accepted (2026-10-18)

## Context

We need a scanner, an auto-trader, a web UI backend, and a Discord bot, plus
an LLM gate. The operator runs everything on one PC. The latency that matters
is engine → exchange and engine → screen, measured in milliseconds to
seconds. The kill switch must be immediate and reliable.

## Decision

Everything runs in one Python process and one asyncio loop. The engine,
FastAPI (embedded uvicorn), and discord.py run as supervised tasks.
Components communicate through an in-process event bus. Module boundaries are
enforced by a dependency rule, checked by `tests/test_architecture.py`.

## Consequences

- (+) No IPC or broker to operate. A kill switch press is a function call.
  State is consistent without distributed coordination.
- (+) One `kst run` starts everything.
- (−) A CPU-heavy task can stall the loop. Rule: no blocking work on the
  loop; use `asyncio.to_thread`. Loop lag is monitored.
- (−) A process crash takes everything down. Mitigations: exchange-side order
  TTLs, `cancel_order_on_pause`, order groups, and a persisted kill state.
- The bus interface lets us move a component out of process later (e.g. the
  Discord bot over Redis or NATS) without touching domain code.
