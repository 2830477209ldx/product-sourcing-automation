"""External image processing API client — supports Gemini + generic multipart."""
from __future__ import annotations

import base64
import json as json_module
import mimetypes
import os
from pathlib import Path

import httpx
from loguru import logger

from src.config import config

GEMINI_HOSTS = ("generativelanguage.googleapis.com", "googleapis.com", "ai.google.dev")


class ImageAPIClient:
    """Send images to processing API, receive processed images back.

    Supports two backends:
    - Gemini (auto-detected): uses OpenAI-compatible chat completions with base64 images
    - Generic: POST multipart/form-data to /process endpoint
    """

    def __init__(self) -> None:
        cfg = config.image_api
        self.base_url = (
            cfg.get("base_url", "").rstrip("/")
            or os.getenv("IMAGE_API_BASE_URL", "").rstrip("/")
        )
        self.api_key = (
            cfg.get("api_key", "")
            or os.getenv("IMAGE_API_KEY", "")
            or config.ai.get("api_key", "")
            or None
        )
        self.model = cfg.get("model", "gemini-2.5-flash")
        self.timeout = cfg.get("timeout", 120)
        self._is_gemini = any(h in self.base_url for h in GEMINI_HOSTS)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def process(self, image_path: str | Path, prompt: str | None = None) -> bytes:
        """Upload an image to the processing API, return processed image bytes."""
        if not self.configured:
            raise RuntimeError("Image API not configured (set IMAGE_API_BASE_URL in .env)")

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {path}")

        if self._is_gemini:
            return await self._process_gemini(path, prompt)
        return await self._process_multipart(path, prompt)

    # ── Gemini backend ──────────────────────────────────────

    async def _process_gemini(self, path: Path, prompt: str | None = None) -> bytes:
        content = path.read_bytes()
        ext = path.suffix.lower().replace(".", "") or "jpg"
        mime = f"image/{ext}" if ext in ("jpg", "jpeg", "png", "webp", "gif") else "image/jpeg"
        image_b64 = base64.b64encode(content).decode("utf-8")

        user_text = prompt or "Process this product image for US e-commerce: background removal, color enhancement, 2048x2048."

        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            }],
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return self._extract_gemini_image(data, path)
            except httpx.HTTPStatusError as exc:
                if attempt < 2 and exc.response.status_code >= 500:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                detail = exc.response.text[:500] if exc.response else str(exc)
                logger.error(f"Gemini API HTTP {exc.response.status_code}: {detail}")
                raise
            except Exception as exc:
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error(f"Gemini API failed: {exc}")
                raise

    def _extract_gemini_image(self, data: dict, path: Path) -> bytes:
        """Extract image bytes from a Gemini chat completions response."""
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Gemini returned no choices")

        message = choices[0].get("message", {})
        msg_content = message.get("content", "")

        # Case 1: content is a list of parts (multimodal response with images)
        if isinstance(msg_content, list):
            for part in msg_content:
                if isinstance(part, dict):
                    # Gemini inline image data
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:image"):
                            b64 = url.split(",", 1)[1] if "," in url else url
                            return base64.b64decode(b64)
                    # Gemini native format: inline_data
                    if "inline_data" in part:
                        b64 = part["inline_data"].get("data", "")
                        if b64:
                            return base64.b64decode(b64)
                    # Gemini native format: base64 in text
                    if "text" in part and isinstance(part["text"], str):
                        text = part["text"]
                        if text.startswith("data:image"):
                            b64 = text.split(",", 1)[1] if "," in text else text
                            return base64.b64decode(b64)

        # Case 2: content is a plain string (text only, no image returned)
        if isinstance(msg_content, str):
            # Check if the text contains a base64 image data URL
            if "data:image" in msg_content:
                import re
                match = re.search(r"data:image/\w+;base64,([A-Za-z0-9+/=]+)", msg_content)
                if match:
                    return base64.b64decode(match.group(1))
            # No image in response — return original image as fallback
            logger.warning(f"Gemini returned text-only response for {path.name}, returning original")
            return path.read_bytes()

        # Last resort: return original image
        logger.warning(f"Could not extract image from Gemini response for {path.name}")
        return path.read_bytes()

    # ── Generic multipart backend ────────────────────────────

    async def _process_multipart(self, path: Path, prompt: str | None = None) -> bytes:
        content = path.read_bytes()
        ext = path.suffix.lower().replace(".", "") or "jpg"
        mime = f"image/{ext}" if ext in ("jpg", "jpeg", "png", "webp") else "image/jpeg"

        files = {"image": (path.name, content, mime)}
        if prompt:
            files["prompt"] = ("prompt.txt", prompt.encode("utf-8"), "text/plain")
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/process",
                        files=files,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    result = resp.content
                    if len(result) < 100:
                        raise RuntimeError(f"API returned too-small response ({len(result)} bytes)")
                    logger.info(f"Image API processed: {path.name} ({len(result)} bytes)")
                    return result
            except httpx.HTTPStatusError as exc:
                if attempt < 2 and exc.response.status_code >= 500:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error(f"Image API HTTP {exc.response.status_code}: {exc}")
                raise
            except Exception as exc:
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error(f"Image API failed: {exc}")
                raise

    async def process_batch(
        self, image_paths: list[str | Path], concurrency: int = 3
    ) -> dict[str, bytes]:
        """Process multiple images concurrently, return {filename: bytes}."""
        import asyncio

        sem = asyncio.Semaphore(concurrency)

        async def _one(path: str | Path) -> tuple[str, bytes]:
            async with sem:
                data = await self.process(path)
                return Path(path).name, data

        tasks = [_one(p) for p in image_paths]
        results = {}
        for coro in asyncio.as_completed(tasks):
            name, data = await coro
            results[name] = data
            logger.info(f"  Processed: {name}")
        return results
