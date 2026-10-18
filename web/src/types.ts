// Shapes mirror src/kst/api/serialize.py. Prices: scan rows use integer Price units ($0.0001);
// everything else uses dollars (floats) and contracts (floats).

export interface Status {
  mode: "paper" | "demo" | "live" | "replay";
  state: "running" | "paused" | "killed";
  state_reason: string;
  state_since_ms: number;
  autotrade: boolean;
  preset: string | null;
  auto_resume: boolean;
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
  matched?: number | null; // set when a column query is active: matches across the whole universe
}

// Mirrors src/kst/scanner/query.py. Units are display units: cents, contracts, hours.
export type TextCol = "market" | "category";
export type RangeCol = "bid" | "ask" | "fair" | "edge" | "vol24h" | "closes";
export type SortCol = TextCol | RangeCol | "signals" | "llm";
export interface ScanQuery {
  text?: Partial<Record<TextCol, string>>;
  ranges?: Partial<Record<RangeCol, [number | null, number | null]>>;
  signals?: string[];
  llm?: ("E" | "S" | "none")[];
  sort?: SortCol | null;
  desc?: boolean;
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

// Mirrors src/kst/presets.py Preset.to_dict().
export interface Preset {
  key: string;
  label: string;
  bankroll_usd: number;
  summary: string;
  limits: Record<string, number>;
  arb_basket_usd: number;
  hands_off: { autotrade: boolean; auto_resume: boolean; llm_fallback: string };
}

export type EquityRange = "TODAY" | "24H" | "1W" | "1M" | "3M" | "1Y" | "ALL";

// Mirrors src/kst/portfolio/history.py EquitySeries.to_dict().
export interface EquitySeries {
  range: EquityRange;
  interval: string; // as requested: tick | auto | <n>s|m|h|d
  interval_ms: number | null; // as used (null = every tick)
  interval_label: string;
  align_offset_ms: number; // buckets start at floor((t + offset) / interval) * interval − offset
  start_ms: number;
  baseline: number | null; // equity at the start of the range (dollars)
  current: number | null;
  change: number | null;
  change_pct: number | null;
  points: [number, number][]; // [ts_ms, equity dollars], oldest first
  days: { start_ms: number; open: number }[]; // each trading day's opening equity
  note: string; // set when the requested interval was coarsened
}
