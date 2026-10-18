import { api } from "./api";
import { qty } from "./format";
import type { Position } from "./types";

interface CloseResult {
  ok: boolean;
  ticker: string;
  filled?: number;
  remaining?: number;
  reason?: string;
}

/** "Sell all": confirm, then exit the whole position at market (IOC inside the flatten slippage band).
 *  Returns false if the user cancelled. Partial fills / refusals are reported, never retried. */
export async function closePosition(p: Position): Promise<boolean> {
  const n = qty(Math.abs(p.qty));
  const what = `${n} ${p.outcome.toUpperCase()} on ${p.title || p.ticker}`;
  if (!window.confirm(`Sell all: exit ${what} at market (within the flatten slippage band)?`)) return false;
  try {
    const r = await api.post<CloseResult>(`/api/positions/${encodeURIComponent(p.ticker)}/close`);
    if (!r.ok || (r.remaining ?? 0) > 0) {
      const sold = r.filled ? `Sold ${qty(r.filled)}. ` : "";
      const left = r.remaining ? `${qty(r.remaining)} still open. ` : "";
      window.alert(`Close ${p.ticker}: ${sold}${left}${r.reason ?? ""}`.trim());
    }
  } catch (e) {
    window.alert(String(e));
  }
  return true;
}
