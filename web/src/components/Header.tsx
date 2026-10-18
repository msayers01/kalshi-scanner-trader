import { useState } from "react";
import { api } from "../api";
import { usd } from "../format";
import { useStore } from "../store";

export function Header() {
  const status = useStore((s) => s.status);
  const portfolio = useStore((s) => s.portfolio);
  const conn = useStore((s) => s.conn);
  const latency = useStore((s) => s.latencyMs);
  const select = useStore((s) => s.select);
  const openHelp = useStore((s) => s.openHelp);
  const [busy, setBusy] = useState(false);

  async function act(path: string, body: unknown = {}, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    try {
      await api.post(path, body);
    } catch (e) {
      window.alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  const state = status?.state ?? "running";
  const pnl = portfolio?.pnl_today ?? status?.pnl_today ?? 0;
  const md = status?.health?.marketdata;
  const dot = conn !== "open" ? "bad" : md && !md.connected ? "warn" : "ok";

  return (
    <header className="header">
      <div className="brand">kst</div>
      <span className={`badge mode-${status?.mode ?? "paper"}`}>{(status?.mode ?? "…").toUpperCase()}</span>
      <span className={`badge state-${state}`} title={status?.state_reason || undefined}>
        {state.toUpperCase()}
      </span>
      {status?.preset && (
        <button className="badge preset-badge" title="Bankroll preset (Risk limits tab). Hands-off: autotrade + auto-resume." onClick={() => openHelp("presets")}>
          {status.preset.toUpperCase()}
        </button>
      )}
      <span className={`dot ${dot}`} title={`UI link: ${conn}; market data: ${md?.mode ?? "?"} ${md?.connected ? "ok" : "down"}`} />
      <span className="muted small">{latency !== null ? `${latency} ms` : ""}</span>

      <div className="kpis">
        <button className="kpi kpi-btn" title="Show the portfolio chart" onClick={() => select(null)}>
          <div className="kpi-label">Equity</div>
          <div className="kpi-value">{usd(portfolio?.equity ?? status?.equity)}</div>
        </button>
        <Kpi label="Today" value={usd(pnl)} tone={pnl >= 0 ? "pos" : "neg"} />
        <Kpi label="Unrealized" value={usd(portfolio?.unrealized)} />
        <Kpi label="Exposure" value={usd(portfolio?.exposure)} />
        <Kpi label="Markets today" value={`${status?.markets_entered_today ?? 0}/${status?.max_markets_per_day ?? 0}`} />
        <Kpi label="Positions" value={`${status?.positions ?? 0}`} />
      </div>

      <div className="controls">
        <label className="toggle" title="Automatic trading (strategies place orders)">
          <input
            type="checkbox"
            checked={!!status?.autotrade}
            disabled={busy || state === "killed"}
            onChange={(e) => act("/api/control/autotrade", { on: e.target.checked })}
          />
          Auto
        </label>
        {state === "running" ? (
          <button className="btn" disabled={busy} onClick={() => act("/api/control/pause", { reason: "web" })}>
            Pause
          </button>
        ) : (
          <button className="btn ok" disabled={busy} onClick={() => act("/api/control/resume", {}, state === "killed" ? "Resume trading after a KILL?" : undefined)}>
            Resume
          </button>
        )}
        <button
          className="btn danger"
          disabled={busy}
          onClick={() => act("/api/control/kill", { reason: "web kill switch" }, "KILL: cancel ALL orders and stop trading?")}
        >
          KILL
        </button>
        <button
          className="btn danger ghost"
          disabled={busy}
          onClick={() => act("/api/control/kill", { reason: "web kill+flatten", flatten: true }, "KILL and FLATTEN: exit every position at market (within the slippage band)?")}
        >
          Flatten
        </button>
        <button className="btn help-btn" title="Help & guide (?)" aria-label="Open help" onClick={() => openHelp()}>
          ?
        </button>
      </div>
    </header>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}
