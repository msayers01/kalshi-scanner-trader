# Reliability and recovery

This pass hardens local persistence, paper restarts and worker/resource lifecycle.
It does not establish live/demo accounting correctness across eventually consistent
exchange snapshots; that acceptance work remains in `EXECUTION_VALIDATION.md`.
Run one engine per database; report commands can read concurrently. The repository
flush lock coordinates tasks within that engine, not multiple engine processes.

## Persistence guarantees

The repository snapshots order and JSON state when events are queued. Later
mutations cannot silently change an earlier checkpoint. A single flush lock orders
writer ticks, explicit checkpoints and shutdown writes. Each batch commits order
rows, fills, portfolio/risk state and audit rows in one SQLite transaction.

If a batch fails, it is restored to the buffers. Newer queued order/KV values take
precedence; journal rows retain their order. The error propagates to the supervisor
instead of being counted and ignored. Shutdown also reports a final flush failure.

A normal cancellation of the writer waits for its active transaction to complete;
the repository then drains any newer queued state during shutdown. This prevents
the prior cancellation path from discarding a drained, uncommitted batch. It cannot
protect memory buffers from a forced process termination.

SQLite uses WAL with `synchronous=FULL`. This adds a WAL sync at commit to improve
durability of acknowledged checkpoints; see the [SQLite synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).
Durability still depends on the storage stack honoring sync requests.

The background flush interval remains 0.5 seconds. A forced exit can lose events
that have not committed. Paper checkpoint/fill consistency is tested at transaction
boundaries; this is not a promise that every emitted event is immediately durable.
No schema changes or migrations are introduced.

## Kill and worker failures

Kill sets the in-memory engine to `killed`, disables autotrading, queues a kill-intent
audit, and flushes the checkpoint before awaiting exchange cancellation. With
healthy storage, a successful kill response therefore has a committed kill state.

If that checkpoint fails, cancellation is still attempted. The response has
`ok: false` and `persisted: false`, and a critical alert explains the failure.
The process remains killed, but a failed save cannot guarantee that state after
restart. If exchange cancellation fails, the command raises, emits a critical
alert, and retains local orders/exposure as described in execution validation.

The database writer is a critical worker. Any critical worker's exception,
unexpected return or unexpected cancellation invokes the kill handler immediately,
before worker retries with exponential backoff. Shutdown cancellation is expected
and does not trigger another kill. Workers can recover within their retry budget;
trading remains killed until an operator resumes it.

Resume is refused while persistence reports a write error or a failed writer.
Repair storage and let the supervised writer restart successfully. If its retry
budget is exhausted, restart the app after repairing storage. A successful ad hoc
flush alone does not clear a failed-writer flag.

## Paper restart

Paper fills now have deterministic SHA-256 IDs derived from the client order ID
and fill sequence. Distinct orders from separate sessions no longer reuse `pf-1`,
`pf-2`, etc. Replay IDs remain deterministic when client IDs and inputs match.

When a paper portfolio checkpoint is restored, the app loads committed fill IDs
into both OMS and portfolio deduplication. Re-delivering a committed fill cannot
add its quantity, fees or cash movement again. The OMS also refuses another
submission using an already tracked client order ID.

Existing history overwritten by old repeated paper IDs is not reconstructed.
Paper maker queue position is not restored. Live/demo restore continues to use
exchange snapshots and still needs the reconciliation work listed in the roadmap.

## Health and cleanup

`GET /api/health` and the status payload's `health.db` expose:

| Field | Meaning |
|---|---|
| `healthy` | No recorded outstanding write error or failed writer |
| `pending` | Buffered items plus items in the active transaction |
| `inflight` | Items in the active transaction |
| `written` | Successfully committed items this session |
| `errors` | Failed flush attempts this session |
| `last_error` | Bounded first-line cause; cleared on a successful batch |
| `last_flush_ms` | Last successful batch completion timestamp |
| `writer_failed` | Worker failed and has not restarted |

Startup failures run cleanup for resources already initialized. Shutdown attempts
broker cleanup, recorder close, repository flush/close and REST close even if an
earlier cleanup fails. Repository shutdown removes its bus subscriptions.
Supervisor shutdown consumes completed task exceptions and reports workers that
exceed its grace period instead of silently treating them as stopped.

These backend changes take effect when the process starts with the updated code.

## Verification

```powershell
uv run --no-sync pytest tests/test_reliability.py -q --junitxml=data/reliability-validation.xml
uv run --no-sync pytest -q
```

Tests use isolated temporary SQLite databases and failure injection. They cover
cancelled and concurrent writes, transaction retry, mutable payloads, durable kill
ordering, disk-failure cancellation, immediate critical escalation, explicit
resume after writer recovery, three paper sessions with duplicate delivery, startup
and shutdown errors, duplicate submissions, and actual SQLite durability settings.
No exchange requests or account changes are made by these tests.
