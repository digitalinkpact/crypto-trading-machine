"""Multi-provider async LLM reasoner with a tiny in-memory cache.

Supports DeepSeek, OpenAI, Groq, Gemini — all via OpenAI-compatible APIs.
Never call this on the order-placement path. Run via the scheduler.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


def _provider_config() -> tuple[str, str, str, Optional[str]]:
    """Return (provider, base_url, model, api_key) for the configured provider."""
    s = get_settings()
    p = (s.llm_provider or "none").lower()
    if p == "deepseek":
        return p, s.deepseek_base_url, s.deepseek_model, s.deepseek_api_key.get_secret_value() or None
    if p == "groq":
        return p, s.groq_base_url, s.groq_model, s.groq_api_key.get_secret_value() or None
    if p == "gemini":
        return p, s.gemini_base_url, s.gemini_model, s.gemini_api_key.get_secret_value() or None
    if p == "github":
        return p, s.github_base_url, s.github_model, s.github_token.get_secret_value() or None
    if p == "openai":
        return p, "https://api.openai.com/v1", s.openai_model, s.openai_api_key.get_secret_value() or None
    return "none", "", "", None


class LLMReasoner:
    def __init__(self, model: Optional[str] = None) -> None:
        self._provider, base_url, default_model, api_key = _provider_config()
        self._model = model or default_model
        if api_key and base_url:
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            self._client = None
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @staticmethod
    def _key(system: str, user: str, model: str) -> str:
        return hashlib.sha256(f"{model}\n{system}\n{user}".encode()).hexdigest()

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Tolerant JSON extraction — some providers wrap output in ```json fences."""
        text = (text or "").strip()
        if not text:
            return {}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = m.group(0) if m else text
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return {}

    async def reason(self, system: str, user: str) -> dict[str, Any]:
        """Return a JSON object: {"action": BUY|SELL|HOLD, "confidence": 0..1, "rationale": str}."""
        if self._client is None:
            return {"action": "HOLD", "confidence": 0.0,
                    "rationale": f"llm-disabled ({self._provider})"}

        cache_key = self._key(system, user, self._model)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Most OpenAI-compatible providers (DeepSeek, OpenAI, Groq) support
        # response_format=json_object. Gemini's compat layer ignores it harmlessly.
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001
            # Retry once without response_format for providers that reject it.
            log.debug("llm json_object mode failed (%s); retrying plain", exc)
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.2,
                    max_tokens=200,
                )
            except Exception as exc2:  # noqa: BLE001
                log.warning("llm call failed (%s): %s", self._provider, exc2)
                return {"action": "HOLD", "confidence": 0.0,
                        "rationale": f"llm-error: {exc2}"}

        content = resp.choices[0].message.content if resp.choices else ""
        parsed = self._extract_json(content or "")
        if not parsed:
            parsed = {"action": "HOLD", "confidence": 0.0, "rationale": "parse-error"}
        self._cache[cache_key] = parsed
        return parsed
