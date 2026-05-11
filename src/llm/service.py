"""Unified LLM service — single async OpenAI client, shared by all modules."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from loguru import logger


class LLMService:
    """Central async LLM client. All AI calls go through this one service."""

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1.0

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model_text: str = "deepseek-chat",
        model_vision: str = "deepseek-chat",
        temperature: float = 0.3,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
        )
        self.model_text = model_text
        self.model_vision = model_vision
        self.temperature = temperature

    # ── Core call ────────────────────────────────────────────

    async def _retry_call(self, messages: list[dict], model: str, temperature: float, max_tokens: int) -> str:
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content or ""
            except (APIError, APITimeoutError) as exc:
                last_exc = exc
                wait = self.RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"LLM API error (attempt {attempt + 1}/{self.MAX_RETRIES}): {exc}. Retrying in {wait}s...")
                await asyncio.sleep(wait)
            except RateLimitError as exc:
                last_exc = exc
                wait = self.RETRY_BASE_DELAY * (4 ** attempt)
                logger.warning(f"LLM rate limited (attempt {attempt + 1}/{self.MAX_RETRIES}). Waiting {wait}s...")
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def chat(
        self,
        messages: list[dict],
        model_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2000,
    ) -> str:
        model = model_key or self.model_text
        return await self._retry_call(
            messages, model,
            temperature if temperature is not None else self.temperature,
            max_tokens,
        )

    async def chat_json(
        self,
        messages: list[dict],
        model_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """chat + JSON parse + error handling, all in one place."""
        raw = await self.chat(messages, model_key, temperature, max_tokens)
        return self._parse_json(raw)

    async def chat_vision(
        self,
        text_prompt: str,
        image_urls: list[str],
        model_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 1000,
    ) -> str:
        """Send text + images to vision-capable model."""
        content: list[dict] = [{"type": "text", "text": text_prompt}]
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url},
            })
        model = model_key or self.model_vision
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    # ── JSON parsing (one place for all logic) ───────────────

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("\n```", 1)[0] if "\n```" in raw else raw
            raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"LLM returned invalid JSON ({len(raw)} chars): {raw[:200]}")
            return {"_raw": raw, "_parse_error": True}
