import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { linePaths, foldLive, nearest, openAt, type Pt } from "../equityGeometry";
import { usd } from "../format";
import { useStore } from "../store";
import type { EquityRange, EquitySeries } from "../types";

const RANGES: { key: EquityRange; label: string; past: string }[] = [
  { key: "TODAY", label: "Today", past: "Today" },
  { key: "24H", label: "24H", past: "Past 24 hours" },
  { key: "1W", label: "1W", past: "Past week" },
  { key: "1M", label: "1M", past: "Past month" },
  { key: "3M", label: "3M", past: "Past 3 months" },
  { key: "1Y", label: "1Y", past: "Past year" },
  { key: "ALL", label: "ALL", past: "All time" },
];
const INTERVALS: { v: string; label: string }[] = [
  { v: "auto", label: "Auto" },
  { v: "tick", label: "1 tick" },
  { v: "1s", label: "1 sec" },
  { v: "5s", label: "5 sec" },
  { v: "15s", label: "15 sec" },
  { v: "30s", label: "30 sec" },
  { v: "1m", label: "1 min" },
  { v: "5m", label: "5 min" },
  { v: "15m", label: "15 min" },
  { v: "30m", label: "30 min" },
  { v: "1h", label: "1 hour" },
  { v: "4h", label: "4 hours" },
  { v: "1d", label: "1 day" },
];
const UNITS = [
  { v: "s", label: "sec" },
  { v: "m", label: "min" },
  { v: "h", label: "hours" },
  { v: "d", label: "days" },
];
const PREF_KEY = "kst.chart";
const PAD_Y = 14;
const REFETCH_MS = 30_000;

interface Prefs {
  range: EquityRange;
  interval: string; // a preset value, or "custom"
  customN: string;
  customUnit: string;
}

function loadPrefs(): Prefs {
  const d: Prefs = { range: "TODAY", interval: "auto", customN: "10", customUnit: "s" };
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) ?? "{}") as Partial<Prefs>;
    return {
      range: RANGES.some((r) => r.key === p.range) ? (p.range as EquityRange) : d.range,
      interval: p.interval === "custom" || INTERVALS.some((i) => i.v === p.interval) ? (p.interval as string) : d.interval,
      customN: p.customN && /^\d{1,6}$/.test(p.customN) ? p.customN : d.customN,
      customUnit: UNITS.some((u) => u.v === p.customUnit) ? (p.customUnit as string) : d.customUnit,
    };
  } catch {
    return d;
  }
}

/** Robinhood-style portfolio chart.
 *  - Ranges: Today (since the trading day began), rolling 24H, 1W … ALL.
 *  - Intervals: 1 tick, seconds, minutes, hours, days, or a custom N×unit; Auto picks one that fits.
 *  - A dashed line marks each trading day's opening balance; the line is green above it and red below,
 *    switching exactly where it crosses (and re-judged against the new open at each midnight).
 *  - Live: every equity push (≤ 4/s from the engine) extends the newest bucket in place, no refetch.
 *  - X is time, so engine-off periods show as faint gaps rather than being squeezed out. */
