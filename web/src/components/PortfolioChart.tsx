import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { usd } from "../format";
import { useStore } from "../store";
import type { EquitySeries, EquityRange } from "../types";

const RANGES: { key: EquityRange; label: string; past: string }[] = [
  { key: "1D", label: "1D", past: "Past day" },
  { key: "1W", label: "1W", past: "Past week" },
  { key: "1M", label: "1M", past: "Past month" },
  { key: "3M", label: "3M", past: "Past 3 months" },
  { key: "1Y", label: "1Y", past: "Past year" },
  { key: "ALL", label: "ALL", past: "All time" },
];
const RANGE_KEY = "kst.equityRange";
const PAD_Y = 12;

function loadRange(): EquityRange {
  try {
    const r = localStorage.getItem(RANGE_KEY);
    if (r && RANGES.some((x) => x.key === r)) return r as EquityRange;
  } catch {
    /* storage blocked */
  }
  return "1D";
}

/** Robinhood-style portfolio value chart: headline value + change, a line that turns green/red against the
 *  range's starting value, a dashed baseline, a hover crosshair that scrubs the headline, and range chips.
 *  Points are spaced evenly (not by time) so hours the engine was off don't flatten the line. */
export function PortfolioChart() {
  const portfolio = useStore((s) => s.portfolio);
  const [range, setRange] = useState<EquityRange>(loadRange);
  const [series, setSeries] = useState<EquitySeries | null>(null);
  const [error, setError] = useState("");
  const [hover, setHover] = useState<number | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 220 });

  useEffect(() => {
    try {
      localStorage.setItem(RANGE_KEY, range);
    } catch {
      /* storage blocked */
    }
    let alive = true;
    const load = () =>
      api
        .get<EquitySeries>(`/api/equity?range=${range}`)
        .then((s) => {
          if (!alive) return;
          setSeries(s);
          setError("");
        })
        .catch((e) => alive && setError(String(e)));
    load();
    const t = setInterval(load, 60_000); // snapshots land once a minute; the live tip comes from the WS
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [range]);

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const { width, height } = e.contentRect;
      setSize({ w: Math.max(200, Math.floor(width)), h: Math.max(120, Math.floor(height)) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Replace the series' last point with the pushed equity so the tip moves in real time.
  const points = useMemo(() => {
    const pts = (series?.points ?? []).slice();
    if (portfolio && pts.length) pts[pts.length - 1] = [Math.max(pts[pts.length - 1][0], portfolio.ts_ms || Date.now()), portfolio.equity];
    return pts;
  }, [series, portfolio]);

  const baseline = series?.baseline ?? points[0]?.[1] ?? null;
  const live = points.length ? points[points.length - 1][1] : (portfolio?.equity ?? null);
  const shown = hover !== null && points[hover] ? points[hover] : null;
  const value = shown ? shown[1] : live;
  const change = value !== null && baseline !== null ? value - baseline : null;
  const changePct = change !== null && baseline ? change / baseline : null;
  const tone = (live ?? 0) - (baseline ?? 0) >= 0 ? "pos" : "neg";
  const past = RANGES.find((r) => r.key === range)?.past ?? "";

  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const vals = points.map((p) => p[1]);
    if (baseline !== null) vals.push(baseline);
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    }
    const { w, h } = size;
    const x = (i: number) => (i / (points.length - 1)) * (w - 2) + 1;
    const y = (v: number) => PAD_Y + (1 - (v - lo) / (hi - lo)) * (h - 2 * PAD_Y);
    const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    return { x, y, d, w, h };
  }, [points, baseline, size]);

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    if (!geo || points.length < 2) return;
    const r = e.currentTarget.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    setHover(Math.round(f * (points.length - 1)));
  }

  return (
    <div className={`pchart tone-${tone}`}>
      <div className="pchart-head">
        <div className="pchart-label muted small">Portfolio value</div>
        <div className="pchart-value">{usd(value)}</div>
        <div className={`pchart-change ${change !== null && change < 0 ? "neg" : "pos"}`}>
          {change === null ? "—" : `${change >= 0 ? "+" : "-"}${usd(Math.abs(change))}`}
          {changePct !== null && ` (${changePct >= 0 ? "+" : ""}${(changePct * 100).toFixed(2)}%)`}
          <span className="muted"> {shown ? stamp(shown[0], range) : past}</span>
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
            onPointerLeave={() => setHover(null)}
          >
            {baseline !== null && (
              <line className="pchart-base" x1={0} x2={geo.w} y1={geo.y(baseline)} y2={geo.y(baseline)} />
            )}
            <path className="pchart-line" d={geo.d} />
            {hover !== null && points[hover] && (
              <g>
                <line className="pchart-cross" x1={geo.x(hover)} x2={geo.x(hover)} y1={0} y2={geo.h} />
                <circle className="pchart-dot" cx={geo.x(hover)} cy={geo.y(points[hover][1])} r={4} />
              </g>
            )}
            {hover === null && (
              <circle className="pchart-dot live" cx={geo.x(points.length - 1)} cy={geo.y(points[points.length - 1][1])} r={3.5} />
            )}
          </svg>
        ) : (
          <div className="empty">
            {error || (series ? "Not enough history for this range yet. Equity is recorded once a minute while kst runs." : "Loading…")}
          </div>
        )}
      </div>

      <div className="pchart-ranges" role="group" aria-label="Chart range">
        {RANGES.map((r) => (
          <button key={r.key} className={r.key === range ? "on" : ""} aria-pressed={r.key === range} onClick={() => setRange(r.key)}>
            {r.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function stamp(ms: number, range: EquityRange): string {
  const d = new Date(ms);
  if (range === "1D") return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (range === "1W" || range === "1M") return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
