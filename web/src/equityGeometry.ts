// Pure helpers for the portfolio chart: day-open lookup, gain/loss line splitting, live bucketing.

export type Pt = [number, number]; // [ts_ms, equity]
export interface DayOpen {
  start_ms: number;
  open: number;
}

/** Opening equity of the trading day containing `t` (days sorted by start). */
export function openAt(days: DayOpen[], t: number, fallback: number | null): number | null {
  let lo = 0;
  let hi = days.length - 1;
  let hit = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (days[mid].start_ms <= t) {
      hit = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return hit >= 0 ? days[hit].open : days.length ? days[0].open : fallback;
}

/** Index of the point nearest in time to `t` (points sorted). */
export function nearest(points: Pt[], t: number): number {
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1][0] - t) <= Math.abs(points[lo][0] - t)) return lo - 1;
  return lo;
}

export interface Paths {
  pos: string; // at/above that day's open
  neg: string; // below it
  gaps: string; // spans with no data (engine off): drawn faint
}

/** Build the line as green/red SVG paths, split exactly where it crosses its day's opening balance and at
 *  day boundaries (where the reference line steps). Spans longer than `gapMs` become a faint gap path. */
export function linePaths(points: Pt[], days: DayOpen[], fallbackOpen: number | null, x: (t: number) => number, y: (v: number) => number, gapMs: number): Paths {
  const out = { pos: [] as string[], neg: [] as string[], gaps: [] as string[] };
  const end: Record<"pos" | "neg", string> = { pos: "", neg: "" };
  const f = (n: number) => n.toFixed(1);
  const emit = (color: "pos" | "neg", t1: number, v1: number, t2: number, v2: number) => {
    const a = `${f(x(t1))},${f(y(v1))}`;
    const b = `${f(x(t2))},${f(y(v2))}`;
    if (end[color] === a) out[color].push(`L${b}`);
    else out[color].push(`M${a}L${b}`);
    end[color] = b;
  };
  const piece = (t1: number, v1: number, t2: number, v2: number) => {
    const o = openAt(days, t1, fallbackOpen);
    if (o === null) return emit("pos", t1, v1, t2, v2);
    const s1 = v1 >= o;
    const s2 = v2 >= o;
    if (s1 === s2 || v1 === v2) return emit(s1 ? "pos" : "neg", t1, v1, t2, v2);
    const tc = t1 + ((o - v1) / (v2 - v1)) * (t2 - t1); // crossing time
    emit(s1 ? "pos" : "neg", t1, v1, tc, o);
    emit(s2 ? "pos" : "neg", tc, o, t2, v2);
  };
  for (let i = 1; i < points.length; i++) {
    const [t1, v1] = points[i - 1];
    const [t2, v2] = points[i];
    if (t2 - t1 > gapMs) {
      out.gaps.push(`M${f(x(t1))},${f(y(v1))}L${f(x(t2))},${f(y(v2))}`);
      end.pos = end.neg = "";
      continue;
    }
    // Split at any day boundary inside (t1, t2) so each piece is judged against its own day's open.
    let ta = t1;
    let va = v1;
    for (const d of days) {
      if (d.start_ms <= ta || d.start_ms >= t2) continue;
      const vb = v1 + ((v2 - v1) * (d.start_ms - t1)) / (t2 - t1);
      piece(ta, va, d.start_ms, vb);
      ta = d.start_ms;
      va = vb;
    }
    piece(ta, va, t2, v2);
  }
  return { pos: out.pos.join(""), neg: out.neg.join(""), gaps: out.gaps.join("") };
}

/** Fold a live equity push into the series the way the server buckets: extend the newest aligned bucket,
 *  or start the next one. Tick mode appends each change (plus a 5 s heartbeat while flat). Mutates `pts`;
 *  returns whether anything changed. */
export function foldLive(pts: Pt[], t: number, v: number, intervalMs: number | null, alignOffsetMs: number): boolean {
  const last = pts[pts.length - 1];
  if (!last) {
    pts.push([t, v]);
    return true;
  }
  if (intervalMs === null) {
    if (t < last[0]) return false;
    if (v === last[1] && t - last[0] < 5000) return false;
    if (t === last[0]) last[1] = v;
    else pts.push([t, v]);
    return true;
  }
  const key = Math.floor((t + alignOffsetMs) / intervalMs);
  const lastKey = Math.floor((last[0] + alignOffsetMs) / intervalMs);
  if (key < lastKey) return false;
  if (key === lastKey) {
    if (last[1] === v) return false;
    last[1] = v;
    return true;
  }
  pts.push([key * intervalMs - alignOffsetMs, v]);
  return true;
}
