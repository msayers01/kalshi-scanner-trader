import { useEffect, useState } from "react";
import { api } from "../api";
import { usd } from "../format";
import { useStore } from "../store";
import type { Preset } from "../types";

/** Bankroll presets: pick your starting cash, get every risk limit scaled to it and a hands-off engine. */
export function Presets() {
  const status = useStore((s) => s.status);
  const positions = useStore((s) => s.positions);
  const orders = useStore((s) => s.orders);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [pick, setPick] = useState<Preset | null>(null);
  const [reset, setReset] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api
      .get<{ presets: Preset[] }>("/api/presets")
      .then((d) => setPresets(d.presets))
      .catch((e) => setMsg(String(e)));
  }, []);

  useEffect(() => {
    if (!pick) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && setPick(null);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pick, busy]);

  const active = status?.preset ?? null;
  const paper = status?.mode === "paper";
  const flat = positions.length === 0 && !orders.some((o) => ["pending_new", "resting", "pending_cancel"].includes(o.status));

  async function apply() {
    if (!pick) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.post<{ ok: boolean; reason?: string; autotrade?: boolean }>(`/api/presets/${pick.key}/apply`, { reset_paper: reset });
      setMsg(r.ok ? `${pick.label} applied${r.autotrade ? " · autotrade on" : ""}${reset ? " · paper balance reset" : ""}.` : (r.reason ?? "Not applied"));
      if (r.ok) setPick(null);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="presets">
      <div className="presets-head">
        <b>Presets</b>
        <span className="muted small">
          Pick your starting cash: every limit below scales to it and kst runs hands-off. Active: <b>{presets.find((p) => p.key === active)?.label ?? "custom"}</b>
          {status?.auto_resume ? " · auto-resume on" : ""}
        </span>
      </div>
      <div className="preset-cards" role="radiogroup" aria-label="Bankroll preset">
        {presets.map((p) => (
          <button
            key={p.key}
            role="radio"
            aria-checked={active === p.key}
            className={`preset-card ${active === p.key ? "on" : ""}`}
            onClick={() => {
              setPick(p);
              setReset(false);
              setMsg("");
            }}
            title={p.summary}
          >
            <span className="pc-label">{p.label}</span>
            <span className="pc-bank">{usd(p.bankroll_usd, 0)}</span>
            <span className="pc-line">order ≤ {usd(p.limits.max_order_usd, 0)}</span>
            <span className="pc-line">pause at −{usd(p.limits.daily_loss_limit_usd, 0)}/day</span>
            <span className="pc-line">kill at −{usd(p.limits.max_drawdown_usd, 0)}</span>
            <span className="pc-line">{p.limits.max_markets_per_day} new mkts/day</span>
          </button>
        ))}
      </div>
      {msg && <div className="muted small">{msg}</div>}

      {pick && (
        <div className="modal-back" onClick={() => !busy && setPick(null)}>
          <div className="modal" role="dialog" aria-modal="true" aria-label={`Apply ${pick.label} preset`} onClick={(e) => e.stopPropagation()}>
            <h3>
              Apply {pick.label} · {usd(pick.bankroll_usd, 0)}
            </h3>
            <p className="muted">{pick.summary}</p>
            <table className="grid compact">
              <tbody>
                {[
                  ["Max per order", usd(pick.limits.max_order_usd, 0)],
                  ["Max per market / event / category", `${usd(pick.limits.max_exposure_per_market_usd, 0)} / ${usd(pick.limits.max_exposure_per_event_usd, 0)} / ${usd(pick.limits.max_exposure_per_category_usd, 0)}`],
                  ["Max total exposure", `${usd(pick.limits.max_total_exposure_usd, 0)} (≤ 50% of equity)`],
                  ["Daily loss → pause", usd(pick.limits.daily_loss_limit_usd, 0)],
                  ["Drawdown → kill", usd(pick.limits.max_drawdown_usd, 0)],
                  ["New markets per day / open positions", `${pick.limits.max_markets_per_day} / ${pick.limits.max_open_positions}`],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td className="left">{k}</td>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul className="small">
              <li>
                <b>Autotrade turns on</b>: strategies trade by themselves from now on{status?.state === "killed" ? " (not while KILLED: resume first)" : ""}.
              </li>
              <li>
                <b>Auto-resume</b>: a daily-loss or losing-streak pause lifts itself at the next trading day. A drawdown <b>KILL still needs you</b>.
              </li>
              <li>
                <b>LLM fallback = allow</b>: if the LLM is unreachable, entries go ahead on the quant signal alone (risk limits still apply).
              </li>
              <li>Your mode stays {status?.mode?.toUpperCase() ?? "as is"}; limit edits you make afterwards are kept on top of the preset.</li>
            </ul>
            {paper && (
              <label className={`toggle small ${flat ? "" : "muted"}`} title={flat ? "" : "Close all positions and cancel open orders first"}>
                <input type="checkbox" checked={reset} disabled={!flat} onChange={(e) => setReset(e.target.checked)} />
                Also reset the paper balance to {usd(pick.bankroll_usd, 0)} {flat ? "" : "(needs no open positions or orders)"}
              </label>
            )}
            <div className="row">
              <div className="spacer" />
              <button className="btn" disabled={busy} onClick={() => setPick(null)}>
                Cancel
              </button>
              <button className="btn ok" disabled={busy} onClick={apply} autoFocus>
                {busy ? "Applying…" : `Apply ${pick.label}`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
