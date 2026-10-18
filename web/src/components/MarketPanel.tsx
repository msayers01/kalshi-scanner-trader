import { useEffect, useState } from "react";
import { api } from "../api";
import { closePosition } from "../closePosition";
import { cents, pct, qty, ttc, usd } from "../format";
import { useStore } from "../store";
import type { MarketDetail } from "../types";
import { OrderBook } from "./OrderBook";
import { PortfolioPanel } from "./PortfolioPanel";
import { TradeTicket } from "./TradeTicket";

export function MarketPanel() {
  const selected = useStore((s) => s.selected);
  const select = useStore((s) => s.select);
  const book = useStore((s) => s.book);
  const orders = useStore((s) => s.orders);
  const positions = useStore((s) => s.positions);
  const [detail, setDetail] = useState<MarketDetail | null>(null);
  const [prefill, setPrefill] = useState<number | null>(null);
  const [showRules, setShowRules] = useState(false);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    setDetail(null);
    setPrefill(null);
    if (!selected) return;
    let alive = true;
    const load = () =>
      api
        .get<MarketDetail>(`/api/markets/${encodeURIComponent(selected)}`)
        .then((d) => alive && setDetail(d))
        .catch(() => undefined);
    load();
    const t = setInterval(load, 5000); // fair value / eval notes refresh; the book itself streams over WS
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [selected]);

  if (!selected) return <PortfolioPanel />;
  const live = orders.filter((o) => o.ticker === selected && ["pending_new", "resting", "pending_cancel"].includes(o.status));
  const pos = positions.find((p) => p.ticker === selected) ?? null;
  const fair = detail?.fair;
  const closeIn = detail?.close_ms ? (detail.close_ms - Date.now()) / 1000 : null;

  return (
    <section className="panel market">
      <div className="panel-head column">
        <button className="link back" onClick={() => select(null)}>
          ← Portfolio
        </button>
        <h2 title={detail?.title}>{detail ? `${detail.title}${detail.subtitle ? ` — ${detail.subtitle}` : ""}` : selected}</h2>
        <div className="muted small">
          {selected} · {detail?.category ?? ""} · closes {ttc(closeIn)} · fee {detail?.fee_type ?? "?"}
          {detail && detail.fee_multiplier !== 1 ? ` ×${detail.fee_multiplier}` : ""}
          {detail?.rules && (
            <button className="link" onClick={() => setShowRules((v) => !v)}>
              {showRules ? "hide rules" : "rules"}
            </button>
          )}
        </div>
        {showRules && <p className="rules">{detail?.rules}</p>}
      </div>

      <div className="market-body">
        <div className="market-left">
          <OrderBook book={book} onPick={setPrefill} />
        </div>
        <div className="market-right">
          <div className="facts">
            <Fact label="Fair (YES)" value={fair ? `${cents(fair.q)}¢ ±${cents(fair.sd)}` : "no model"} />
            <Fact label="Model" value={fair?.model ?? "—"} />
            <Fact label="Model weight" value={fair ? pct(fair.w_model, 0) : "—"} />
            <Fact label="LLM" value={detail?.llm ? `${detail.llm.enter ? "ENTER" : "SKIP"} ${detail.llm.confidence}% · ${detail.llm.reason}` : "—"} />
            <Fact label="Strategy" value={detail?.last_eval ? `${detail.last_eval.strategy}${detail.last_eval.gated ? ` (${detail.last_eval.gated})` : ""}` : "—"} />
            {detail?.last_eval?.note && <Fact label="Note" value={detail.last_eval.note} />}
            {fair?.detail && Object.keys(fair.detail).length > 0 && (
              <Fact label="Inputs" value={Object.entries(fair.detail).map(([k, v]) => `${k}=${String(v)}`).join("  ")} />
            )}
          </div>
          <TradeTicket ticker={selected} book={book} fair={fair?.q ?? null} feeMultiplier={detail?.fee_multiplier ?? 1} prefill={prefill} position={pos} />
          {pos && (
            <div className="position-box">
              <b>
                {pos.outcome.toUpperCase()} {qty(Math.abs(pos.qty))}
              </b>{" "}
              @ {pos.avg_price !== null ? cents(pos.outcome === "yes" ? pos.avg_price : 1 - pos.avg_price) : "—"}¢ · uPnL{" "}
              <span className={pos.unrealized !== null && pos.unrealized >= 0 ? "pos" : "neg"}>{usd(pos.unrealized)}</span> ·{" "}
              {pos.strategy}
              <button
                className="btn tiny danger ghost sell-all"
                disabled={closing}
                onClick={async () => {
                  setClosing(true);
                  try {
                    await closePosition(pos);
                  } finally {
                    setClosing(false);
                  }
                }}
              >
                {closing ? "Closing…" : "Sell all"}
              </button>
            </div>
          )}
          {live.length > 0 && (
            <table className="grid compact">
              <tbody>
                {live.map((o) => (
                  <tr key={o.coid}>
                    <td className="left">{o.side === "bid" ? "BUY YES" : "BUY NO"}</td>
                    <td>{cents(o.side === "bid" ? o.price : 1 - o.price)}¢</td>
                    <td>
                      {qty(o.filled)}/{qty(o.qty)}
                    </td>
                    <td className="muted">{o.strategy}</td>
                    <td>
                      <button className="btn tiny" onClick={() => api.del(`/api/orders/${o.coid}`).catch((e) => alert(String(e)))}>
                        Cancel
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <span className="muted">{label}</span>
      <span>{value}</span>
    </div>
  );
}
