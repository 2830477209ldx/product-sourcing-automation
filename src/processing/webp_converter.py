"""WebP converter — single and batch image conversion."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from loguru import logger


def convert_to_webp(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    quality: int = 85,
) -> Path:
    """Convert a single image to WebP format.

    Args:
        input_path: Source image file.
        output_dir: Output directory (defaults to same as input).
        quality: WebP quality (1-100), 85 is good balance.

    Returns:
        Path to the .webp file.
    """
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"Image not found: {src}")

    img = Image.open(src)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")

    out_dir = Path(output_dir) if output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{src.stem}.webp"

    save_kwargs = {"quality": quality, "method": 6}
    if img.mode == "RGBA":
        img.save(out_path, "WEBP", **save_kwargs, lossless=False)
    else:
        img.save(out_path, "WEBP", **save_kwargs)

    orig_size = src.stat().st_size
    webp_size = out_path.stat().st_size
    ratio = webp_size / orig_size * 100 if orig_size else 0
    logger.info(f"WebP: {src.name} → {out_path.name} ({ratio:.0f}%)")
    return out_path


def batch_convert_to_webp(
    image_paths: list[str | Path],
    output_dir: str | Path,
    quality: int = 85,
) -> list[Path]:
    """Convert multiple images to WebP.

    Args:
        image_paths: List of source image paths.
        output_dir: Output directory for .webp files.
        quality: WebP quality (1-100).

    Returns:
        List of output .webp file paths.
    """
    results: list[Path] = []
    for path in image_paths:
        try:
            out = convert_to_webp(path, output_dir, quality)
            results.append(out)
        except Exception as exc:
            logger.warning(f"WebP convert failed for {path}: {exc}")
    return results