export function PortfolioChart() {
  const portfolio = useStore((s) => s.portfolio);
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [series, setSeries] = useState<EquitySeries | null>(null);
  const [error, setError] = useState("");
  const [hoverT, setHoverT] = useState<number | null>(null);
  const [version, setVersion] = useState(0); // bumps when live points mutate `pts`
  const pts = useRef<Pt[]>([]);
  const lastPushAt = useRef<number>(0);
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 240 });

  const interval = prefs.interval === "custom" ? `${prefs.customN || "1"}${prefs.customUnit}` : prefs.interval;

  useEffect(() => {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch {
      /* storage blocked */
    }
  }, [prefs]);

  // History: on range/interval change, every REFETCH_MS (picks up DB/tape points), and at day rollover.
  const day = portfolio?.day;
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<EquitySeries>(`/api/equity?range=${prefs.range}&interval=${encodeURIComponent(interval)}`)
        .then((s) => {
          if (!alive) return;
          pts.current = s.points.map((p) => [p[0], p[1]] as Pt);
          setSeries(s);
          setError("");
          setVersion((v) => v + 1);
        })
        .catch((e) => alive && setError(String(e)));
    const t = setTimeout(load, prefs.interval === "custom" ? 300 : 0); // debounce typing a custom interval
    const r = setInterval(load, REFETCH_MS);
    return () => {
      alive = false;
      clearTimeout(t);
      clearInterval(r);
    };
  }, [prefs.range, interval, prefs.interval, day]);

  // Live: fold each pushed equity into the series exactly as the server would bucket it.
  useEffect(() => {
    if (!portfolio || !series) return;
    lastPushAt.current = performance.now();
    const t = portfolio.ts_ms || Date.now();
    if (foldLive(pts.current, t, portfolio.equity, series.interval_ms, series.align_offset_ms)) {
      if (series.range === "24H") {
        const cutoff = t - 86_400_000;
        let k = 0;
        while (k < pts.current.length - 1 && pts.current[k + 1][0] <= cutoff) k++;
        if (k) pts.current.splice(0, k); // keep one point at/before the edge
      }
      setVersion((v) => v + 1);
    }
  }, [portfolio, series]);

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const { width, height } = e.contentRect;
      setSize({ w: Math.max(200, Math.floor(width)), h: Math.max(140, Math.floor(height)) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const points = pts.current;
  const days = series?.days ?? [];
  const baseline = series?.baseline ?? points[0]?.[1] ?? null;
  const live = points.length ? points[points.length - 1][1] : (portfolio?.equity ?? null);
  const hi = hoverT !== null && points.length ? nearest(points, hoverT) : null;
  const shown = hi !== null ? points[hi] : null;
  const value = shown ? shown[1] : live;
  const change = value !== null && baseline !== null ? value - baseline : null;
  const changePct = change !== null && baseline ? change / baseline : null;
  const past = RANGES.find((r) => r.key === prefs.range)?.past ?? "";
  const ivMs = series?.interval_ms ?? null;
  const fine = ivMs === null || ivMs < 60_000;

  const geo = useMemo(() => {
    if (!series || points.length < 2) return null;
    const t0 = Math.min(series.start_ms, points[0][0]);
    const t1 = Math.max(points[points.length - 1][0], t0 + 1000);
    const visibleDays = days.filter((d) => d.start_ms <= t1);
    const vals = points.map((p) => p[1]);
    for (const d of visibleDays) vals.push(d.open);
    let lo = Math.min(...vals);
    let hiV = Math.max(...vals);
    if (hiV - lo < 0.01) {
      lo -= 0.5;
      hiV += 0.5;
    }
    const { w, h } = size;
    const x = (t: number) => ((t - t0) / (t1 - t0)) * (w - 4) + 2;
    const y = (v: number) => PAD_Y + (1 - (v - lo) / (hiV - lo)) * (h - 2 * PAD_Y);
    const gapMs = Math.max(3 * (ivMs ?? 0), 180_000);
    const paths = linePaths(points, visibleDays, baseline, x, y, gapMs);
    // Dashed opening-balance step: one horizontal run per trading day, plus faint separators at midnights.
    const opens = visibleDays.map((d, i) => {
      const a = Math.max(d.start_ms, t0);
      const b = Math.min(visibleDays[i + 1]?.start_ms ?? t1, t1);
      return { x1: x(a), x2: x(b), y: y(d.open), open: d.open, sep: d.start_ms > t0 ? x(d.start_ms) : null };
    });
    return { x, y, w, h, paths, opens, t0, t1 };
  }, [series, version, size, days, baseline, ivMs]); // `points` is a ref: `version` tracks its mutations

  const tone = (() => {
    const o = live !== null ? openAt(days, points[points.length - 1]?.[0] ?? Date.now(), baseline) : null;
    return live !== null && o !== null && live < o ? "neg" : "pos";
  })();
  const headTone = change !== null && change < 0 ? "neg" : "pos";

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    if (!geo) return;
    const r = e.currentTarget.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    setHoverT(geo.t0 + f * (geo.t1 - geo.t0));
  }

  const setP = (p: Partial<Prefs>) => setPrefs((cur) => ({ ...cur, ...p }));
  const lastOpen = geo?.opens[geo.opens.length - 1];

  return (
    <div className={`pchart tone-${tone}`}>
      <div className="pchart-head">
        <div className="pchart-label muted small">
          Portfolio value <LiveTag at={lastPushAt} version={version} />
        </div>
        <div className="pchart-value">{usd(value)}</div>
        <div className={`pchart-change ${headTone}`}>
          {change === null ? "—" : `${change >= 0 ? "+" : "-"}${usd(Math.abs(change))}`}
          {changePct !== null && ` (${changePct >= 0 ? "+" : ""}${(changePct * 100).toFixed(2)}%)`}
          <span className="muted"> {shown ? stamp(shown[0], prefs.range, fine) : past}</span>
        </div>
      </div>

      <div className="pchart-plot" ref={wrap}>
        {geo ? (
          <svg
            width={geo.w}
            height={geo.h}
            viewBox={`0 0 ${geo.w} ${geo.h}`}
            role="img"
            aria-label={`Portfolio value ${past.toLowerCase()}: ${usd(baseline)} to ${usd(live)}`}
            onPointerMove={onMove}
            onPointerLeave={() => setHoverT(null)}
          >
            {geo.opens.map((o, i) => (
              <g key={i}>
                {o.sep !== null && <line className="pchart-daysep" x1={o.sep} x2={o.sep} y1={0} y2={geo.h} />}
                <line className="pchart-base" x1={o.x1} x2={o.x2} y1={o.y} y2={o.y} />
              </g>
            ))}
            {lastOpen && (
              <text className="pchart-openlabel" x={geo.w - 4} y={lastOpen.y - 4} textAnchor="end">
                open {usd(lastOpen.open)}
              </text>
            )}
            <path className="pchart-gap" d={geo.paths.gaps} />
            <path className="pchart-line pos" d={geo.paths.pos} />
            <path className="pchart-line neg" d={geo.paths.neg} />
            {shown ? (
              <g>
                <line className="pchart-cross" x1={geo.x(shown[0])} x2={geo.x(shown[0])} y1={0} y2={geo.h} />
                <circle className={`pchart-dot ${shown[1] >= (openAt(days, shown[0], baseline) ?? shown[1]) ? "pos" : "neg"}`} cx={geo.x(shown[0])} cy={geo.y(shown[1])} r={4} />
              </g>
            ) : (
              <circle className={`pchart-dot live ${tone}`} cx={geo.x(points[points.length - 1][0])} cy={geo.y(points[points.length - 1][1])} r={3.5} />
            )}
          </svg>
        ) : (
          <div className="empty">
            {error || (series ? "Not enough history for this range yet. Equity is recorded while kst runs." : "Loading…")}
          </div>
        )}
      </div>

      <div className="pchart-controls">
        <div className="pchart-ranges" role="group" aria-label="Chart range">
          {RANGES.map((r) => (
            <button key={r.key} className={r.key === prefs.range ? "on" : ""} aria-pressed={r.key === prefs.range} onClick={() => setP({ range: r.key })}>
              {r.label}
            </button>
          ))}
        </div>
        <div className="pchart-interval">
          <label className="muted small" htmlFor="pchart-iv">
            Interval
          </label>
          <select id="pchart-iv" value={prefs.interval} onChange={(e) => setP({ interval: e.target.value })}>
            {INTERVALS.map((i) => (
              <option key={i.v} value={i.v}>
                {i.label}
              </option>
            ))}
            <option value="custom">Custom…</option>
          </select>
          {prefs.interval === "custom" && (
            <>
              <input
                className="pchart-n"
                inputMode="numeric"
                aria-label="Custom interval length"
                value={prefs.customN}
                onChange={(e) => setP({ customN: e.target.value.replace(/\D/g, "").slice(0, 6) })}
              />
              <select aria-label="Custom interval unit" value={prefs.customUnit} onChange={(e) => setP({ customUnit: e.target.value })}>
                {UNITS.map((u) => (
                  <option key={u.v} value={u.v}>
                    {u.label}
                  </option>
                ))}
              </select>
            </>
          )}
          {series && (prefs.interval === "auto" || series.note) && (
            <span className="muted small" title={series.note || undefined}>
              {series.note ? `showing ${series.interval_label}` : `= ${series.interval_label}`}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** "● LIVE 0.4s" — age of the last equity push, re-rendered 4×/s so staleness is visible at a glance. */
function LiveTag({ at, version }: { at: React.RefObject<number>; version: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(t);
  }, []);
  void version;
  const age = at.current ? (performance.now() - at.current) / 1000 : null;
  const stale = age === null || age > 5;
  return (
    <span className={`pchart-live ${stale ? "stale" : ""}`} title="Time since the last equity update from the engine">
      ● {stale ? "waiting" : "live"} {age !== null ? `${age < 10 ? age.toFixed(1) : Math.round(age)}s` : ""}
    </span>
  );
}

function stamp(ms: number, range: EquityRange, fine: boolean): string {
  const d = new Date(ms);
  const time = d.toLocaleTimeString([], fine ? { hour: "numeric", minute: "2-digit", second: "2-digit" } : { hour: "numeric", minute: "2-digit" });
  if (range === "TODAY") return time;
  if (range === "24H" || range === "1W" || range === "1M") return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
