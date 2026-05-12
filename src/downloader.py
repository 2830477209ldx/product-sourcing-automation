"""Shared async image downloader with SSRF protection — single source of truth."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path

import httpx
from loguru import logger

IMAGE_DIR = Path("data/images")

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}


def _is_safe_url(url: str) -> bool:
    """Block private/loopback/reserved/multicast IPs (SSRF protection)."""
    if not url.startswith("http"):
        return False
    try:
        parsed = httpx.URL(url)
        host = parsed.host
        if not host:
            return False
        addr = socket.getaddrinfo(host, None)[0][4][0]
        ip = ipaddress.ip_address(addr)
        return not (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast)
    except (socket.gaierror, ValueError):
        return False


def _extract_extension(url: str) -> str:
    """Extract file extension from URL, defaulting to 'jpg'."""
    ext = url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
    return ext if ext.lower() in ALLOWED_EXTENSIONS else "jpg"


def _sanitize_sku_name(raw_name: str) -> str:
    """Sanitize a SKU name for use as a filename component."""
    name = re.sub(r"[^\w\s\-.]", "", str(raw_name))
    name = re.sub(r"\s+", "-", name).strip("-.")
    return name or "sku"


async def download_images(
    folder: str, urls: list[str], name_prefix: str = ""
) -> list[str]:
    """Download images from URLs to data/images/{folder}/, return local paths.

    If name_prefix is given, files are named {name_prefix}_{01}.{ext};
    otherwise {000}.{ext}.
    """
    if not urls:
        return []
    img_dir = IMAGE_DIR / folder
    img_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []

    for i, url in enumerate(urls):
        if not _is_safe_url(url):
            continue
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    continue
                content = await resp.aread()
                if len(content) > MAX_IMAGE_BYTES:
                    continue

            ext = _extract_extension(url)
            if name_prefix:
                local_path = img_dir / f"{name_prefix}_{i+1:02d}.{ext}"
            else:
                local_path = img_dir / f"{i:03d}.{ext}"
            local_path.write_bytes(content)
            local_paths.append(str(local_path))
        except Exception:
            pass

    logger.info(f"  Downloaded {len(local_paths)}/{len(urls)} images")
    return local_paths


async def download_sku_images(
    folder: str, sku_prices: list[dict]
) -> list[str]:
    """Download SKU-specific images, naming them by SKU name."""
    if not sku_prices:
        return []
    img_dir = IMAGE_DIR / folder
    img_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []

    for sku in sku_prices:
        sku_name = _sanitize_sku_name(sku.get("name", "sku"))
        sku_images = sku.get("images", [])
        if not isinstance(sku_images, list):
            continue
        for j, url in enumerate(sku_images):
            if not isinstance(url, str) or not _is_safe_url(url):
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, follow_redirects=True)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        continue
                    content = await resp.aread()
                    if len(content) > MAX_IMAGE_BYTES:
                        continue

                ext = _extract_extension(url)
                if len(sku_images) <= 1:
                    local_path = img_dir / f"{sku_name}.{ext}"
                else:
                    local_path = img_dir / f"{sku_name}_{j+1:02d}.{ext}"
                local_path.write_bytes(content)
                local_paths.append(str(local_path))
            except Exception:
                pass

    return local_paths
