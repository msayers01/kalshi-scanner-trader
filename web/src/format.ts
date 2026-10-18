// Display helpers. Scan rows carry integer Price units ($0.0001); books/positions carry dollars.

export const PRICE_SCALE = 10_000;

export function cents(p: number | null | undefined, fromUnits = false): string {
  if (p === null || p === undefined) return "—";
  const c = fromUnits ? p / 100 : p * 100;
  return Number.isInteger(c) ? `${c}` : c.toFixed(c < 1 || c > 99 ? 2 : 1);
}

export function usd(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const s = Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${x < 0 ? "-" : ""}$${s}`;
}

export function signedCents(x: number | null | undefined): string {
  if (x === null || x === undefined) return "—";
  const c = x * 100;
  return `${c >= 0 ? "+" : ""}${c.toFixed(1)}¢`;
}

export function pct(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;
}

export function qty(x: number): string {
  return Math.abs(x) >= 1000 ? `${(x / 1000).toFixed(1)}k` : Number.isInteger(x) ? `${x}` : x.toFixed(2);
}

export function ttc(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 0) return "closed";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 172_800) return `${(seconds / 3600).toFixed(1)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

export function time(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Kalshi general taker fee per contract at price p (dollars): 0.07·p·(1−p)·multiplier. */
export function takerFee(p: number, multiplier = 1): number {
  return 0.07 * multiplier * p * (1 - p);
}
