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
| `off` | The gate is bypassed. Only quant-modelled markets trade, per the router. |
| `advisory` | The LLM runs, and its verdicts show in the UI and scanner, but they do not block trades. Use this to evaluate the LLM. |
| `gate` | New entries in markets within `llm.scope` need an ENTER verdict. |

`llm.scope`:

- `unmodeled_only` (default): only markets without a quant model need a verdict.
- `all`: every new entry needs a verdict.

## Token frugality: how the gate avoids burning tokens

1. **Deterministic pre-filter.** Only scanner candidates that already passed
   the universe filters, price bands, liquidity, and risk pre-checks are
   considered.
2. **Scope.** By default, markets with a quant model never reach the LLM.
3. **Verdict cache.** The key is `(ticker, state_bucket)`, where
   `state_bucket` is `(mid rounded to 5¢, spread bucket, time-to-close bucket,
   edge sign)`. The TTL defaults to 45 min. A verdict is re-requested only on
   a material change.
4. **Batching.** Up to `llm.batch_size` (default 15) markets go in one call.
   A call fires when pending ≥ `min_batch` (default 5) or the oldest pending
   is older than `max_wait_s` (default 120 s), with at least
   `min_interval_s` (default 60 s) between calls.
5. **Compact format.** A pipe-delimited table with short numeric IDs, prices
   as cents, abbreviated categories, and titles truncated to 60 characters.
   Rules text is included only as a 160-character excerpt.
6. **Constrained output.** `{"d":[{"i":<id>,"v":"E"|"S","c":0-100,"s":"Y"|"N"|"-","r":"<≤12 words>"}]}`.
   `max_tokens` is about `20 + 28 × rows`. The model is asked for no prose.
7. **Cache-friendly prompt.** The system prompt is static. Anthropic gets
   `cache_control` on it. OpenAI caches long prefixes automatically. Ollama
   keeps the KV cache warm with `keep_alive`.
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

## Packet example

```
You screen prediction-market candidates for a systematic trading bot... (static system prompt)

T=2026-10-18T14:02Z cols: i|mkt|cat|close|bid|ask|vol24|oi|chg24|fair|note
1|NYC high >61F Oct18|WX|3.1h|1|2|5k|12k|-3|0.4|ens mu58.2 sd1.4
2|Fed cuts Dec?|ECO|52d|31|33|80k|1.2M|+4|-|
3|Chiefs win vs Raiders?|SPT|1.2d|71|72|210k|900k|+2|-|
Reply JSON only.
```

## Fallbacks and failure handling

- Malformed JSON gets one repair attempt (a strict re-ask with only the IDs).
  If that also fails, every row is `SKIP` and the failure is logged.
- On timeout (default 30 s), rows stay pending. Once the escalation and
  fallback policies are exhausted, they are treated per `llm.fallback`.
- Every decision is persisted in `llm_decisions` with the token usage and the
  packet hash, so you can audit whether the LLM adds value.
  Compare the realized P&L of LLM-approved vs LLM-skipped markets in advisory
  mode before switching to gate mode.

## Manual workflow

- `kst llm-packet --top 20` prints the packet to stdout. Paste it into
  ChatGPT or Claude.
- The UI button **Copy LLM packet** puts the same text on the clipboard.
