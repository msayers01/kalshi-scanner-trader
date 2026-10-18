# Runbook

## 1. Install

```bash
uv sync --all-extras                 # Python 3.12+ (uv installs it if needed)
cd web && npm ci && npm run build    # Node 20.19+ / 22.12+
cp .env.example .env
```

Configuration is layered. Each layer overrides the one before it:

1. `config/default.yaml` (committed)
2. `config/local.yaml` (yours, git-ignored)
3. `.env` and `KST_*` environment variables

Every key is documented in `src/kst/config.py`.

## 2. Kalshi API keys (optional for paper, required for demo and live)

1. Go to kalshi.com → Account → **API Keys** → *Create key*.
   - Prefer **Ed25519**.
   - Download the private key PEM and save it as `secrets/kalshi_private_key.pem` (git-ignored).
2. In `.env`:
   ```
   KST_KALSHI__KEY_ID=<key id>
   KST_KALSHI__PRIVATE_KEY_PATH=secrets/kalshi_private_key.pem
   ```
3. Run `uv run kst check`. It should print your balance and API tier.
   - Basic tier: 20 reads/s, 10 writes/s.
   - Upgrade to Advanced with `POST /account/api_usage_level/upgrade` (one call from the Kalshi API).

For **demo**, create keys on demo.kalshi.co and run with `--mode demo`. The demo and prod hosts are
switched automatically.

With keys, market data switches from REST polling to the authenticated WebSocket, and crypto is priced
off the CF Benchmarks settlement index streamed by Kalshi.

## 3. Discord bot (phone control)

1. https://discord.com/developers/applications → *New Application* → **Bot**.
   - Copy the token.
   - No privileged intents are needed.
2. *OAuth2 → URL Generator*:
   - Scopes: `bot`, `applications.commands`.
   - Permissions: Send Messages, Embed Links.
   - Open the generated URL and invite the bot to your server.
3. Enable Developer Mode in Discord, then right-click to copy your **user ID**, the **server ID**, and
   the **channel ID**.
4. In `.env`:
   ```
   KST_DISCORD__ENABLED=true
   KST_DISCORD__TOKEN=...
   KST_DISCORD__GUILD_ID=...          # slash commands register instantly in this server
   KST_DISCORD__CHANNEL_ID=...        # notifications + control panel
   KST_DISCORD__ALLOWED_USER_IDS=[123456789012345678]
   ```
5. Start `kst run`. The bot posts a control panel in the channel. `/panel` reposts it any time.

Only allow-listed users can use the controls. Every press is audited (`audit_log` table).
**Kill + Flatten** asks for a second confirmation.

## 4. LLM entry gate

**Local (default).** Install [Ollama](https://ollama.com), then run `ollama pull qwen3:14b` (or any
model). Set `llm.provider.model` in `config/local.yaml`.

- LM Studio: `base_url: http://localhost:1234/v1`.
- Qwen3 "thinking" is switched off automatically (`/no_think`) to keep calls fast and cheap.

**Claude as a fallback or primary.**

```yaml
llm:
  escalation:            # used only when the local model is unreachable
    kind: anthropic
    model: claude-opus-5-5
  escalate_on_unavailable: true
```

Put `ANTHROPIC_API_KEY` (or `KST_LLM__ESCALATION__API_KEY`) in `.env`.

The Anthropic provider uses the official SDK with:

- low effort (this is a classification task)
- JSON-schema structured output
- prompt caching on the static system prompt
- server-side refusal fallbacks (`fallbacks: "default"`)

To cut cost further, set `model: claude-haiku-5-5`.

**Modes** (`llm.mode`):

- `off`
- `advisory`: logs verdicts only, so you can measure whether the LLM adds value first.
- `gate`

See `docs/LLM_GATE.md`.

If the LLM is unreachable in `gate` mode, gated entries are **denied** (`llm.fallback: deny`). For
model-only trading without an LLM, set `llm.mode: off`.

## 5. Going from paper → demo → live (checklist)

- [ ] At least two weeks of paper trading. Review `kst report`, the per-strategy P&L in the UI, and
      `data/pnl/*.json`.
- [ ] Realized edge per trade is positive after fees for each strategy you keep enabled. Disable the
      rest.
- [ ] Calibrate:
  - `pricing.s_p_base`
  - `strategies.edge_taker.min_edge` and `z`
  - crypto `implied_weight`
  - weather `bias_f` per station
- [ ] Risk limits sized to money you can afford to lose. Start with
      `daily_loss_limit_usd ≤ 1–2%` of the account.
- [ ] A demo run with the same config: orders, fills, cancels, and kill switch all exercised.
- [ ] `risk.order_group.enabled: true` in live mode (an exchange-side runaway-fill breaker).
- [ ] Discord controls tested from your phone: Pause, KILL, Kill+Flatten.
- [ ] Run `kst run --mode live --i-understand-live-trading`. Keep autotrade off for the first session
      and watch it.

## 6. Operations

| Situation | Action |
|---|---|
| Something looks wrong | **KILL** (UI button, Discord button, `/kill`, or `kst kill`). It cancels every order exchange-wide. Positions stay. |
| Exit everything now | **Kill + Flatten**: IOC exits within `risk.flatten_max_slippage` of the touch. Anything that can't fill inside the band is reported, not chased. |
| Resume after a kill | Fix the cause first, then press Resume. `KILLED` persists across restarts on purpose. |
| Daily loss breaker paused trading | It stays paused for the day unless you resume. Exits are still managed. |
| Market data down | In WS mode, the dead-man's switch cancels all resting orders after `risk.ws_down_cancel_after_s` and pauses. |
| Process crashed with live orders | GTC orders expire after `risk.order_ttl_s` (exchange-side) and cancel on exchange pauses. On restart the engine reconciles positions and open orders from Kalshi. |

**Logs.** `data/logs/kst.jsonl` holds structured JSON lines. Try `jq 'select(.event=="risk_rejected")'`.

**Daily P&L files** are written to `data/pnl/<day>_<mode>.json`. They are generated:

- at `discord.daily_report_time`
- at day rollover
- on shutdown

**Databases.** Each mode has its own SQLite file: `data/kst_paper.db`, `data/kst_demo.db`,
`data/kst_live.db`.

**Schema changes.** v0.1 creates tables at startup. When you change `persistence/tables.py` after you
have real data, add Alembic (`uv run alembic init migrations`), point it at `Base.metadata`, and stamp
existing databases before the first migration.

## 7. Unauthenticated limits (observed 2026-10-18)

Light public requests are fine at about 20/s. Large `GET /markets` pages (limit 500–1000) are throttled
much harder when you are not authenticated. That is why the catalog fetches markets by close-time
horizon (`universe.max_hours_to_close`, default 24 h, about 5k markets) with `universe.page_delay_s`
between pages. Widening the horizon to 72 h means about 33k markets and about 70 MB per refresh, so
only do it with API keys.
