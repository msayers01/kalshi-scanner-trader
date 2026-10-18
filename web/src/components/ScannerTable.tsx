import { useMemo, useState } from "react";
import { cents, qty, signedCents, ttc } from "../format";
import { useStore } from "../store";
import type { ScanRow } from "../types";

const SIGNAL_CLASS: Record<string, string> = { ARB: "sig-arb", "EDGE+": "sig-pos", "EDGE-": "sig-neg", WIDE: "sig-wide" };

export function ScannerTable() {
  const scan = useStore((s) => s.scan);
  const selected = useStore((s) => s.selected);
  const select = useStore((s) => s.select);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState<string>("");
  const [edgeOnly, setEdgeOnly] = useState(false);

  const cats = useMemo(() => [...new Set((scan?.rows ?? []).map((r) => r.category).filter(Boolean))].sort(), [scan]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (scan?.rows ?? []).filter(
      (r) =>
        (!cat || r.category === cat) &&
        (!edgeOnly || r.signals.some((s) => s.startsWith("EDGE") || s === "ARB")) &&
        (!needle || r.ticker.toLowerCase().includes(needle) || r.title.toLowerCase().includes(needle)),
    );
  }, [scan, q, cat, edgeOnly]);

  return (
    <section className="panel scanner">
      <div className="panel-head">
        <h2>Scanner</h2>
        <span className="muted small">
          {scan ? `${scan.universe} markets · ${scan.priced} priced · ${scan.arbs.length} arbs` : "waiting for data…"}
        </span>
        <div className="spacer" />
        <input className="search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">All categories</option>
          {cats.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <label className="toggle small">
          <input type="checkbox" checked={edgeOnly} onChange={(e) => setEdgeOnly(e.target.checked)} />
          Signals only
        </label>
      </div>
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th className="left">Market</th>
              <th>Bid</th>
              <th>Ask</th>
              <th title="Model fair value combined with the market (cents)">Fair</th>
              <th title="Best after-fee taker edge per contract">Edge</th>
              <th>Vol 24h</th>
              <th>Closes</th>
              <th className="left">Signals</th>
              <th title="LLM verdict">LLM</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row key={r.ticker} r={r} active={r.ticker === selected} onClick={() => select(r.ticker)} />
            ))}
          </tbody>
        </table>
        {scan && rows.length === 0 && <div className="empty">No markets match the filters.</div>}
      </div>
    </section>
  );
}

function Row({ r, active, onClick }: { r: ScanRow; active: boolean; onClick: () => void }) {
  const edge = r.edge;
  return (
    <tr className={active ? "active" : undefined} onClick={onClick}>
      <td className="left market">
        <div className="title" title={r.title}>
          {r.title}
        </div>
        <div className="ticker">{r.ticker}</div>
      </td>
      <td>{cents(r.bid, true)}</td>
      <td>{cents(r.ask, true)}</td>
      <td title={r.model || undefined}>{r.q !== null ? cents(r.q) : "—"}</td>
      <td className={edge !== null && edge > 0 ? "pos" : edge !== null ? "neg" : ""}>{signedCents(edge)}</td>
      <td>{qty(r.vol24h)}</td>
      <td>{ttc(r.close_s)}</td>
      <td className="left">
        {r.signals.slice(0, 3).map((s) => (
          <span key={s} className={`sig ${SIGNAL_CLASS[s] ?? ""}`}>
            {s}
          </span>
        ))}
      </td>
      <td className={r.llm === "E" ? "pos" : r.llm === "S" ? "neg" : "muted"}>{r.llm ?? "·"}</td>
    </tr>
  );
}
