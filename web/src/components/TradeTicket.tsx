import { useEffect, useState } from "react";
import { api } from "../api";
import { cents, qty, signedCents, takerFee, usd } from "../format";
import type { Book, Position } from "../types";

interface Props {
  ticker: string;
  book: Book | null;
  fair: number | null; // P(YES)
  feeMultiplier: number;
  prefill: number | null; // YES price clicked in the ladder
  position: Position | null;
}

type Action = "buy" | "sell";
type Outcome = "yes" | "no";
type Mode = "bid" | "mid" | "ask" | "custom";

interface Quotes {
  bid: number | null;
  mid: number | null;
  ask: number | null;
  tick: number;
}

/** Bid / mid / ask of the chosen outcome's own contract. The book is YES-space: a NO bid is 1 − YES ask.
 *  Mid snaps to the tick, rounding toward the passive side for this action, so "Mid" never crosses. */
function quotesFor(book: Book | null, outcome: Outcome, action: Action): Quotes {
  const yb = book?.bids[0]?.[0] ?? null;
  const ya = book?.asks[0]?.[0] ?? null;
  const all = [...(book?.bids ?? []), ...(book?.asks ?? [])].map((l) => l[0]);
  const tick = all.some((p) => Math.abs(p * 100 - Math.round(p * 100)) > 1e-6) ? 0.001 : 0.01;
  const snap = (v: number, up: boolean) => {
    const n = v / tick;
    return Number(((up ? Math.ceil(n - 1e-9) : Math.floor(n + 1e-9)) * tick).toFixed(4));
  };
  const bid = outcome === "yes" ? yb : ya === null ? null : Number((1 - ya).toFixed(4));
  const ask = outcome === "yes" ? ya : yb === null ? null : Number((1 - yb).toFixed(4));
  const mid = bid !== null && ask !== null ? snap((bid + ask) / 2, action === "sell") : null;
  return { bid, mid, ask, tick };
}

/** Manual order entry: Buy/Sell × YES/NO at a live Bid / Mid / Ask (re-priced on every book update) or a
 *  custom price. Kalshi only has "buy YES" and "buy NO": selling YES at p is sent as buying NO at 1 − p.
 *  Every order goes through the same risk checks as the auto-trader. */
