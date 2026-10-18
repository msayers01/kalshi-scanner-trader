import { useEffect, useState } from "react";
import { api } from "../api";
import { qty, usd } from "../format";

type Row = {
  strategy: string; orders: number; rejected: number; fills: number; filled_contracts: number;
  order_fill_rate: number | null; gross_realized_usd: number; fees_usd: number; net_realized_usd: number;
  closed_contracts: number; win_rate: number | null; expected_entry_usd: number | null;
  expected_contracts: number; settled_contracts: number; settled_net_usd: number | null;
  paired_settled_contracts: number; settled_expected_usd: number | null; settled_actual_usd: number | null;
  slippage_cents: number | null; slippage_contracts: number; markout_60s_cents: number | null; markout_contracts: number;
};
type Data = { mode: string; as_of_ms: number; first_fill_ms: number | null; totals: Row; strategies: Row[]; unsupported_settlements: number };
const percent = (n: number | null) => n === null ? "—" : `${(n * 100).toFixed(1)}%`;
const cents = (n: number | null) => n === null ? "—" : `${n.toFixed(2)}¢`;

export function Scorecard() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await api.get<Data>("/api/scorecard");
        if (active) { setData(next); setError(""); }
      } catch (e) { if (active) setError(String(e)); }
      finally { if (active) timer = setTimeout(load, 10000); }
    };
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, []);
  return <div>
    {error && <div className="banner" role="alert">Scorecard unavailable: {error}{data ? " · Showing the last successful update." : ""}</div>}
    {!data && !error && <div className="empty">Loading paper scorecard…</div>}
    {data && <>
      <p className="muted small">{data.mode.toUpperCase()} · Lifetime{data.first_fill_ms ? ` since ${new Date(data.first_fill_ms).toLocaleString()}` : ""} · Updated {new Date(data.as_of_ms).toLocaleTimeString()} · Refreshes every 10 seconds</p>
      {!data.strategies.length ? <div className="empty">No paper orders yet. Trade in paper mode to begin building the scorecard.</div> : <>
        <table className="grid">
          <thead><tr><th className="left">Strategy</th><th>Orders / rejected</th><th>Order fill rate</th><th>Filled contracts</th><th>Gross realized</th><th>Fees</th><th>Net realized</th><th>Closed contracts</th><th>Win rate</th><th>Slippage</th><th>60s markout</th></tr></thead>
          <tbody>{[...data.strategies, data.totals].map(r => <tr key={r.strategy}>
            <td className="left">{r.strategy}</td><td>{r.orders} / {r.rejected}</td><td>{percent(r.order_fill_rate)}</td><td>{qty(r.filled_contracts)}</td>
            <td>{usd(r.gross_realized_usd)}</td><td>{usd(r.fees_usd)}</td><td className={r.net_realized_usd >= 0 ? "pos" : "neg"}>{usd(r.net_realized_usd)}</td>
            <td>{qty(r.closed_contracts)}</td><td>{percent(r.win_rate)}</td>
            <td title={`${qty(r.slippage_contracts)} contracts measured`}>{cents(r.slippage_cents)}</td>
            <td title={`${qty(r.markout_contracts)} contracts measured`}>{cents(r.markout_60s_cents)}</td>
          </tr>)}</tbody>
        </table>
        <p className="muted small">Expected versus realized at settlement · Same held-to-settlement contracts with recorded entry expectations. Early exits are excluded from this comparison.</p>
        <table className="grid">
          <thead><tr><th className="left">Strategy</th><th>Entry expectation</th><th>Entry coverage</th><th>Settled contracts</th><th>Settled net</th><th>Compared contracts</th><th>Expected net</th><th>Actual net</th><th>Actual − expected</th></tr></thead>
          <tbody>{[...data.strategies, data.totals].map(r => <tr key={r.strategy}>
            <td className="left">{r.strategy}</td><td>{usd(r.expected_entry_usd)}</td><td>{qty(r.expected_contracts)} contracts</td>
            <td>{qty(r.settled_contracts)}</td><td>{usd(r.settled_net_usd)}</td><td>{qty(r.paired_settled_contracts)}</td>
            <td>{usd(r.settled_expected_usd)}</td><td>{usd(r.settled_actual_usd)}</td>
            <td>{usd(r.settled_actual_usd === null || r.settled_expected_usd === null ? null : r.settled_actual_usd - r.settled_expected_usd)}</td>
          </tr>)}</tbody>
        </table>
      </>}
      <p className="muted small">FIFO attribution assigns exits and their fees to the entry strategy. Net realized subtracts all fill fees, including fees on open positions; it excludes unrealized P&amp;L. Win rate is weighted by closed contracts after fees; breakeven is not a win. Fill rate is orders with a fill / non-rejected orders, including still-resting orders.</p>
      <p className="muted small">Slippage is fill price versus the fresh decision midpoint; positive means a higher cost. 60s markout is the signed midpoint change from the fill price; negative indicates adverse selection. Both are quantity weighted in cents per contract. Markouts use the first fresh book observed 60–90 seconds after a fill. Hover for measured contract counts. Missing historical context is shown as —. Expectations are strategy estimates after modeled fees, adjusted for execution price; they are not a guarantee of profit.</p>
      {data.unsupported_settlements > 0 && <p className="banner">{data.unsupported_settlements} scalar settlements lack payout prices and are excluded from realized settlement results.</p>}
    </>}
  </div>;
}
