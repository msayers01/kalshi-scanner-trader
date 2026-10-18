import { useEffect, useRef, useState, type ReactNode } from "react";
import { useStore } from "../store";

interface Section {
  id: string;
  title: string;
  body: ReactNode;
}

/** In-app guide: every panel, control, column and setting, plus how the engine keeps you safe.
 *  Opened with the "?" button or the ? key; searchable; sections can be deep-linked via openHelp(id). */
export function Help({ onResetLayout }: { onResetLayout: () => void }) {
  const section = useStore((s) => s.help);
  const close = useStore((s) => s.closeHelp);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(section || SECTIONS[0].id);
  const body = useRef<HTMLDivElement>(null);
  const refs = useRef<Record<string, HTMLElement | null>>({});
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [close]);

  useEffect(() => {
    if (section) jump(section);
  }, [section]); // jump only when a (new) section is requested

  // Search: hide sections whose text doesn't contain every word of the query.
  useEffect(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const h = new Set<string>();
    for (const s of SECTIONS) {
      const text = (refs.current[s.id]?.textContent ?? "").toLowerCase();
      if (words.length && !words.every((w) => text.includes(w))) h.add(s.id);
    }
    setHidden(h);
  }, [q]);

  function jump(id: string) {
    setActive(id);
    refs.current[id]?.scrollIntoView({ block: "start" });
  }

  function onScroll() {
    const top = body.current?.getBoundingClientRect().top ?? 0;
    let cur = SECTIONS[0].id;
    for (const s of SECTIONS) {
      const el = refs.current[s.id];
      if (el && !hidden.has(s.id) && el.getBoundingClientRect().top - top < 80) cur = s.id;
    }
    setActive(cur);
  }

  return (
    <div className="modal-back" onClick={close}>
      <div className="modal help" role="dialog" aria-modal="true" aria-label="kst help" onClick={(e) => e.stopPropagation()}>
        <div className="help-top">
          <h2>kst guide</h2>
          <input autoFocus placeholder="Search the guide" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the guide" />
          <span className="muted small">{q && `${SECTIONS.length - hidden.size} section(s) match`}</span>
          <div className="spacer" />
          <button className="btn" onClick={onResetLayout} title="Restore the default panel sizes">
            Reset layout
          </button>
          <button className="btn" onClick={close} aria-label="Close help">
            Close
          </button>
        </div>
        <nav className="help-toc" aria-label="Guide contents">
          {SECTIONS.filter((s) => !hidden.has(s.id)).map((s) => (
            <button key={s.id} className={active === s.id ? "on" : ""} onClick={() => jump(s.id)}>
              {s.title}
            </button>
          ))}
        </nav>
        <div className="help-body" ref={body} onScroll={onScroll}>
          {SECTIONS.map((s) => (
            <section key={s.id} id={`help-${s.id}`} ref={(el) => void (refs.current[s.id] = el)} hidden={hidden.has(s.id)}>
              <h3>{s.title}</h3>
              {s.body}
            </section>
          ))}
          {hidden.size === SECTIONS.length && <div className="empty">Nothing in the guide matches “{q}”.</div>}
        </div>
      </div>
    </div>
  );
}

