// Shapes mirror src/kst/api/serialize.py. Prices: scan rows use integer Price units ($0.0001);
// everything else uses dollars (floats) and contracts (floats).

export interface Status {
  mode: "paper" | "demo" | "live" | "replay";
  state: "running" | "paused" | "killed";
  state_reason: string;
  state_since_ms: number;
  autotrade: boolean;
  live_armed: boolean;
  equity: number;
  cash: number;
  pnl_today: number;
  positions: number;
  open_orders: number;
  markets_entered_today: number;
  max_markets_per_day: number;
  md_connected: boolean;
  health?: Health;
}

export interface Health {
  loop_lag_ms: number;
  marketdata: { mode: string; connected: boolean; focus: number; universe: number; errors: number };
  scanner: { universe: number; priced: number; age_s: number } | null;
  llm: LlmStatus;
  authenticated: boolean;
}

export interface Portfolio {
  ts_ms: number;
  mode: string;
  cash: number;
  equity: number;
  unrealized: number;
  exposure: number;
  realized_today: number;
  fees_today: number;
  pnl_today: number;
  trades_today: number;
  round_trips_today: number;
  wins_today: number;
  losses_today: number;
  n_positions: number;
  day: string;
  peak_equity: number;
  by_strategy: Record<string, { net_usd: number; fees_usd: number; trades: number }>;
}

export interface ScanRow {
  ticker: string;
  event: string;
  series: string;
  title: string;
  category: string;
  bid: number | null; // Price units
  ask: number | null;
  bid_qty: number;
  ask_qty: number;
  mid: number | null; // dollars
  spread: number | null; // Price units
  vol24h: number;
  oi: number;
  chg24h: number | null;
  close_s: number | null;
  book: boolean;
  model: string;
  fair: number | null;
  q: number | null;
  q_sd: number | null;
  edge_buy: number | null;
  edge_sell: number | null;
  edge: number | null;
  signals: string[];
  score: number;
  detail: Record<string, unknown>;
  llm: "E" | "S" | null;
}

export interface Arb {
  event: string;
  kind: string;
  edge: number;
  baskets: number;
  legs: { ticker: string; side: "bid" | "ask"; price: number }[];
}

export interface Scan {
  ts_ms: number;
  rows: ScanRow[];
  arbs: Arb[];
  universe: number;
  priced: number;
  ms?: number;
}

export interface Book {
  ticker: string;
  bids: [number, number][];
  asks: [number, number][];
  ts_ms: number;
  fresh: boolean;
  mid: number | null;
  micro: number | null;
}

export interface Position {
  ticker: string;
  title: string;
  event: string;
  category: string;
  qty: number;
  outcome: "yes" | "no";
  avg_price: number | null;
  mark: number | null;
  exposure: number;
  unrealized: number | null;
  realized: number;
  fees: number;
  strategy: string;
  opened_ms: number;
}

export interface Order {
  coid: string;
  ticker: string;
  side: "bid" | "ask";
  outcome: "yes" | "no";
  price: number;
  qty: number;
  filled: number;
  status: string;
  tif: string;
  post_only: boolean;
  strategy: string;
  slot: string;
  fees: number;
  reason: string;
  created_ms: number;
  updated_ms: number;
}

export interface Fill {
  id: string;
  ticker: string;
  side: "bid" | "ask";
  outcome?: "yes" | "no";
  price: number;
  qty: number;
  fee: number;
  taker: boolean;
  strategy: string;
  ts_ms: number;
}

export interface LlmStatus {
  mode: string;
  scope: string;
  provider: string | null;
  model: string | null;
  calls_today: number;
  max_calls: number;
  tokens_in: number;
  tokens_out: number;
  pending: number;
  last_error: string;
  budget_left: boolean;
}

export interface Verdict {
  ticker: string;
  enter: boolean;
  confidence: number;
  side: "yes" | "no" | null;
  reason: string;
  ts_ms: number;
  model: string;
}

export interface MarketDetail {
  ticker: string;
  event: string;
  series: string;
  title: string;
  subtitle: string;
  category: string;
  status: string;
  close_ms: number | null;
  rules: string;
  fee_type: string;
  fee_multiplier: number;
  book: Book | null;
  scan: ScanRow | null;
  fair: { q: number; sd: number; w_model: number; model: string | null; detail: Record<string, unknown> } | null;
  position: Position | null;
  orders: Order[];
  llm: Verdict | null;
  last_eval: { strategy: string; note: string; gated: string; takes: number; quotes: number; ts_ms: number } | null;
}

export interface Note {
  kind: "alert" | "reject";
  level: string;
  title: string;
  message: string;
  ts_ms: number;
}

export type Limits = Record<string, number>;
