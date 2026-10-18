# Paper-trading scorecard

Open the **Scorecard** tab at http://127.0.0.1:8787. It refreshes every ten seconds.
`uv run kst scorecard` prints the same lifetime paper metrics as JSON; the authenticated
read API is `GET /api/scorecard` (paper/replay only). The CLI always reads the paper database.
Reads use committed records, so updates may lag the repository's 0.5-second flush.

## Accounting and attribution

The scorecard replays the full persisted fill and settlement history using FIFO lots.
Closing and flattening P&L and closing fees belong to the strategy that opened each lot.
Execution statistics (fills, slippage, markout) belong to the strategy submitting the order.
A fill that reverses a position splits its actual fee between the closed and opened quantities.
All money accumulates in integer micros; conversions to dollars happen at the response boundary.

- Gross realized: P&L on closed contracts and settled lots.
- Net realized: gross realized less **all** fill fees, including fees on still-open lots.
  Unrealized P&L is excluded. FIFO interim realized P&L may differ from the portfolio's
  average-cost accounting. Totals agree when all positions have closed, given complete history.
- Win rate: profitable closed quantity / all closed quantity, using entry and exit fees.
  Breakeven is not a win. This is contract weighted, not a round-trip win rate.
- Order fill rate: orders receiving any fill / non-rejected orders. Includes still-resting
  orders; partially filled canceled orders count as filled. This is not a quantity fill ratio,
  which would require an amendment history instead of the current order snapshots.
- Entry expectation: strategy-provided net edge at the order's limit, adjusted by signed
  execution-price improvement, multiplied by the quantity actually opening exposure.
  This is a conservative strategy estimate, not a reconstructed fair-value forecast.
  Arbitrage expectations are the strategy's basket edge allocated across its legs.
- Settlement comparison: expected versus actual net P&L on **the same remaining lots** held
  through settlement. Early-exited quantities are removed pro rata from their lot expectation.
  Actual net uses the realized payout and the corresponding actual entry fees.
  The comparison includes only lots with recorded expectations; all settled net is also shown.
- Slippage: signed fill price minus fresh decision midpoint, quantity weighted, in cents per
  contract. Positive means higher cost, negative means improvement. Paying the spread naturally
  contributes to this measure. The decision midpoint is preserved through broker callbacks and
  updated on price/quantity amendments.
- 60s markout: signed fresh midpoint minus fill price, quantity weighted, in cents per contract.
  Negative indicates adverse selection. The first fresh, two-sided book observed at 60–90 seconds
  is used; this is a gross price measure before fees. The observation timestamp is persisted.

## Coverage and persistence

Historical orders/fills/fees/YES-NO settlements contribute immediately. Decision midpoints,
strategy expectations, scalar payout prices, and markouts start being captured when this
version runs. Missing values appear as `null` / **—**, never zero. Hover over slippage and markout
for the measured contract count; expectation and settlement coverage appear in the second table.
Legacy scalar settlements without payout prices are excluded and flagged as incomplete.

Context is appended through the existing write-behind audit journal under `scorecard_fill`,
`scorecard_markout`, and `scorecard_settlement`; no database schema change is required.
Pending markouts restore from that journal after a restart if still inside the 90-second window.
Fresh-book checks prevent a stale pre-restart book from becoming an observation. Expired
observations remain missing. Paper/replay recording does not change trading or risk decisions.

This initial implementation scans lifetime history for each request. Large histories may need
materialized summaries before increasing the refresh frequency. It does not claim statistical
significance or prove live profitability; use it to gather paper execution evidence before tuning.