const SECTIONS: Section[] = [
  {
    id: "start",
    title: "Quick start",
    body: (
      <>
        <p>
          kst scans every open Kalshi market, prices the ones it has a model for, and (optionally) trades them for you behind a risk engine. It always starts in{" "}
          <b>PAPER</b> mode: real market data, simulated money.
        </p>
        <h4>Hands-off in three steps</h4>
        <ol>
          <li>
            Open the <b>Risk limits</b> tab (bottom panel) and pick the <b>preset</b> that matches your starting cash. In paper mode you can also reset the paper balance to that amount.
          </li>
          <li>That's it: autotrade turns on and strategies start looking for entries. Watch the portfolio chart (right panel when no market is selected).</li>
          <li>
            Check in occasionally. If a breaker pauses trading for the day it resumes by itself tomorrow; a <b>KILL</b> (drawdown) waits for you to press Resume.
          </li>
        </ol>
        <h4>Doing it by hand instead</h4>
        <p>
          Leave <b>Auto</b> off, click any market in the scanner, and use the order ticket. Every manual order goes through the same risk checks as the auto-trader.
        </p>
        <div className="callout">
          Live trading needs real API keys, <code>app.mode: live</code> in your config <i>and</i> starting with <code>kst run --i-understand-live-trading</code>. Nothing in the UI can switch you to live.
        </div>
      </>
    ),
  },
  {
    id: "header",
    title: "Header & controls",
    body: (
      <>
        <dl>
          <dt>PAPER / DEMO / LIVE</dt>
          <dd>Trading mode. Paper simulates fills against the live order book; demo uses Kalshi's demo exchange; live is real money.</dd>
          <dt>RUNNING / PAUSED / KILLED</dt>
          <dd>
            Engine state. <b>Paused</b>: no new entries, exits and manual orders still work. <b>Killed</b>: every order cancelled, strategies stop; survives restarts until you press Resume.
            Hover the badge for the reason.
          </dd>
          <dt>Preset badge</dt>
          <dd>The active bankroll preset (if any). Click it to read about presets.</dd>
          <dt>Dot · ms</dt>
          <dd>Green = UI connected and market data fresh; amber = market data stale; red = UI disconnected. The number is the UI ↔ engine round trip.</dd>
          <dt>Equity</dt>
          <dd>Cash + cost of open positions + unrealized P&amp;L. Click it to show the portfolio chart.</dd>
          <dt>Today</dt>
          <dd>Equity now minus equity at the start of the trading day (midnight New York time by default). Includes unrealized P&amp;L and fees.</dd>
          <dt>Unrealized / Exposure</dt>
          <dd>Mark-to-mid P&amp;L of open positions / worst-case loss of open positions and resting buy orders.</dd>
          <dt>Markets today</dt>
          <dd>New markets entered today vs the daily limit. When full, entries into <i>new</i> markets are refused until tomorrow.</dd>
          <dt>Auto</dt>
          <dd>Autotrade. On: strategies place and manage orders. Off: strategy orders are cancelled; you trade manually. Disabled while killed.</dd>
          <dt>Pause / Resume</dt>
          <dd>Pause stops new strategy entries (cancels resting entry orders). Resume restarts trading, including after a KILL (asks first).</dd>
          <dt>KILL</dt>
          <dd>Emergency stop: cancels every order on the exchange and stops all trading. Positions stay open.</dd>
          <dt>Flatten</dt>
          <dd>KILL plus exit every position at market, at most the flatten slippage band (5¢ by default) through the best price, in order-size slices.</dd>
          <dt>?</dt>
          <dd>This guide (also the <kbd>?</kbd> key).</dd>
        </dl>
      </>
    ),
  },
  {
    id: "presets",
    title: "Presets (plug and play)",
    body: (
      <>
        <p>
          A preset scales every risk limit to your starting cash and makes kst <b>hands-off</b>. Pick one in the <b>Risk limits</b> tab, put <code>preset: standard</code> in{" "}
          <code>config/local.yaml</code>, or start with <code>kst run --preset standard</code>. <code>kst presets</code> lists them.
        </p>
        <table className="grid compact">
          <thead>
            <tr>
              <th className="left">Preset</th>
              <th>Bankroll</th>
              <th>Per order</th>
              <th>Daily loss → pause</th>
              <th>Drawdown → kill</th>
              <th>New mkts/day</th>
            </tr>
          </thead>
          <tbody>
            {[
              ["Micro", 100, 6],
              ["Small", 500, 8],
              ["Starter", 1000, 10],
              ["Standard", 5000, 15],
              ["Large", 25000, 20],
              ["Pro", 100000, 25],
            ].map(([n, b, m]) => (
              <tr key={n as string}>
                <td className="left">{n}</td>
                <td>${(b as number).toLocaleString()}</td>
                <td>${((b as number) * 0.05).toLocaleString()}</td>
                <td>${((b as number) * 0.05).toLocaleString()}</td>
                <td>${((b as number) * 0.1).toLocaleString()}</td>
                <td>{m}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <h4>How they scale</h4>
        <p>
          5% of bankroll per order and per market, 10% per event, 25% per category, 50% total exposure (and never more than half your equity). Daily loss pauses at 5%, drawdown kills at 10%. Starter
          ($1,000) equals the shipped defaults.
        </p>
        <h4>What "hands-off" switches on</h4>
        <ul>
          <li>
            <b>Autotrade</b> from the moment kst starts.
          </li>
          <li>
            <b>Auto-resume</b>: a pause caused by a breaker (daily loss, losing streak, reject storm) lifts itself at the next trading day. Pauses you made yourself and any KILL are never undone
            automatically: a drawdown kill means something went badly wrong and deserves a look.
          </li>
          <li>
            <b>LLM fallback = allow</b>: if your LLM is unreachable or out of budget, entries proceed on the quant signal alone. With a working LLM the gate still decides.
          </li>
        </ul>
        <p>
          Limit edits you make afterwards are kept on top of the preset (also across restarts). Choosing a different preset replaces them. Large and Pro allow orders bigger than most Kalshi
          books: treat those limits as ceilings; expect partial fills.
        </p>
      </>
    ),
  },
  {
    id: "layout",
    title: "Layout & resizing",
    body: (
      <>
        <p>The screen has three panels: the scanner (top left), the market / portfolio panel (top right) and the tabs (bottom).</p>
        <ul>
          <li>
            Drag the thin bar between two panels to resize them. It highlights on hover. Sizes are remembered in this browser.
          </li>
          <li>Double-click a bar (or focus it and press Enter) to reset that split; "Reset layout" at the top of this guide restores both.</li>
          <li>
            Keyboard: <kbd>Tab</kbd> to a bar, then arrow keys (<kbd>Shift</kbd> for bigger steps), <kbd>Home</kbd>/<kbd>End</kbd>.
          </li>
          <li>On narrow windows (under 1100 px) the panels stack vertically and the bars are hidden.</li>
        </ul>
      </>
    ),
  },
  {
    id: "scanner",
    title: "Scanner",
    body: (
      <>
        <p>
          Every open market in the universe, ranked by how interesting it is (after-fee edge, liquidity, wide spreads, arbs). The header shows how many markets are scanned and how many have a
          fair-value model.
        </p>
        <h4>Columns</h4>
        <dl>
          <dt>Market</dt>
          <dd>Title and ticker. Click a row to open it in the market panel.</dd>
          <dt>Bid / Ask</dt>
          <dd>Best YES bid and ask in cents. Buying NO at p is the same as selling YES at 100 − p.</dd>
          <dt>Fair</dt>
          <dd>The model's probability of YES combined with the market price, in cents. "—" = no model for this market. Hover for the model name.</dd>
          <dt>Edge</dt>
          <dd>Best expected profit per contract after taker fees if you took the better side right now. Green = positive.</dd>
          <dt>Vol 24h</dt>
          <dd>Contracts traded in the last 24 hours.</dd>
          <dt>Closes</dt>
          <dd>Time until the market closes.</dd>
          <dt>LLM</dt>
          <dd>The LLM gate's latest verdict: E = enter, S = skip, · = none yet.</dd>
        </dl>
        <h4>Signals</h4>
        <dl>
          <dt>ARB</dt>
          <dd>Part of a structural arbitrage (prices in an event that don't add up).</dd>
          <dt>EDGE+ / EDGE−</dt>
          <dd>After-fee edge above the signal threshold: + on buying YES, − on buying NO.</dd>
          <dt>WIDE</dt>
          <dd>Wide bid/ask spread: room for patient (maker) orders.</dd>
          <dt>MOVER</dt>
          <dd>Large price change over 24 h.</dd>
          <dt>CLOSING</dt>
          <dd>Closes within the hour.</dd>
          <dt>DISAGREE</dt>
          <dd>The model and the market disagree a lot; strategies won't trust the model here.</dd>
          <dt>ONESIDED</dt>
          <dd>Only a bid or only an ask.</dd>
        </dl>
        <h4>Filtering & sorting</h4>
        <ul>
          <li>
            Click any column header for <b>sort</b> (low→high / high→low) and a <b>filter</b>: text for Market, min/max for numbers (in the units shown: cents, contracts, hours), checkboxes for
            Signals and LLM.
          </li>
          <li>Filters run on the engine over <i>every</i> scanned market, not just the 100 rows on screen; the header shows the match count.</li>
          <li>Active filters appear as chips under the header; click × to remove one, "Clear all" for everything. Your filters are remembered.</li>
          <li>The search box, category menu and "Signals only" are shortcuts for the same filters.</li>
        </ul>
      </>
    ),
  },
  {
    id: "market",
    title: "Market panel & order book",
    body: (
      <>
        <p>Shows the selected market. "← Portfolio" (or clicking Equity in the header) goes back to the portfolio chart.</p>
        <dl>
          <dt>Order book</dt>
          <dd>
            YES prices: asks (sellers) on top, bids (buyers) below, with depth bars. Click a level to put that price in the ticket. "stale" means the book hasn't refreshed recently: don't trust it.
          </dd>
          <dt>Fair (YES)</dt>
          <dd>Model fair value ± its uncertainty.</dd>
          <dt>Model / weight</dt>
          <dd>Which pricing model applies and how much it is trusted relative to the market price.</dd>
          <dt>LLM</dt>
          <dd>Latest verdict and the model's reason.</dd>
          <dt>Strategy / Note</dt>
          <dd>Which strategy manages this market and what it last decided (and why it didn't trade, if it didn't).</dd>
          <dt>Inputs</dt>
          <dd>The model's raw inputs (spot, volatility, forecast…).</dd>
          <dt>rules</dt>
          <dd>Kalshi's settlement rules for the market. Read them before trading anything unusual.</dd>
        </dl>
        <p>
          In REST mode (no API keys) the book you're looking at, and the books of your open positions, refresh about once a second; other books every few seconds.
        </p>
      </>
    ),
  },
  {
    id: "ticket",
    title: "Order ticket",
    body: (
      <>
        <dl>
          <dt>Buy / Sell</dt>
          <dd>
            Kalshi only has "buy YES" and "buy NO": selling YES at p is sent as buying NO at 100 − p, and the ticket does the conversion. Selling something you don't hold opens the opposite
            position (the ticket warns you).
          </dd>
          <dt>YES / NO</dt>
          <dd>Which outcome you're trading. Prices are always that outcome's own price.</dd>
          <dt>Bid / Mid / Ask</dt>
          <dd>
            Live prices for that outcome. The selected one <b>drives the price and follows the market on every update</b> ("● live bid"). Mid is rounded toward the passive side so it never crosses.
            The ticket starts at the live bid.
          </dd>
          <dt>Price</dt>
          <dd>Typing a price (or clicking a book level) switches to a fixed custom price until you pick Bid/Mid/Ask again.</dd>
          <dt>Type</dt>
          <dd>
            <b>IOC</b> takes what's available now and cancels the rest; <b>GTC</b> rests in the book. Picking Ask on a buy (or Bid on a sell) sets IOC; the passive side sets GTC.
          </dd>
          <dt>Post-only</dt>
          <dd>GTC only: refuse the order if it would trade immediately (guarantees maker fees).</dd>
          <dt>Cost / Proceeds / Collateral</dt>
          <dd>What a buy costs; what a sell of a holding returns; what a sell that opens the other side ties up.</dd>
          <dt>Fee ≤</dt>
          <dd>Worst-case taker fee: 7% × p × (1 − p) per contract (times the market's fee multiplier).</dd>
          <dt>EV/contract</dt>
          <dd>Expected profit per contract against the model's fair value, after the taker fee.</dd>
          <dt>held N</dt>
          <dd>Click to fill the quantity you hold.</dd>
        </dl>
        <p>Warnings appear if a GTC would cross the spread, an IOC can't fill at its price, or the book is stale.</p>
      </>
    ),
  },
  {
    id: "portfolio",
    title: "Portfolio chart",
    body: (
      <>
        <p>The right panel when no market is selected. The headline shows equity and its change over the selected range; hover the chart to scrub to any moment.</p>
        <dl>
          <dt>Ranges</dt>
          <dd>
            <b>Today</b> = since the trading day began (its change equals the header's Today); <b>24H</b> = rolling 24 hours; then 1W, 1M, 3M, 1Y, ALL.
          </dd>
          <dt>Interval</dt>
          <dd>
            How points are grouped: 1 tick (every update), seconds, minutes, hours, a day, Auto, or Custom (any number of sec/min/hours/days). If an interval would draw too many points the chart
            uses a coarser one and says "showing …".
          </dd>
          <dt>Dashed line</dt>
          <dd>Each trading day's opening balance ("open $…"). The line is green above it and red below, switching exactly where it crosses.</dd>
          <dt>Shaded "off 12h"</dt>
          <dd>Periods when kst wasn't running, squeezed to a narrow band so they don't flatten the chart. Hover for exact times.</dd>
          <dt>● live 0.4s</dt>
          <dd>Age of the last equity update. Equity is re-marked 4× a second; amber "waiting" means updates stopped.</dd>
        </dl>
        <p>Tick and second-level detail is kept in memory for 24 h; after a restart, older periods show 1-minute data from the database.</p>
      </>
    ),
  },
  {
    id: "positions",
    title: "Positions & Sell all",
    body: (
      <>
        <dl>
          <dt>Side / Qty / Avg</dt>
          <dd>Outcome held, contracts, and average entry price of that outcome.</dd>
          <dt>Mark</dt>
          <dd>Current mid price used for unrealized P&amp;L (falls back to the last price if the book is unavailable).</dd>
          <dt>Exposure</dt>
          <dd>Cost basis: the most you can lose on the position.</dd>
          <dt>uPnL</dt>
          <dd>Unrealized P&amp;L at the mark.</dd>
          <dt>Strategy</dt>
          <dd>Who opened it (manual, edge_taker, structural_arb, …).</dd>
          <dt>Sell all</dt>
          <dd>
            Exits the whole position at market (IOC, at most the flatten slippage band through the best price, in order-size slices). Cancels that market's resting orders first. If nobody is
            bidding ("no liquidity"), nothing happens; try later or rest a GTC order. Also in the market panel and as <code>/close</code> in Discord.
          </dd>
        </dl>
        <p>Click a row to open the market.</p>
      </>
    ),
  },
  {
    id: "tabs",
    title: "Orders, Fills, Scorecard, Activity, LLM",
    body: (
      <>
        <h4>Orders</h4>
        <p>Recent orders with status (resting, filled, canceled, rejected) and the reason. Cancel resting ones here.</p>
        <h4>Fills</h4>
        <p>Every execution: price, quantity, fee and whether you were the taker or maker.</p>
        <h4>Scorecard</h4>
        <p>
          Lifetime paper performance per strategy: fill rates, realized P&amp;L after fees, win rate, slippage and 60-second markout (did the price move for or against you right after
          filling?), plus expected vs actual results for positions held to settlement. Use it to decide whether a strategy has earned real money.
        </p>
        <h4>Activity</h4>
        <p>Alerts (kills, pauses, preset changes, closes) and risk rejections with the check that refused the order.</p>
        <h4>LLM</h4>
        <p>
          The LLM gate's status, budget and verdicts. "Copy LLM packet" copies the current top candidates in a compact format you can paste into any chat assistant.
        </p>
      </>
    ),
  },
  {
    id: "risk",
    title: "Risk limits",
    body: (
      <>
        <p>
          Every order, manual or automatic, passes these checks; a refusal shows in Activity. Edit a value and Save: changes apply immediately, are audited, and survive restarts. Exits that
          reduce a position are allowed through most entry limits.
        </p>
        <dl>
          <dt>max markets per day</dt>
          <dd>New markets you may enter per trading day.</dd>
          <dt>max open positions</dt>
          <dd>Positions open at once.</dd>
          <dt>max orders per minute</dt>
          <dd>Order rate cap (exits still pass).</dd>
          <dt>max order contracts / usd</dt>
          <dd>Size of a single order.</dd>
          <dt>max position contracts</dt>
          <dd>Contracts held in one market.</dd>
          <dt>max exposure per market / event / category usd</dt>
          <dd>Worst-case loss allowed in one market, one event (related markets) and one category.</dd>
          <dt>max total exposure usd / pct</dt>
          <dd>Worst-case loss across everything, in dollars and as a fraction of equity.</dd>
          <dt>min / max entry price</dt>
          <dd>Refuse entries priced near 0 or 1 (bad risk/reward after fees).</dd>
          <dt>max price deviation</dt>
          <dd>Fat-finger guard: refuse orders far from fair value / mid.</dd>
          <dt>min seconds to close</dt>
          <dd>No new entries this close to the market closing.</dd>
          <dt>stale ws / poll s</dt>
          <dd>Book age after which prices count as stale and entries are refused.</dd>
          <dt>daily loss limit usd</dt>
          <dd>Today's P&amp;L at or below −this → PAUSE.</dd>
          <dt>max drawdown usd</dt>
          <dd>Equity this far below today's peak → KILL.</dd>
          <dt>max consecutive losses</dt>
          <dd>This many losing round trips in a row → PAUSE.</dd>
        </dl>
      </>
    ),
  },
  {
    id: "breakers",
    title: "Breakers, pause & kill",
    body: (
      <>
        <dl>
          <dt>Daily loss → PAUSE</dt>
          <dd>Stops new entries for the rest of the day.</dd>
          <dt>Losing streak → PAUSE</dt>
          <dd>Too many losing round trips in a row.</dd>
          <dt>Reject storm → PAUSE</dt>
          <dd>Many risk rejections in a short window usually means a bug or bad data.</dd>
          <dt>Drawdown → KILL</dt>
          <dd>Cancels everything and stops. Needs a human to press Resume.</dd>
          <dt>Critical task failure → KILL</dt>
          <dd>Market data, the database writer or another essential task crashed repeatedly.</dd>
        </dl>
        <p>
          With <b>auto-resume</b> (on with any preset) breaker pauses lift at the next trading day. KILLED survives restarts on purpose. Kalshi's cancel-all can also cancel orders placed within
          the next minute, so don't re-enter immediately after a kill.
        </p>
      </>
    ),
  },
  {
    id: "strategies",
    title: "Strategies & models",
    body: (
      <>
        <dl>
          <dt>structural_arb</dt>
          <dd>
            Trades events whose prices violate probability rules (e.g. mutually exclusive outcomes whose NO prices sum to less than the payout), as a basket across all legs. Lowest risk, rare.
          </dd>
          <dt>edge_taker</dt>
          <dd>
            Enters when the model's fair value beats the price by more than fees plus a safety margin, sized by fractional Kelly and shrunk by the model's uncertainty; exits when holding stops being
            worth it. Takes liquidity or rests a patient order depending on the spread.
          </dd>
          <dt>market_maker</dt>
          <dd>Quotes both sides around fair value with inventory skew. Off by default; enable per series only after paper results justify it.</dd>
        </dl>
        <h4>Models</h4>
        <ul>
          <li>
            <b>Crypto</b> (BTC, ETH, SOL, XRP, DOGE above/below/range): settles on a 60-second average of the CF Benchmarks index; priced with fat-tailed volatility, cross-checked with the
            volatility implied by the whole event.
          </li>
          <li>
            <b>Weather</b> (daily high temperature): ensemble forecasts, adjusted by the high observed so far today. The climate day follows standard time.
          </li>
          <li>Everything else has no model: fair value is the market price, so only arbs (and the LLM, if used) find trades there.</li>
        </ul>
        <p>The math is in docs/STRATEGIES.md.</p>
      </>
    ),
  },
  {
    id: "llm",
    title: "LLM gate",
    body: (
      <>
        <p>
          An optional local (Ollama, LM Studio) or cloud model reviews candidate entries in batches and answers enter/skip with a reason. It never sizes or places orders; it can only veto.
        </p>
        <dl>
          <dt>mode</dt>
          <dd>off · advisory (verdicts shown, not enforced) · gate (entries need an ENTER).</dd>
          <dt>fallback</dt>
          <dd>What happens when the LLM is down or over budget: deny (no entries) or allow (quant signal alone). Presets set allow.</dd>
          <dt>budget</dt>
          <dd>Daily caps on calls and tokens.</dd>
        </dl>
        <p>
          If you don't run an LLM, either use a preset or set <code>llm.fallback: allow</code> (or <code>llm.mode: off</code>); otherwise the gate will refuse every entry.
        </p>
      </>
    ),
  },
  {
    id: "discord",
    title: "Discord bot",
    body: (
      <>
        <p>
          Optional phone control. Put the token, channel and your user id in <code>.env</code>, set <code>discord.enabled: true</code>. Only allowed users can use controls; replies are private.
        </p>
        <dl>
          <dt>/panel</dt>
          <dd>Posts a persistent button panel (Pause, Resume, KILL, Kill + Flatten, Status, P&amp;L, Autotrade).</dd>
          <dt>/status /pnl /positions /scan</dt>
          <dd>Read-only summaries.</dd>
          <dt>/pause /resume /kill /flatten</dt>
          <dd>Same as the header buttons (flatten asks to confirm).</dd>
          <dt>/close TICKER</dt>
          <dd>Sell all of one position.</dd>
          <dt>/autotrade on|off</dt>
          <dd>Toggle autotrade.</dd>
          <dt>/limits /setlimit</dt>
          <dd>Show or change a risk limit.</dd>
        </dl>
      </>
    ),
  },
  {
    id: "cli",
    title: "Command line",
    body: (
      <dl>
        <dt>kst run</dt>
        <dd>
          Everything (paper by default). Options: <code>--preset KEY</code>, <code>--autotrade/--no-autotrade</code>, <code>--mode</code>, <code>--no-web</code>, <code>--no-discord</code>,{" "}
          <code>--i-understand-live-trading</code>.
        </dd>
        <dt>kst scan</dt>
        <dd>Scanner + UI + manual trading; never trades automatically (not even with a preset).</dd>
        <dt>kst presets</dt>
        <dd>List bankroll presets.</dd>
        <dt>kst check</dt>
        <dd>Validate config, keys, exchange connectivity and the LLM.</dd>
        <dt>kst status / pause / resume / kill</dt>
        <dd>Control a running instance from another terminal.</dd>
        <dt>kst report / scorecard</dt>
        <dd>Daily P&amp;L history; lifetime paper scorecard (JSON).</dd>
        <dt>kst llm-packet</dt>
        <dd>Print the LLM decision packet.</dd>
        <dt>kst replay / calibrate / calibrate-weather</dt>
        <dd>Offline backtests on recorded data and model calibration (see docs/REPLAY.md, docs/CALIBRATION.md).</dd>
      </dl>
    ),
  },
  {
    id: "trouble",
    title: "Troubleshooting",
    body: (
      <dl>
        <dt>Auto is on but nothing trades</dt>
        <dd>
          Check, in order: engine RUNNING (not paused/killed); Markets today below its limit; the LLM tab (gate + unreachable LLM with fallback deny refuses everything); Activity for risk
          rejections; and whether the scanner shows any EDGE+/EDGE−/ARB signals at all.
        </dd>
        <dt>PAUSED: daily_loss</dt>
        <dd>Today's loss hit the limit. Resume manually (it will re-pause if still below), raise the limit, or wait: with auto-resume it lifts tomorrow.</dd>
        <dt>KILLED: drawdown</dt>
        <dd>Equity fell the max drawdown below today's peak. Review positions and fills, then Resume.</dd>
        <dt>Rejected: PriceBand</dt>
        <dd>Entry price outside min/max entry price (3–97¢ by default).</dd>
        <dt>Rejected: DailyNewMarketLimit</dt>
        <dd>Already entered today's maximum of new markets.</dd>
        <dt>Rejected: FatFinger</dt>
        <dd>Price too far from fair value / mid.</dd>
        <dt>Sell all: no liquidity</dt>
        <dd>Nobody is bidding for what you hold. Wait, or rest a GTC sell.</dd>
        <dt>Book "stale" / amber dot</dt>
        <dd>Market data isn't refreshing (network or rate limits). Entries are refused on stale books, by design.</dd>
        <dt>Chart shows "waiting"</dt>
        <dd>No equity update for 5 s: the engine may be busy or disconnected (see the reconnect banner).</dd>
      </dl>
    ),
  },
  {
    id: "glossary",
    title: "Glossary",
    body: (
      <dl>
        <dt>Contract</dt>
        <dd>Pays $1 if its outcome happens, $0 otherwise. A price of 42¢ ≈ a 42% market-implied chance.</dd>
        <dt>YES-price space</dt>
        <dd>kst shows everything as YES prices: a NO bid at 58¢ is a YES ask at 42¢.</dd>
        <dt>Taker / maker</dt>
        <dd>Taker = your order trades immediately against the book (higher fee); maker = it rests and someone trades against it (lower fee).</dd>
        <dt>Edge</dt>
        <dd>Fair value minus the price you pay, minus fees, per contract.</dd>
        <dt>Exposure</dt>
        <dd>The most you can lose: cost basis of positions plus resting buy orders.</dd>
        <dt>Drawdown</dt>
        <dd>How far equity is below its peak for the day.</dd>
        <dt>Markout</dt>
        <dd>How the mid moved after your fill; consistently negative means you're being picked off.</dd>
        <dt>Trading day</dt>
        <dd>Midnight to midnight in America/New_York (configurable). Daily limits and P&amp;L reset then.</dd>
      </dl>
    ),
  },
];
