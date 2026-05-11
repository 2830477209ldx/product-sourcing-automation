from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from loguru import logger


class ImageStyler:
    """Adapt product images for US market aesthetics.

    Capabilities:
      - Clean background enhancement (whiten/gradient backgrounds)
      - Product-centered composition (smart cropping)
      - English text overlay support
      - US-friendly color grading (warm, bright, clean)
    """

    OUTPUT_SIZE = (2048, 2048)
    MARGIN = 80

    def adapt(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        title_en: str = "",
        brand_name: str = "",
    ) -> Path:
        """Full US market adaptation of a product image."""
        img = cv2.imread(str(input_path))
        if img is None:
            logger.warning(f"Cannot read: {input_path}")
            return Path(input_path)

        img = self._enhance_background(img)
        img = self._smart_center_crop(img)
        img = self._us_color_grade(img)

        if title_en:
            img = self._add_english_overlay(img, title_en, brand_name)

        img = self._resize_to_shopify(img)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{Path(input_path).stem}_us.jpg"
        output_path = output_dir / out_name
        cv2.imwrite(str(output_path), img, [cv2.IMWRITE_JPEG_QUALITY, 92])

        logger.info(f"US-styled image: {output_path}")
        return output_path

    def adapt_batch(
        self,
        image_paths: list[str | Path],
        output_dir: str | Path,
        **kwargs: object,
    ) -> list[Path]:
        return [self.adapt(p, output_dir, **kwargs) for p in image_paths]

    # ── Private methods ──────────────────────────────────────

    @staticmethod
    def _load_font(size: int) -> ImageFont.FreeTypeFont:
        for path in ("arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        raise OSError("No usable font found")

    def _enhance_background(self, img: np.ndarray) -> np.ndarray:
        """Lightly whiten the background for clean e-commerce look."""
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        value = hsv[:, :, 2]
        background_mask = (saturation < 40) & (value > 180)

        if background_mask.sum() > img.shape[0] * img.shape[1] * 0.05:
            img_float = img.astype(np.float32)
            img_float[background_mask] *= [1.08, 1.08, 1.08]
            img_float[background_mask] += [20, 20, 20]
            img = np.clip(img_float, 0, 255).astype(np.uint8)

        return img

    def _smart_center_crop(self, img: np.ndarray) -> np.ndarray:
        """Crop to center the product, removing excess background."""
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (21, 21), 0)
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return img

        largest = max(contours, key=cv2.contourArea)
        x, y, cw, ch = cv2.boundingRect(largest)

        x1 = max(0, x - self.MARGIN)
        y1 = max(0, y - self.MARGIN)
        x2 = min(w, x + cw + self.MARGIN)
        y2 = min(h, y + ch + self.MARGIN)

        if (x2 - x1) > w * 0.3 and (y2 - y1) > h * 0.3:
            return img[y1:y2, x1:x2]

        return img

    def _us_color_grade(self, img: np.ndarray) -> np.ndarray:
        """Apply US-market-friendly color grading: slightly warm, punchy contrast."""
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)

        from PIL import ImageEnhance

        for method, factor in [
            (ImageEnhance.Brightness, 1.06),
            (ImageEnhance.Contrast, 1.14),
            (ImageEnhance.Color, 1.04),
            (ImageEnhance.Sharpness, 1.18),
        ]:
            pil_img = method(pil_img).enhance(factor)

        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    def _add_english_overlay(
        self, img: np.ndarray, title: str, brand: str
    ) -> np.ndarray:
        """Add English title/brand overlay at the bottom."""
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)

        h = pil_img.height
        overlay_h = min(80, h // 10)
        overlay = Image.new("RGBA", (pil_img.width, overlay_h), (0, 0, 0, 140))
        pil_img.paste(overlay, (0, h - overlay_h), overlay)

        try:
            font = self._load_font(28)
        except OSError:
            font = ImageFont.load_default()

        short_title = title[:60] if len(title) > 60 else title
        draw.text((16, h - overlay_h + 12), short_title, fill=(255, 255, 255), font=font)

        if brand:
            draw.text(
                (pil_img.width - 200, h - overlay_h + 12),
                brand,
                fill=(200, 200, 200),
                font=font,
            )

        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    def _resize_to_shopify(self, img: np.ndarray) -> np.ndarray:
        """Resize to max Shopify dimensions while maintaining aspect ratio."""
        h, w = img.shape[:2]
        max_w, max_h = self.OUTPUT_SIZE

        if w <= max_w and h <= max_h:
            return img

        ratio = min(max_w / w, max_h / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
