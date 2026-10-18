# Execution validation

Automated adapter validation is available in `tests/test_live_execution.py` and
`tests/test_kalshi_ws.py`. These run the production risk engine, OMS, LiveBroker,
REST serialization/signing and WebSocket client. HTTP is intercepted, signatures
use ephemeral test keys, and the WebSocket server binds only to loopback. There
are no exchange requests or account mutations. CI runs these with the full suite.

```powershell
uv run --no-sync pytest tests/test_live_execution.py tests/test_kalshi_ws.py -q --junitxml=data/execution-validation.xml
```

On Windows `--no-sync` avoids trying to replace a CLI executable held open by the
running paper UI. Install dev dependencies first with `uv sync --all-extras` while
the UI is stopped if pytest/respx are missing.

## Automated evidence

| Scenario | Checks |
|---|---|
| GTC placement | Integer-to-fixed-point conversion, post-only, TTL and cancel-on-pause |
| Risk gate | Rejected order never reaches REST |
| Create failures | Definitive rejection becomes terminal; conflict, transport loss, server failure and missing acknowledgement ID retain risk exposure and raise a critical alert |
| Fills | BID/ASK accounting, fees and attribution; duplicate WS/REST delivery in either order; different REST fill ID vs WS trade ID; both counterparties of one trade |
| IOC | Zero, partial and full fill; WS before acknowledgement; REST fallback; missing fills retain exposure; finalizer task cleanup on shutdown |
| Amend | Already-filled plus desired remaining quantity; exchange ID replacement; TTL refresh |
| Cancel | Not-found response, failed cancel, late fill after cancellation and stale order updates |
| Replacement | Failed cancellation cannot cause another order to be placed |
| Kill | Successful exchange-wide cancel; failed cancel retains local orders/exposure while the engine remains killed |
| Order group | Configured group must be created successfully before startup proceeds; group ID is attached to orders |
| Restart | Resting-order snapshot adoption and attribution of a subsequent fill without client ID |
| WebSocket | Signed real handshake, global/focus subscriptions, focus changes during subscribe, sequence-gap quarantine, resubscription, reconnect and shutdown |
| Market-data gap | Every affected book becomes unusable for risk checks until a fresh snapshot arrives |

REST fills include both `fill_id` and `trade_id`, while WS fills identify the trade.
LiveBroker emits a stable internal fill key, SHA-256 of `order_id:trade_id`, when available,
falling back to the parser's fill identifier otherwise. Pairing with order ID
prevents dropping one of our two fills when both counterparties belong to us.
The 64-character digest fits the existing fill table without a schema change.
See Kalshi's [REST fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)
and [WS fills](https://docs.kalshi.com/websockets/user-fills) payloads. Amend quantities
follow the documented [total-count semantics](https://docs.kalshi.com/api-reference/orders/amend-order-v2).

## Demo acceptance run — pending

The automated tests are not evidence of a successful demo-exchange session. No
Kalshi credentials were configured when this validation was implemented. The
roadmap demo checkbox remains open.

Use a dedicated demo account for account-wide cancellation tests. Configure its
demo key ID and local private-key path via `KST_KALSHI__KEY_ID` and
`KST_KALSHI__PRIVATE_KEY_PATH` in `.env` or the environment. Demo keys are separate
from production keys; see [Kalshi demo setup](https://docs.kalshi.com/getting_started/demo_env).
Never put the private key in a report or commit it.

The provided overlay selects demo hosts, a separate database and port 8788, small
limits, a two-contract order group and disabled autotrading:

```powershell
uv run --no-sync kst check --mode demo -c config/default.yaml -c examples/execution-demo.yaml
uv run --no-sync kst run --mode demo --no-autotrade --no-discord -c config/default.yaml -c examples/execution-demo.yaml
```

Verify the status displays `demo` at `http://127.0.0.1:8788/`. Select an active
market with a fresh book, valid price grid, sufficient time until close, and
counterparty liquidity. Submit all orders through the UI/control service so the
risk engine and OMS remain in the path. Do not bypass a risk rejection.

Record the following in a local report under `data/execution-demo/`, retaining
client/exchange IDs, timestamps, requests with secrets removed, and observed outcomes:

1. Place one post-only GTC contract, amend its price and remaining size, then cancel it.
   Confirm the exchange IDs and remaining count agree with local state.
2. Leave a manual GTC untouched past its TTL (manual quotes have no strategy slot
   and are not refreshed). Confirm exchange cancellation, then restart with a new
   resting order to check adoption and later fill attribution.
3. Send an IOC outside the spread and confirm zero fill/cancellation. With available
   counterparties, exercise a partial and full IOC. Confirm fill-driven completion,
   fees, signed position and cash against REST. A no-liquidity result does not pass
   the partial/full cases.
4. Interrupt WS delivery during a fill. Confirm reconnect subscriptions and REST
   recovery apply that fill once. Check fresh snapshots precede further entries.
5. Exercise the order group's rolling match limit. Confirm group-triggered
   cancellation/rejection on the exchange. Startup/attachment tests do not prove
   exchange-side enforcement. Use a new session/group after it trips.
6. With a resting order, invoke cancel-all/kill. Confirm the account has no resting
   orders, the engine remains killed, and local state matches. Cancel-all can also
   cancel newly placed orders during the following minute, per the
   [exchange documentation](https://docs.kalshi.com/api-reference/orders/cancel-all-orders).

## Limits and follow-up

Restart coverage currently proves order adoption/routing, not atomic portfolio
recovery. Exchange balance, positions and fills are separate eventually consistent
reads; startup snapshots can already include fills returned by the overlapping
REST poll. Persistent fill/checkpoint reconciliation must be validated and hardened
before claiming exactly-once accounting across restarts. In-memory deduplication
alone does not establish that guarantee.

Local persistence and paper-mode checkpoint/dedup recovery have since been
hardened and failure-tested; see [reliability](RELIABILITY.md). The live/demo
snapshot issue above remains open.

Uncertain creates remain pending and consume risk capacity until WS or a resting
snapshot resolves them. A create that immediately executed/canceled and is absent
from resting snapshots may need explicit order-history reconciliation. Missing IOC
fills similarly retain exposure and log `ioc_fills_missing`; they require recovery
or operator reconciliation. This is deliberate: missing evidence must not free
capacity for replacement orders. Demo results and these recovery cases must be
reviewed before live use.
