import { useEffect, useState } from "react";
import { api } from "../api";
import { closePosition } from "../closePosition";
import { cents, qty, time, usd } from "../format";
import { useStore } from "../store";
import { Scorecard } from "./Scorecard";

type Tab = "positions" | "orders" | "fills" | "risk" | "activity" | "llm" | "scorecard";

export function BottomTabs() {
  const [tab, setTab] = useState<Tab>("positions");
  const npos = useStore((s) => s.positions.length);
  const nord = useStore((s) => s.orders.filter((o) => o.status === "resting").length);
  const tabs: [Tab, string][] = [
    ["positions", `Positions (${npos})`],
    ["orders", `Orders (${nord})`],
    ["fills", "Fills"],
    ["scorecard", "Scorecard"],
    ["risk", "Risk limits"],
    ["activity", "Activity"],
    ["llm", "LLM"],
  ];
  return (
    <section className="panel bottom">
      <div className="tabs">
        {tabs.map(([k, label]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
        <div className="spacer" />
        <PnlStrip />
      </div>
      <div className="tab-body">
        {tab === "positions" && <Positions />}
        {tab === "orders" && <Orders />}
        {tab === "fills" && <Fills />}
        {tab === "scorecard" && <Scorecard />}
        {tab === "risk" && <RiskLimits />}
        {tab === "activity" && <Activity />}
        {tab === "llm" && <Llm />}
      </div>
    </section>
  );
}

function PnlStrip() {
  const p = useStore((s) => s.portfolio);
  if (!p) return null;
  const rt = p.round_trips_today;
  return (
    <span className="muted small">
      realized {usd(p.realized_today)} · fees {usd(-p.fees_today)} · trades {p.trades_today} · win {rt ? `${Math.round((p.wins_today / rt) * 100)}%` : "—"}
    </span>
  );
}

function Positions() {
  const positions = useStore((s) => s.positions);
  const select = useStore((s) => s.select);
  const [closing, setClosing] = useState<string | null>(null);
  if (!positions.length) return <div className="empty">Flat.</div>;
  async function close(p: (typeof positions)[number]) {
    setClosing(p.ticker);
    try {
      await closePosition(p);
    } finally {
      setClosing(null);
    }
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th className="left">Market</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Avg</th>
          <th>Mark</th>
          <th>Exposure</th>
          <th>uPnL</th>
          <th>Strategy</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => {
          const avg = p.avg_price === null ? null : p.outcome === "yes" ? p.avg_price : 1 - p.avg_price;
          const mark = p.mark === null ? null : p.outcome === "yes" ? p.mark : 1 - p.mark;
          return (
            <tr key={p.ticker} onClick={() => select(p.ticker)}>
              <td className="left market">
                <div className="title">{p.title || p.ticker}</div>
                <div className="ticker">{p.ticker}</div>
              </td>
              <td className={p.outcome === "yes" ? "yes" : "no"}>{p.outcome.toUpperCase()}</td>
              <td>{qty(Math.abs(p.qty))}</td>
              <td>{cents(avg)}</td>
              <td>{cents(mark)}</td>
              <td>{usd(p.exposure)}</td>
              <td className={p.unrealized !== null && p.unrealized >= 0 ? "pos" : "neg"}>{usd(p.unrealized)}</td>
              <td className="muted">{p.strategy}</td>
              <td>
                <button
                  className="btn tiny danger ghost"
                  disabled={closing !== null}
                  title="Exit the whole position at market"
                  onClick={(e) => {
                    e.stopPropagation(); // don't also select the row
                    void close(p);
                  }}
                >
                  {closing === p.ticker ? "Closing…" : "Sell all"}
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Orders() {
  const orders = useStore((s) => s.orders);
  if (!orders.length) return <div className="empty">No orders yet.</div>;
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Time</th>
          <th className="left">Market</th>
          <th>Side</th>
          <th>Price</th>
          <th>Filled</th>
          <th>Status</th>
          <th>Strategy</th>
          <th className="left">Note</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {orders.map((o) => (
          <tr key={o.coid}>
            <td>{time(o.updated_ms || o.created_ms)}</td>
            <td className="left ticker">{o.ticker}</td>
            <td className={o.side === "bid" ? "yes" : "no"}>{o.side === "bid" ? "YES" : "NO"}</td>
            <td>{cents(o.side === "bid" ? o.price : 1 - o.price)}</td>
            <td>
              {qty(o.filled)}/{qty(o.qty)}
            </td>
            <td className={`st-${o.status}`}>{o.status}</td>
            <td className="muted">{o.strategy}</td>
            <td className="left muted small" title={o.reason}>
              {o.reason.slice(0, 60)}
            </td>
            <td>
              {o.status === "resting" && (
                <button className="btn tiny" onClick={() => api.del(`/api/orders/${o.coid}`).catch((e) => alert(String(e)))}>
                  Cancel
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Fills() {
  const live = useStore((s) => s.fills);
  const [hist, setHist] = useState<typeof live>([]);
  useEffect(() => {
    api.get<typeof live>("/api/fills?n=200").then(setHist).catch(() => undefined);
  }, []);
  const seen = new Set(live.map((f) => f.id));
  const fills = [...live, ...hist.filter((f) => !seen.has(f.id))];
  if (!fills.length) return <div className="empty">No fills yet.</div>;
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Time</th>
          <th className="left">Market</th>
          <th>Bought</th>
          <th>Price</th>
          <th>Qty</th>
          <th>Fee</th>
          <th>Liquidity</th>
          <th>Strategy</th>
        </tr>
      </thead>
      <tbody>
        {fills.map((f) => (
          <tr key={f.id}>
            <td>{time(f.ts_ms)}</td>
            <td className="left ticker">{f.ticker}</td>
            <td className={f.side === "bid" ? "yes" : "no"}>{f.side === "bid" ? "YES" : "NO"}</td>
            <td>{cents(f.side === "bid" ? f.price : 1 - f.price)}</td>
            <td>{qty(f.qty)}</td>
            <td>{usd(f.fee, 3)}</td>
            <td className="muted">{f.taker ? "taker" : "maker"}</td>
            <td className="muted">{f.strategy}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RiskLimits() {
  const limits = useStore((s) => s.limits);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  if (!limits) return <div className="empty">Loading…</div>;
  const changed = Object.entries(draft).filter(([k, v]) => v !== "" && Number(v) !== limits[k]);

  async function save() {
    const changes = Object.fromEntries(changed.map(([k, v]) => [k, Number(v)]));
    try {
      const r = await api.post<{ changed: Record<string, number> }>("/api/limits", { changes });
      setMsg(`Saved: ${Object.keys(r.changed).join(", ") || "no change"}`);
      setDraft({});
    } catch (e) {
      setMsg(String(e));
    }
  }

  return (
    <div>
      <div className="limits">
        {Object.entries(limits).map(([k, v]) => (
          <label key={k} className={draft[k] !== undefined && Number(draft[k]) !== v ? "dirty" : undefined}>
            <span>{k.replaceAll("_", " ")}</span>
            <input inputMode="decimal" value={draft[k] ?? String(v)} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="row">
        <button className="btn ok" disabled={!changed.length} onClick={save}>
          Save {changed.length ? `(${changed.length})` : ""}
        </button>
        <button className="btn" disabled={!changed.length} onClick={() => setDraft({})}>
          Reset
        </button>
        <span className="muted small">{msg}</span>
      </div>
    </div>
  );
}

function Activity() {
  const notes = useStore((s) => s.notes);
  if (!notes.length) return <div className="empty">Nothing yet.</div>;
  return (
    <ul className="notes">
      {notes.map((n, i) => (
        <li key={`${n.ts_ms}-${i}`} className={`note ${n.kind} lvl-${n.level}`}>
          <span className="muted">{time(n.ts_ms)}</span> <b>{n.title}</b> <span>{n.message}</span>
        </li>
      ))}
    </ul>
  );
}

function Llm() {
  const llm = useStore((s) => s.llm);
  const [packet, setPacket] = useState("");
  async function copyPacket() {
    const text = await api.get<string>("/api/llm/packet?top=20");
    setPacket(text);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* clipboard blocked: the packet is shown below for manual copy */
    }
  }
  const s = llm?.status;
  return (
    <div>
      <div className="row">
        <span>
          mode <b>{s?.mode ?? "—"}</b> · {s?.provider ?? "—"}:{s?.model ?? "—"} · calls {s?.calls_today ?? 0}/{s?.max_calls ?? 0} · tokens{" "}
          {s ? `${s.tokens_in}/${s.tokens_out}` : "—"} · pending {s?.pending ?? 0}
          {s?.last_error ? <span className="neg"> · {s.last_error}</span> : null}
        </span>
        <div className="spacer" />
        <button className="btn" onClick={copyPacket}>
          Copy LLM packet
        </button>
      </div>
      {packet && <textarea className="packet" readOnly value={packet} />}
      <table className="grid">
        <tbody>
          {(llm?.verdicts ?? [])
            .slice()
            .reverse()
            .map((v) => (
              <tr key={`${v.ticker}-${v.ts_ms}`}>
                <td>{time(v.ts_ms)}</td>
                <td className="left ticker">{v.ticker}</td>
                <td className={v.enter ? "pos" : "neg"}>{v.enter ? "ENTER" : "SKIP"}</td>
                <td>{v.confidence}%</td>
                <td className="left">{v.reason}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}
