import { useEffect, useState } from "react";
import { api } from "../api";
import { cents, signedCents, takerFee, usd } from "../format";
import type { Book } from "../types";

interface Props {
  ticker: string;
  book: Book | null;
  fair: number | null; // P(YES)
  feeMultiplier: number;
  prefill: number | null; // YES price clicked in the ladder
}

/** Manual order entry. "Buy YES @ p" or "Buy NO @ p" (NO price = 1 − YES price). Every order goes
 *  through the same risk checks as the auto-trader. */
export function TradeTicket({ ticker, book, fair, feeMultiplier, prefill }: Props) {
  const [side, setSide] = useState<"yes" | "no">("yes");
  const [price, setPrice] = useState("");
  const [contracts, setContracts] = useState("10");
  const [tif, setTif] = useState<"gtc" | "ioc">("ioc");
  const [postOnly, setPostOnly] = useState(false);
  const [result, setResult] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // Default price: the touch you'd take (best ask for YES, 1 − best bid for NO).
  const touch = side === "yes" ? book?.asks[0]?.[0] : book?.bids[0] ? 1 - book.bids[0][0] : undefined;
  useEffect(() => {
    setResult("");
    if (prefill !== null) {
      setPrice((side === "yes" ? prefill : 1 - prefill).toFixed(prefill * 100 % 1 === 0 ? 2 : 3));
    } else if (touch !== undefined) {
      setPrice(touch.toFixed(2));
    }
  }, [ticker, side, prefill]);

  useEffect(() => {
    if (price === "" && touch !== undefined) setPrice(touch.toFixed(2));
  }, [touch, price]);

  const p = Number(price);
  const n = Number(contracts);
  const valid = p > 0 && p < 1 && n > 0;
  const fee = valid ? takerFee(p, feeMultiplier) * n : 0;
  const cost = valid ? p * n + (tif === "ioc" ? fee : 0) : 0;
  const winProb = fair === null ? null : side === "yes" ? fair : 1 - fair;
  const ev = valid && winProb !== null ? winProb - p - (tif === "ioc" ? takerFee(p, feeMultiplier) : 0) : null;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    setResult("");
    try {
      const r = await api.post<{ ok: boolean; status: string; filled: number; reason: string }>("/api/orders", {
        ticker,
        side,
        price: p,
        contracts: n,
        tif,
        post_only: postOnly,
      });
      setResult(r.ok ? `✓ ${r.status} · filled ${r.filled}` : `✗ ${r.reason || r.status}`);
    } catch (e) {
      setResult(`✗ ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ticket">
      <div className="seg">
        <button className={side === "yes" ? "on yes" : ""} onClick={() => setSide("yes")}>
          Buy YES
        </button>
        <button className={side === "no" ? "on no" : ""} onClick={() => setSide("no")}>
          Buy NO
        </button>
      </div>
      <div className="fields">
        <label>
          Price ($)
          <input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
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
        <span>Cost {usd(cost)}</span>
        <span>Max win {valid ? usd((1 - p) * n - (tif === "ioc" ? fee : 0)) : "—"}</span>
        <span>Fee ≤ {usd(fee, 3)}</span>
        <span className={ev !== null && ev > 0 ? "pos" : ev !== null ? "neg" : ""}>EV/contract {signedCents(ev)}</span>
        {touch !== undefined && <span className="muted">touch {cents(touch)}¢</span>}
      </div>
      <button className={`btn wide ${side}`} disabled={!valid || busy} onClick={submit}>
        {busy ? "Sending…" : `Buy ${n || 0} ${side.toUpperCase()} @ ${valid ? cents(p) : "—"}¢`}
      </button>
      {result && <div className={`ticket-result ${result.startsWith("✓") ? "pos" : "neg"}`}>{result}</div>}
    </div>
  );
}
