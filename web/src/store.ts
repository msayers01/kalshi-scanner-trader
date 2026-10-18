// Single Zustand store fed by one WebSocket (/ws). The server pushes a snapshot on connect, then
// coalesced per-topic updates (≤10 Hz) — the UI never polls.
import { create } from "zustand";
import { getToken } from "./api";
import type { Book, Fill, Limits, LlmStatus, Note, Order, Portfolio, Position, Scan, ScanQuery, Status, Verdict } from "./types";

type Conn = "connecting" | "open" | "closed";

interface State {
  conn: Conn;
  latencyMs: number | null;
  lastMsgMs: number;
  status: Status | null;
  portfolio: Portfolio | null;
  positions: Position[];
  orders: Order[];
  scan: Scan | null;
  limits: Limits | null;
  llm: { status: LlmStatus; verdicts: Verdict[] } | null;
  book: Book | null;
  fills: Fill[];
  notes: Note[];
  selected: string | null;
  select: (ticker: string | null) => void;
  scanQuery: ScanQuery;
  scanQueryError: string;
  setScanQuery: (q: ScanQuery) => void;
}

const QUERY_KEY = "kst.scanQuery";

function loadQuery(): ScanQuery {
  try {
    const raw = localStorage.getItem(QUERY_KEY);
    return raw ? (JSON.parse(raw) as ScanQuery) : {};
  } catch {
    return {};
  }
}

/** Drop empty parts so an "all cleared" query is {} (= the server's plain top-N). */
export function compactQuery(q: ScanQuery): ScanQuery {
  const out: ScanQuery = {};
  const text = Object.fromEntries(Object.entries(q.text ?? {}).filter(([, v]) => v && v.trim()));
  if (Object.keys(text).length) out.text = text;
  const ranges = Object.fromEntries(Object.entries(q.ranges ?? {}).filter(([, v]) => v && (v[0] !== null || v[1] !== null)));
  if (Object.keys(ranges).length) out.ranges = ranges;
  if (q.signals?.length) out.signals = q.signals;
  if (q.llm?.length) out.llm = q.llm;
  if (q.sort) {
    out.sort = q.sort;
    out.desc = q.desc ?? true;
  }
  return out;
}

const MAX_FILLS = 200;
const MAX_NOTES = 100;

export const useStore = create<State>((set, get) => ({
  conn: "connecting",
  latencyMs: null,
  lastMsgMs: 0,
  status: null,
  portfolio: null,
  positions: [],
  orders: [],
  scan: null,
  limits: null,
  llm: null,
  book: null,
  fills: [],
  notes: [],
  selected: null,
  select: (ticker) => {
    if (get().selected === ticker) return;
    set({ selected: ticker, book: null });
    send(ticker ? { op: "sub_book", ticker } : { op: "unsub_book" });
  },
  scanQuery: loadQuery(),
  scanQueryError: "",
  setScanQuery: (q) => {
    const c = compactQuery(q);
    set({ scanQuery: c, scanQueryError: "" });
    try {
      localStorage.setItem(QUERY_KEY, JSON.stringify(c));
    } catch {
      /* storage blocked: the query lasts for this page only */
    }
    send({ op: "scan_query", query: c });
  },
}));

// ------------------------------------------------------------------ websocket

let ws: WebSocket | null = null;
let retry = 0;

function send(msg: unknown): void {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

type Msg = { type: string; data: unknown };

function apply(msg: Msg): void {
  const s = useStore.getState();
  const d = msg.data as never;
  switch (msg.type) {
    case "snapshot": {
      const snap = msg.data as Record<string, unknown>;
      useStore.setState({
        status: snap.status as Status,
        portfolio: snap.portfolio as Portfolio,
        positions: snap.positions as Position[],
        orders: snap.orders as Order[],
        scan: snap.scan as Scan,
        limits: snap.limits as Limits,
        llm: snap.llm as State["llm"],
      });
      if (s.selected) send({ op: "sub_book", ticker: s.selected });
      if (Object.keys(s.scanQuery).length) send({ op: "scan_query", query: s.scanQuery }); // per-connection on the server
      break;
    }
    case "scan_query_error":
      useStore.setState({ scanQueryError: (msg.data as { error: string }).error });
      break;
    case "status":
      useStore.setState({ status: d });
      break;
    case "portfolio":
      useStore.setState({ portfolio: d });
      break;
    case "positions":
      useStore.setState({ positions: d });
      break;
    case "orders":
      useStore.setState({ orders: d });
      break;
    case "scan":
      useStore.setState({ scan: d });
      break;
    case "limits":
      useStore.setState({ limits: d });
      break;
    case "llm":
      useStore.setState({ llm: d });
      break;
    case "book": {
      const b = msg.data as Book | null;
      if (b && b.ticker === s.selected) useStore.setState({ book: b });
      break;
    }
    case "fill":
      useStore.setState({ fills: [msg.data as Fill, ...s.fills].slice(0, MAX_FILLS) });
      break;
    case "alert": {
      const a = msg.data as { level: string; title: string; message: string; ts_ms: number };
      const note: Note = { kind: "alert", level: a.level, title: a.title, message: a.message, ts_ms: a.ts_ms };
      useStore.setState({ notes: [note, ...s.notes].slice(0, MAX_NOTES) });
      break;
    }
    case "reject": {
      const r = msg.data as { ticker: string; check: string; detail: string; ts_ms: number };
      const note: Note = { kind: "reject", level: "info", title: `${r.check} · ${r.ticker}`, message: r.detail, ts_ms: r.ts_ms };
      useStore.setState({ notes: [note, ...s.notes].slice(0, MAX_NOTES) });
      break;
    }
    case "pong": {
      const ts = (msg.data as { ts?: number } | null)?.ts;
      if (typeof ts === "number") useStore.setState({ latencyMs: Math.round(performance.now() - ts) });
      break;
    }
  }
}

export function connect(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = getToken();
  const url = `${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  useStore.setState({ conn: "connecting" });
  ws = new WebSocket(url);
  ws.onopen = () => {
    retry = 0;
    useStore.setState({ conn: "open" });
  };
  ws.onmessage = (ev) => {
    useStore.setState({ lastMsgMs: Date.now() });
    try {
      apply(JSON.parse(ev.data as string) as Msg);
    } catch {
      /* ignore malformed frame */
    }
  };
  ws.onclose = () => {
    useStore.setState({ conn: "closed" });
    const delay = Math.min(10_000, 500 * 2 ** retry++);
    setTimeout(connect, delay);
  };
  ws.onerror = () => ws?.close();
}

// Round-trip latency probe (UI header shows it).
setInterval(() => send({ op: "ping", ts: performance.now() }), 5000);
