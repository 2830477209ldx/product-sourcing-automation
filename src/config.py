from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _env_replace(value: str) -> str:
    """Replace ${ENV_VAR} placeholders with environment variable values."""

    def _replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.getenv(var_name, "")

    return re.sub(r"\$\{(\w+)\}", _replacer, value)


def _resolve_refs(data: Any) -> Any:
    """Recursively walk a dict/list and resolve env-var placeholders."""
    if isinstance(data, dict):
        return {k: _resolve_refs(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_resolve_refs(item) for item in data]
    if isinstance(data, str):
        return _env_replace(data)
    return data


class Config:
    """Singleton configuration loader.

    Loads settings.yaml and overlays settings.local.yaml if present.
    Environment variables in the form ${VAR} are expanded automatically.
    """

    _instance: Config | None = None

    def __init__(self) -> None:
        settings_path = PROJECT_ROOT / "config" / "settings.yaml"
        if not settings_path.exists():
            raise FileNotFoundError(f"Config file not found: {settings_path}")

        with open(settings_path, encoding="utf-8") as f:
            self._data: dict[str, Any] = yaml.safe_load(f) or {}

        local_path = PROJECT_ROOT / "config" / "settings.local.yaml"
        if local_path.exists():
            with open(local_path, encoding="utf-8") as f:
                local_data = yaml.safe_load(f) or {}
                _deep_merge(self._data, local_data)

        self._data = _resolve_refs(self._data)
        logger.info("Configuration loaded")

    @classmethod
    def instance(cls) -> Config:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def ai(self) -> dict[str, Any]:
        return self._data["ai"]

    @property
    def market_judge(self) -> dict[str, Any]:
        return self._data["market_judge"]

    @property
    def platforms(self) -> dict[str, Any]:
        return self._data.get("platforms", {})

    @property
    def paths(self) -> dict[str, Any]:
        return self._data["paths"]

    @property
    def shopify(self) -> dict[str, Any]:
        return self._data.get("shopify", {})

    @property
    def currency(self) -> dict[str, Any]:
        return self._data.get("currency", {"cny_to_usd": 7.2})


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


config = Config.instance()
