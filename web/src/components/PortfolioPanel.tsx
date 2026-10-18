import { usd } from "../format";
import { useStore } from "../store";
import { PortfolioChart } from "./PortfolioChart";

/** Right-hand panel when no market is selected: the equity chart plus the account's key numbers. */
export function PortfolioPanel() {
  const p = useStore((s) => s.portfolio);
  const drawdown = p && p.peak_equity > 0 ? p.equity - p.peak_equity : null;
  return (
    <section className="panel market portfolio">
      <PortfolioChart />
      <div className="pstats">
        <Stat label="Cash" value={usd(p?.cash)} />
        <Stat label="Exposure" value={usd(p?.exposure)} />
        <Stat label="Unrealized" value={usd(p?.unrealized)} tone={p ? (p.unrealized >= 0 ? "pos" : "neg") : undefined} />
        <Stat label="Today" value={usd(p?.pnl_today)} tone={p ? (p.pnl_today >= 0 ? "pos" : "neg") : undefined} />
        <Stat label="Realized today" value={usd(p?.realized_today)} />
        <Stat label="Fees today" value={usd(p ? -p.fees_today : null)} />
        <Stat label="Positions" value={p ? String(p.n_positions) : "—"} />
        <Stat label="From peak" value={usd(drawdown)} tone={drawdown !== null && drawdown < 0 ? "neg" : undefined} />
      </div>
      <div className="muted small phint">Select a market in the scanner to trade it.</div>
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div className="pstat">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}
