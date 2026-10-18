import { useEffect, useMemo, useRef, useState } from "react";
import { cents, qty, signedCents, ttc } from "../format";
import { useStore } from "../store";
import type { RangeCol, ScanQuery, ScanRow, SortCol } from "../types";

const SIGNAL_CLASS: Record<string, string> = { ARB: "sig-arb", "EDGE+": "sig-pos", "EDGE-": "sig-neg", WIDE: "sig-wide" };
const KNOWN_SIGNALS = ["ARB", "EDGE+", "EDGE-", "WIDE", "MOVER", "CLOSING"];
const EDGE_SIGNALS = ["ARB", "EDGE+", "EDGE-"];
const LLM_OPTS: { v: "E" | "S" | "none"; label: string }[] = [
  { v: "E", label: "Enter" },
  { v: "S", label: "Skip" },
  { v: "none", label: "No verdict" },
];

type Col =
  | { key: "market"; label: string; kind: "text"; left: true; title?: string }
  | { key: RangeCol; label: string; kind: "range"; unit: string; title?: string; left?: false }
  | { key: "signals" | "llm"; label: string; kind: "set"; left?: boolean; title?: string };

const COLS: Col[] = [
  { key: "market", label: "Market", kind: "text", left: true },
  { key: "bid", label: "Bid", kind: "range", unit: "¢" },
  { key: "ask", label: "Ask", kind: "range", unit: "¢" },
  { key: "fair", label: "Fair", kind: "range", unit: "¢", title: "Model fair value combined with the market (cents)" },
  { key: "edge", label: "Edge", kind: "range", unit: "¢", title: "Best after-fee taker edge per contract" },
  { key: "vol24h", label: "Vol 24h", kind: "range", unit: "contracts" },
  { key: "closes", label: "Closes", kind: "range", unit: "hours" },
  { key: "signals", label: "Signals", kind: "set", left: true },
  { key: "llm", label: "LLM", kind: "set", title: "LLM verdict" },
];

export function ScannerTable() {
  const scan = useStore((s) => s.scan);
  const selected = useStore((s) => s.selected);
  const select = useStore((s) => s.select);
  const query = useStore((s) => s.scanQuery);
  const setQuery = useStore((s) => s.setScanQuery);
  const queryError = useStore((s) => s.scanQueryError);
  const [open, setOpen] = useState<Col["key"] | null>(null);
  const [search, setSearch] = useState(query.text?.market ?? "");

  // The search box is the Market text filter; debounce it so typing doesn't send a query per keystroke.
  useEffect(() => {
    if ((query.text?.market ?? "") === search) return;
    const t = setTimeout(() => setQuery({ ...query, text: { ...query.text, market: search } }), 250);
    return () => clearTimeout(t);
  }, [search]); // only the box drives this; external query changes sync back below
  useEffect(() => setSearch(query.text?.market ?? ""), [query.text?.market]);

  const rows = scan?.rows ?? [];
  const cat = query.text?.category ?? "";
  const cats = useMemo(() => {
    const seen = new Set((scan?.rows ?? []).map((r) => r.category).filter(Boolean));
    if (cat) seen.add(cat); // a filtered result may not contain every category: keep the chosen one listed
    return [...seen].sort();
  }, [scan, cat]);
  const edgeOnly = EDGE_SIGNALS.every((s) => query.signals?.includes(s)) && query.signals?.length === EDGE_SIGNALS.length;
  const chips = describe(query);

  return (
    <section className="panel scanner">
      <div className="panel-head">
        <h2>Scanner</h2>
        <span className="muted small">
          {scan
            ? `${scan.universe} markets · ${scan.priced} priced · ${scan.arbs.length} arbs` +
              (scan.matched !== null && scan.matched !== undefined ? ` · ${scan.matched} match${scan.matched === 1 ? "" : "es"}` : "")
            : "waiting for data…"}
        </span>
        <div className="spacer" />
        <input className="search" placeholder="Search all markets" value={search} onChange={(e) => setSearch(e.target.value)} />
        <select value={cat} onChange={(e) => setQuery({ ...query, text: { ...query.text, category: e.target.value } })}>
          <option value="">All categories</option>
          {cats.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <label className="toggle small">
          <input type="checkbox" checked={edgeOnly} onChange={(e) => setQuery({ ...query, signals: e.target.checked ? EDGE_SIGNALS : [] })} />
          Signals only
        </label>
      </div>
      {(chips.length > 0 || queryError) && (
        <div className="chips">
          {chips.map((c) => (
            <span key={c.id} className="chip">
              {c.label}
              <button aria-label={`Remove ${c.label}`} onClick={() => setQuery(c.remove(query))}>
                ×
              </button>
            </span>
          ))}
          {chips.length > 1 && (
            <button className="link" onClick={() => setQuery({})}>
              Clear all
            </button>
          )}
          {queryError && <span className="neg small">{queryError}</span>}
        </div>
      )}
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              {COLS.map((c) => (
                <HeaderCell key={c.key} col={c} query={query} open={open === c.key} setOpen={(o) => setOpen(o ? c.key : null)} setQuery={setQuery} rows={rows} />
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row key={r.ticker} r={r} active={r.ticker === selected} onClick={() => select(r.ticker)} />
            ))}
          </tbody>
        </table>
        {scan && rows.length === 0 && <div className="empty">No markets match the filters.</div>}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ header + popover

function HeaderCell({
  col,
  query,
  open,
  setOpen,
  setQuery,
  rows,
}: {
  col: Col;
  query: ScanQuery;
  open: boolean;
  setOpen: (o: boolean) => void;
  setQuery: (q: ScanQuery) => void;
  rows: ScanRow[];
}) {
  const ref = useRef<HTMLTableCellElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, setOpen]);

  const sorted = query.sort === col.key ? (query.desc ? "desc" : "asc") : null;
  const filtered = isFiltered(query, col.key);
  return (
    <th ref={ref} className={`${col.left ? "left" : ""} th-filter ${filtered ? "filtered" : ""}`} title={col.title} aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}>
      <button className="th-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>
        {col.label}
        {sorted && <span className="th-sort">{sorted === "desc" ? "▼" : "▲"}</span>}
        {filtered && <span className="th-dot" aria-label="filtered" />}
      </button>
      {open && <FilterPopover col={col} query={query} setQuery={setQuery} close={() => setOpen(false)} rows={rows} />}
    </th>
  );
}