export function TradeTicket({ ticker, book, fair, feeMultiplier, prefill, position }: Props) {
  const [action, setAction] = useState<Action>("buy");
  const [outcome, setOutcome] = useState<Outcome>("yes");
  const [mode, setMode] = useState<Mode>("bid");
  const [custom, setCustom] = useState("");
  const [contracts, setContracts] = useState("10");
  const [tif, setTif] = useState<"gtc" | "ioc">("gtc");
  const [postOnly, setPostOnly] = useState(false);
  const [result, setResult] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // New market: back to the live bid (a passive buy), never a price carried over from another book.
  useEffect(() => {
    setMode("bid");
    setTif("gtc");
    setCustom("");
    setResult("");
  }, [ticker]);

  // Ladder click (YES price) → a fixed custom price in this outcome's terms.
  useEffect(() => {
    if (prefill === null) return;
    const p = outcome === "yes" ? prefill : 1 - prefill;
    setCustom(p.toFixed(Math.abs(p * 100 - Math.round(p * 100)) > 1e-6 ? 3 : 2));
    setMode("custom");
  }, [prefill]); // only a new click re-prices; switching YES/NO keeps the typed price

  // Buy ↔ Sell flips which touch is the "take": keep IOC/GTC matched to the live mode.
  useEffect(() => {
    if (mode !== "custom") pickMode(mode);
  }, [action]); // mode/pickMode are read fresh; re-run only when the action flips

  const q = quotesFor(book, outcome, action);
  const live = mode === "custom" ? null : q[mode];
  const p = mode === "custom" ? Number(custom) : (live ?? NaN);
  const n = Number(contracts);
  const valid = Number.isFinite(p) && p > 0 && p < 1 && n > 0;

  // Taking = crossing the spread: buying at/above the ask, or selling at/below the bid.
  const takes = valid && (action === "buy" ? q.ask !== null && p >= q.ask - 1e-9 : q.bid !== null && p <= q.bid + 1e-9);
  const feePer = valid && tif === "ioc" ? takerFee(p, feeMultiplier) : 0;
  const fee = feePer * n;
  const pWin = fair === null ? null : outcome === "yes" ? fair : 1 - fair;
  const ev = !valid || pWin === null ? null : action === "buy" ? pWin - p - feePer : p - pWin - feePer;
  const held = position && position.outcome === outcome ? Math.abs(position.qty) : 0;
  const closing = action === "sell" && held > 0;

  function pickMode(m: Exclude<Mode, "custom">) {
    setMode(m);
    // Taking the touch only makes sense as IOC; resting at it (or at mid) as GTC.
    const take = (action === "buy" && m === "ask") || (action === "sell" && m === "bid");
    setTif(take ? "ioc" : "gtc");
    if (take) setPostOnly(false);
  }

  async function submit() {
    if (!valid) return;
    // Snapshot the price at click time: the live quote may move while the request is in flight.
    const price = p;
    const apiSide: Outcome = action === "buy" ? outcome : outcome === "yes" ? "no" : "yes";
    const apiPrice = action === "buy" ? price : Number((1 - price).toFixed(4));
    setBusy(true);
    setResult("");
    try {
      const r = await api.post<{ ok: boolean; status: string; filled: number; reason: string }>("/api/orders", {
        ticker,
        side: apiSide,
        price: apiPrice,
        contracts: n,
        tif,
        post_only: postOnly,
      });
      setResult(r.ok ? `✓ ${r.status} · filled ${r.filled} @ ${cents(price)}¢` : `✗ ${r.reason || r.status}`);
    } catch (e) {
      setResult(`✗ ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  const verb = action === "buy" ? "Buy" : "Sell";
  const btnTone = action === "buy" ? outcome : outcome === "yes" ? "no" : "yes";
  const missing = mode !== "custom" && live === null;
  const modeLabel = mode === "bid" ? "bid" : mode === "mid" ? "mid (needs a bid and an ask)" : "ask";

  return (
    <div className="ticket">
      <div className="seg">
        <button className={action === "buy" ? "on yes" : ""} onClick={() => setAction("buy")}>
          Buy
        </button>
        <button className={action === "sell" ? "on no" : ""} onClick={() => setAction("sell")}>
          Sell
        </button>
      </div>
      <div className="seg">
        <button className={outcome === "yes" ? "on yes" : ""} onClick={() => setOutcome("yes")}>
          YES
        </button>
        <button className={outcome === "no" ? "on no" : ""} onClick={() => setOutcome("no")}>
          NO
        </button>
      </div>

      <div className="pricemodes" role="radiogroup" aria-label="Price">
        {(["bid", "mid", "ask"] as const).map((m) => (
          <button
            key={m}
            role="radio"
            aria-checked={mode === m}
            className={mode === m ? "on" : ""}
            disabled={q[m] === null}
            onClick={() => pickMode(m)}
            title={m === "mid" ? "Midpoint, rounded toward the passive side" : `Best ${outcome.toUpperCase()} ${m}`}
          >
            <span className="pm-label">{m === "bid" ? "Bid" : m === "mid" ? "Mid" : "Ask"}</span>
            <span key={q[m] ?? "none"} className="pm-val flash">
              {q[m] === null ? "—" : `${cents(q[m])}¢`}
            </span>
          </button>
        ))}
      </div>

      <div className="fields">
        <label>
          Price ($) {mode !== "custom" ? <span className="live-tag">● live {mode}</span> : <span className="muted">custom</span>}
          <input
            inputMode="decimal"
            value={mode === "custom" ? custom : live === null ? "" : live.toFixed(q.tick < 0.01 ? 3 : 2)}
            onChange={(e) => {
              setCustom(e.target.value);
              setMode("custom");
            }}
          />
        </label>
        <label>
          Contracts
          <input inputMode="decimal" value={contracts} onChange={(e) => setContracts(e.target.value)} />
        </label>
        <label>
          Type
          <select value={tif} onChange={(e) => setTif(e.target.value as "gtc" | "ioc")}>
            <option value="ioc">IOC (take now)</option>
            <option value="gtc">GTC (rest)</option>
          </select>
        </label>
        <label className="toggle small">
          <input type="checkbox" checked={postOnly} disabled={tif === "ioc"} onChange={(e) => setPostOnly(e.target.checked)} />
          Post-only
        </label>
      </div>

      <div className="ticket-stats">
        {action === "buy" ? (
          <>
            <span>Cost {valid ? usd(p * n + fee) : "—"}</span>
            <span>Max win {valid ? usd((1 - p) * n - fee) : "—"}</span>
          </>
        ) : closing ? (
          <span>Proceeds {valid ? usd(p * Math.min(n, held) - fee) : "—"}</span>
        ) : (
          <span title={`No ${outcome.toUpperCase()} held: this opens ${outcome === "yes" ? "NO" : "YES"} at ${valid ? cents(1 - p) : "—"}¢`}>
            Collateral {valid ? usd((1 - p) * n + fee) : "—"}
          </span>
        )}
        <span>Fee ≤ {usd(fee, 3)}</span>
        <span className={ev !== null && ev > 0 ? "pos" : ev !== null ? "neg" : ""}>EV/contract {signedCents(ev)}</span>
        {held > 0 && (
          <button className="link" onClick={() => setContracts(String(held))}>
            held {qty(held)} {outcome.toUpperCase()}
          </button>
        )}
      </div>
      {action === "sell" && !closing && (
        <div className="muted small">
          You hold no {outcome.toUpperCase()} here: selling it opens a {outcome === "yes" ? "NO" : "YES"} position.
        </div>
      )}
      {valid && tif === "gtc" && takes && <div className="warn small">This price crosses the spread and will fill now as a taker.</div>}
      {valid && tif === "ioc" && !takes && <div className="warn small">IOC at this price won't fill unless the market moves to it.</div>}
      {book && !book.fresh && <div className="neg small">Order book is stale: live prices may be out of date.</div>}

      <button className={`btn wide ${btnTone}`} disabled={!valid || busy} onClick={submit}>
        {busy ? "Sending…" : missing ? `No ${outcome.toUpperCase()} ${modeLabel} in the book` : `${verb} ${n || 0} ${outcome.toUpperCase()} @ ${valid ? cents(p) : "—"}¢`}
      </button>
      {result && <div className={`ticket-result ${result.startsWith("✓") ? "pos" : "neg"}`}>{result}</div>}
    </div>
  );
}
