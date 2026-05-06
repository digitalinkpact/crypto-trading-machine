"""Async OpenAI reasoner with a tiny in-memory cache.

Never call this on the order-placement path. Run via the scheduler or
pre-compute, then pass the result to agents as plain data.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from openai import AsyncOpenAI

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


class LLMReasoner:
    def __init__(self, model: Optional[str] = None) -> None:
        settings = get_settings()
        self._model = model or settings.openai_model
        api_key = settings.openai_api_key.get_secret_value()
        self._client = AsyncOpenAI(api_key=api_key) if api_key else None
        self._cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(system: str, user: str, model: str) -> str:
        return hashlib.sha256(f"{model}\n{system}\n{user}".encode()).hexdigest()

    async def reason(self, system: str, user: str) -> dict[str, Any]:
        """Return a JSON object: {"action": BUY|SELL|HOLD, "confidence": 0..1, "rationale": str}."""
        if self._client is None:
            log.warning("OPENAI_API_KEY not set; returning HOLD")
            return {"action": "HOLD", "confidence": 0.0, "rationale": "llm-disabled"}

        cache_key = self._key(system, user, self._model)
        if cache_key in self._cache:
            return self._cache[cache_key]

        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        try:
            parsed = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            parsed = {"action": "HOLD", "confidence": 0.0, "rationale": "parse-error"}
        self._cache[cache_key] = parsed
        return parsed
