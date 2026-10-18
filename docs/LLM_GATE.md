# LLM Decision Gate

**Purpose.** Let an LLM decide **which markets to enter**. That is its only
job. Strategy choice, pricing, sizing, and exits stay deterministic and
quantitative.

Code: `src/kst/llm/`.

- `packet.py` builds the compact decision packet and parses the reply.
- `providers.py` holds the providers: OpenAI-compatible (Ollama, LM Studio,
  vLLM, llama.cpp server, OpenAI) and Anthropic.
- `gate.py` holds `LLMGate`: invocation policy, cache, budgets, and fallback.

## Modes (`llm.mode`)

| Mode | Effect |
|---|---|
| `off` | The gate is bypassed. Strategies enter on their own signals, per the router. |
| `advisory` | The LLM runs, and its verdicts show in the UI and scanner, but they do not block trades. Use this to evaluate the LLM. |
| `gate` | New entries in markets within `llm.scope` need an ENTER verdict. |

`llm.scope`:

- `all` (default): every new entry needs a verdict.
- `unmodeled_only`: only markets without a quant model need a verdict.

**When the gate is asked.** Only when a strategy has already decided it wants
to *enter* a market: its `Decision` contains an opening take or an entry quote.
Exits, re-quotes of existing positions, and strategies in
`llm.bypass_strategies` (default: `structural_arb`, since a hedged arb basket has
no directional view to screen) never consult the LLM. While a verdict is
pending, the entry is held back and exits continue.

## Token frugality: how the gate avoids burning tokens

1. **Deterministic pre-filter.** Only markets where a strategy already found
   after-fee edge, inside the universe filters and risk headroom (including
   the daily market limit), are sent.
2. **Scope.** `unmodeled_only` keeps quant-modelled markets away from the LLM
   entirely.
3. **Verdict cache.** The key is `(ticker, state_bucket)`, where
   `state_bucket` is `(mid rounded to 5¢, spread bucket, time-to-close bucket,
   edge sign)`. The TTL defaults to 45 min. A verdict is re-requested only on
   a material change.
4. **Batching.** Up to `llm.batch_size` (default 15) markets go in one call.
   A call fires when pending ≥ `min_batch` (default 5) or the oldest pending
   is older than `max_wait_s` (default 20 s, because entry edges are
   perishable), with at least `min_interval_s` (default 20 s) between calls.
5. **Compact format.** A pipe-delimited table with short numeric IDs, prices
   as cents, abbreviated categories, and titles truncated to 70 characters.
   The `want` column says which side the strategy wants and its edge in
   cents. The `note` column (at most 160 characters) carries model detail.
6. **Constrained output.** `{"d":[{"i":<id>,"v":"E"|"S","c":0-100,"s":"Y"|"N"|"-","r":"<≤12 words>"}]}`.
   `max_tokens` is about `20 + 28 × rows`. The model is asked for no prose.
7. **Cache-friendly prompt.** The system prompt is static. Anthropic gets
   `cache_control` on it. OpenAI caches long prefixes automatically. Ollama
   keeps the KV cache warm with `keep_alive`, and Qwen3 gets `/no_think`, so
   no reasoning tokens are spent on a classification task.
8. **Hard budgets.** `llm.budget.max_calls_per_day`, `max_input_tokens_per_day`,
   and `max_output_tokens_per_day` are tracked from provider usage fields.
   When the budget is exhausted, `llm.fallback` applies: `deny` refuses gated
   entries, `allow` lets them through.
9. **Local first.** The default provider is Ollama at
   `http://localhost:11434/v1`. A cloud provider can be set as `escalation`:
   it is used only when the local model is unreachable, if
   `llm.escalate_on_unavailable: true`.

Typical cost: 15 markets ≈ 1.1k input tokens + ~0.45k output tokens per call.
At the default limit of 40 calls/day, that is under 65k tokens/day, or $0 on
a local Qwen.

## Claude provider

`kind: anthropic` uses the official `anthropic` SDK:

- `model` defaults to `claude-opus-5-5` (set `claude-haiku-5-5` for lower
  cost).
- `output_config` uses `effort: "low"` and a JSON-schema `format`
  (`REPLY_SCHEMA` in `packet.py`), so replies always parse.
- The system prompt carries `cache_control: ephemeral`. Caching only engages
  once the prefix is long enough for the model's minimum cacheable length.
  Below that it is a no-op, not an error.
- `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) lets the
  server retry a refusal on a fallback model. A `stop_reason: "refusal"` is
  recorded as SKIP for every row.

## Packet example

```
You screen prediction-market candidates for a systematic trading bot... (static system prompt)

T=2026-10-18T14:02Z cols: i|mkt|cat|close|bid|ask|vol24|oi|chg24|fair|want|note
1|NYC high >61F Oct18|WX|3.1h|1|2|5k|12k|-3|0.4|BUY_NO +0.9|ens mu58.2 sd1.4
2|Fed cuts Dec?|ECO|52d|31|33|80k|1.2M|+4|-|BUY_YES +1.5|
3|Chiefs win vs Raiders?|SPT|1.2d|71|72|210k|900k|+2|-|BUY_NO +1.1|
Reply JSON only.
```

## Fallbacks and failure handling

- **Malformed or partial JSON.** Rows that cannot be parsed become `SKIP`
  ("unparseable reply → skip") and are cached like any verdict. There is no
  re-ask, which saves tokens.
- **Provider unreachable or timeout** (`provider.timeout_s`, default 60 s).
  Rows stay pending. Retries back off exponentially:
  `min_interval_s × 2^failures`, capped at 32×, about 10 minutes. After 3
  consecutive failures, a Discord alert fires and gated entries follow
  `llm.fallback`. With `escalate_on_unavailable: true`, the `escalation`
  provider is tried first.
- Every decision is persisted in `llm_decisions` with the token usage and the
  packet hash, so you can audit whether the LLM adds value.
  Compare the realized P&L of LLM-approved vs LLM-skipped markets in advisory
  mode before switching to gate mode.

## Manual workflow

- `kst llm-packet --top 20` prints the packet to stdout. Paste it into
  ChatGPT or Claude.
- The UI button **Copy LLM packet** puts the same text on the clipboard.
