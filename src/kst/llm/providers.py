"""LLM providers for the entry gate.

* OpenAICompatibleProvider — local runtimes (Ollama, LM Studio, vLLM, llama.cpp server) or any
  OpenAI-compatible endpoint, over plain HTTP.
* AnthropicProvider — Claude via the official `anthropic` SDK: static system prompt with cache_control,
  structured JSON output, low effort (classification route), server-side refusal fallbacks enabled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from kst.config import ProviderCfg
from kst.core.logging import get_logger
from kst.llm.packet import REPLY_SCHEMA

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str
    latency_ms: float
    cached_tokens: int = 0
    refused: bool = False


class LLMUnavailableError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, user: str, max_tokens: int) -> LLMResult: ...

    async def available(self) -> bool: ...


class OpenAICompatibleProvider:
    def __init__(self, cfg: ProviderCfg, http: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.name = "openai_compatible"
        self.model = cfg.model
        self.base = cfg.base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if cfg.api_key is not None:
            headers["Authorization"] = f"Bearer {cfg.api_key.get_secret_value()}"
        self.http = http or httpx.AsyncClient(timeout=cfg.timeout_s, headers=headers)

    async def available(self) -> bool:
        try:
            r = await self.http.get(f"{self.base}/models", timeout=3.0)
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    async def complete(self, system: str, user: str, max_tokens: int) -> LLMResult:
        # Qwen3-family models think by default; the /no_think switch keeps the gate fast and cheap.
        if "qwen3" in self.model.lower() and "/no_think" not in user:
            user = user + " /no_think"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
            **self.cfg.extra_body,
        }
        if "11434" in self.base:  # Ollama keeps the model (and KV cache for the static prefix) warm
            body.setdefault("keep_alive", self.cfg.keep_alive)
        t0 = time.monotonic()
        try:
            r = await self.http.post(f"{self.base}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"{self.base}: {exc}") from exc
        if r.status_code >= 400:
            raise LLMUnavailableError(f"{self.base}: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        usage = data.get("usage") or {}
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            text=msg.get("content") or "",
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            provider=self.name,
            model=data.get("model") or self.model,
            latency_ms=(time.monotonic() - t0) * 1000,
            cached_tokens=int(details.get("cached_tokens") or 0),
        )


class AnthropicProvider:
    """Claude via the official SDK. Default model claude-opus-5-5 (set provider.model to change it)."""

    def __init__(self, cfg: ProviderCfg) -> None:
        import anthropic

        self.cfg = cfg
        self.name = "anthropic"
        self.model = cfg.model if cfg.model.startswith("claude") else "claude-opus-5-5"
        kwargs: dict[str, Any] = {"timeout": cfg.timeout_s, "max_retries": 2}
        if cfg.api_key is not None:
            kwargs["api_key"] = cfg.api_key.get_secret_value()
        if (
            cfg.base_url
            and "anthropic.com" not in cfg.base_url
            and not cfg.base_url.startswith("http://localhost")
        ):
            kwargs["base_url"] = cfg.base_url
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def available(self) -> bool:
        return True  # credentials are resolved lazily by the SDK; failures surface as LLMUnavailableError

    async def complete(self, system: str, user: str, max_tokens: int) -> LLMResult:
        a = self._anthropic
        t0 = time.monotonic()
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                # Thinking is always on for current Claude models and counts toward max_tokens: leave headroom.
                max_tokens=max(max_tokens * 4, 4000),
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError as exc:
            raise LLMUnavailableError(f"anthropic rate limited: {exc}") from exc
        except a.APIStatusError as exc:
            raise LLMUnavailableError(f"anthropic HTTP {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMUnavailableError(f"anthropic connection error: {exc}") from exc
        latency = (time.monotonic() - t0) * 1000
        u = resp.usage
        tin = (
            int(u.input_tokens or 0)
            + int(getattr(u, "cache_read_input_tokens", 0) or 0)
            + int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        )
        if resp.stop_reason == "refusal":
            log.warning("llm_refusal", model=resp.model, details=str(getattr(resp, "stop_details", "")))
            return LLMResult("", tin, int(u.output_tokens or 0), self.name, resp.model, latency, refused=True)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(
            text=text,
            tokens_in=tin,
            tokens_out=int(u.output_tokens or 0),
            provider=self.name,
            model=resp.model,
            latency_ms=latency,
            cached_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        )


def build_provider(cfg: ProviderCfg) -> LLMProvider:
    if cfg.kind == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAICompatibleProvider(cfg)
