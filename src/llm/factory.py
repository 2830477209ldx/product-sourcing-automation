"""LLMService factory — single constructor, used everywhere."""

from __future__ import annotations

from src.config import Config
from src.llm.service import LLMService


def create_llm_service(temperature: float | None = None) -> LLMService:
    cfg = Config.instance()
    return LLMService(
        api_key=cfg.ai["api_key"],
        base_url=cfg.ai.get("base_url", ""),
        model_text=cfg.ai.get("model_text", "deepseek-chat"),
        model_vision=cfg.ai.get("model_vision", "deepseek-chat"),
        temperature=temperature if temperature is not None else cfg.ai.get("temperature", 0.3),
        provider=cfg.ai.get("provider", "deepseek"),
    )