function FilterPopover({ col, query, setQuery, close, rows }: { col: Col; query: ScanQuery; setQuery: (q: ScanQuery) => void; close: () => void; rows: ScanRow[] }) {
  const sortKey = col.key as SortCol;
  const sort = (desc: boolean) => {
    setQuery({ ...query, sort: sortKey, desc });
    close();
  };
  const [text, setText] = useState(col.key === "market" ? (query.text?.market ?? "") : "");
  const r = col.kind === "range" ? query.ranges?.[col.key] : undefined;
  const [lo, setLo] = useState(r?.[0] ?? "");
  const [hi, setHi] = useState(r?.[1] ?? "");
  const [pick, setPick] = useState<string[]>(col.key === "signals" ? (query.signals ?? []) : col.key === "llm" ? (query.llm ?? []) : []);
  const signalOpts = useMemo(() => [...new Set([...KNOWN_SIGNALS, ...rows.flatMap((x) => x.signals)])], [rows]);

  function apply(e?: React.FormEvent) {
    e?.preventDefault();
    if (col.kind === "text") setQuery({ ...query, text: { ...query.text, market: text } });
    if (col.kind === "range") {
      const num = (v: string | number) => (v === "" || v === null || Number.isNaN(Number(v)) ? null : Number(v));
      setQuery({ ...query, ranges: { ...query.ranges, [col.key]: [num(lo), num(hi)] } });
    }
    if (col.key === "signals") setQuery({ ...query, signals: pick });
    if (col.key === "llm") setQuery({ ...query, llm: pick as ScanQuery["llm"] });
    close();
  }
  function clear() {
    setQuery(clearCol(query, col.key));
    close();
  }
  const toggle = (v: string) => setPick((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v]));

  return (
    <form className={`popover ${col.left ? "" : "right"}`} role="dialog" aria-label={`${col.label} filter`} onSubmit={apply}>
      <div className="pop-sort">
        <button type="button" className={query.sort === sortKey && !query.desc ? "on" : ""} onClick={() => sort(false)}>
          ▲ {col.kind === "text" ? "A → Z" : "Low → high"}
        </button>
        <button type="button" className={query.sort === sortKey && query.desc ? "on" : ""} onClick={() => sort(true)}>
          ▼ {col.kind === "text" ? "Z → A" : "High → low"}
        </button>
      </div>
      {col.kind === "text" && (
        <label className="pop-field">
          <span>Contains (title, ticker or event)</span>
          <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. bitcoin" />
        </label>
      )}
      {col.kind === "range" && (
        <div className="pop-range">
          <label className="pop-field">
            <span>Min {col.unit}</span>
            <input autoFocus inputMode="decimal" value={lo ?? ""} onChange={(e) => setLo(e.target.value)} />
          </label>
          <label className="pop-field">
            <span>Max {col.unit}</span>
            <input inputMode="decimal" value={hi ?? ""} onChange={(e) => setHi(e.target.value)} />
          </label>
        </div>
      )}
      {col.kind === "set" && (
        <div className="pop-set">
          {(col.key === "signals" ? signalOpts.map((v) => ({ v, label: v })) : LLM_OPTS).map((o) => (
            <label key={o.v} className="toggle">
              <input type="checkbox" checked={pick.includes(o.v)} onChange={() => toggle(o.v)} />
              {o.label}
            </label>
          ))}
          {col.key === "signals" && <div className="muted small">Shows rows with any of the checked signals.</div>}
        </div>
      )}
      <div className="pop-actions">
        <button type="button" className="btn tiny" onClick={clear}>
          Clear
        </button>
        <button type="submit" className="btn tiny ok">
          Apply
        </button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------ query helpers

function isFiltered(q: ScanQuery, key: Col["key"]): boolean {
  if (key === "market") return !!q.text?.market;
  if (key === "signals") return !!q.signals?.length;
  if (key === "llm") return !!q.llm?.length;
  const r = q.ranges?.[key];
  return !!r && (r[0] !== null || r[1] !== null);
}

function clearCol(q: ScanQuery, key: Col["key"]): ScanQuery {
  const next: ScanQuery = { ...q, text: { ...q.text }, ranges: { ...q.ranges } };
  if (key === "market") delete next.text!.market;
  else if (key === "signals") next.signals = [];
  else if (key === "llm") next.llm = [];
  else delete next.ranges![key];
  if (next.sort === key) next.sort = null;
  return next;
}

function describe(q: ScanQuery): { id: string; label: string; remove: (q: ScanQuery) => ScanQuery }[] {
  const out: { id: string; label: string; remove: (q: ScanQuery) => ScanQuery }[] = [];
  if (q.text?.market) out.push({ id: "market", label: `Market: “${q.text.market}”`, remove: (x) => ({ ...x, text: { ...x.text, market: "" } }) });
  if (q.text?.category) out.push({ id: "category", label: `Category: ${q.text.category}`, remove: (x) => ({ ...x, text: { ...x.text, category: "" } }) });
  for (const c of COLS) {
    if (c.kind !== "range") continue;
    const r = q.ranges?.[c.key];
    if (!r || (r[0] === null && r[1] === null)) continue;
    const u = ({ "¢": "¢", hours: "h" } as Record<string, string>)[c.unit] ?? "";
    const label = r[0] !== null && r[1] !== null ? `${c.label} ${r[0]}–${r[1]}${u}` : r[0] !== null ? `${c.label} ≥ ${r[0]}${u}` : `${c.label} ≤ ${r[1]}${u}`;
    out.push({ id: c.key, label, remove: (x) => clearCol(x, c.key) });
  }
  if (q.signals?.length) out.push({ id: "signals", label: `Signals: ${q.signals.join(" | ")}`, remove: (x) => ({ ...x, signals: [] }) });
  if (q.llm?.length) out.push({ id: "llm", label: `LLM: ${q.llm.map((v) => LLM_OPTS.find((o) => o.v === v)?.label ?? v).join(" | ")}`, remove: (x) => ({ ...x, llm: [] }) });
  if (q.sort) {
    const c = COLS.find((x) => x.key === q.sort);
    out.push({ id: "sort", label: `Sort: ${c?.label ?? q.sort} ${q.desc ? "▼" : "▲"}`, remove: (x) => ({ ...x, sort: null }) });
  }
  return out;
}

// ------------------------------------------------------------------ rows

function Row({ r, active, onClick }: { r: ScanRow; active: boolean; onClick: () => void }) {
  const edge = r.edge;
  return (
    <tr className={active ? "active" : undefined} onClick={onClick}>
      <td className="left market">
        <div className="title" title={r.title}>
          {r.title}
        </div>
        <div className="ticker">{r.ticker}</div>
      </td>
      <td>{cents(r.bid, true)}</td>
      <td>{cents(r.ask, true)}</td>
      <td title={r.model || undefined}>{r.q !== null ? cents(r.q) : "—"}</td>
      <td className={edge !== null && edge > 0 ? "pos" : edge !== null ? "neg" : ""}>{signedCents(edge)}</td>
      <td>{qty(r.vol24h)}</td>
      <td>{ttc(r.close_s)}</td>
      <td className="left">
        {r.signals.slice(0, 3).map((s) => (
          <span key={s} className={`sig ${SIGNAL_CLASS[s] ?? ""}`}>
            {s}
          </span>
        ))}
      </td>
      <td className={r.llm === "E" ? "pos" : r.llm === "S" ? "neg" : "muted"}>{r.llm ?? "·"}</td>
    </tr>
  );
}
