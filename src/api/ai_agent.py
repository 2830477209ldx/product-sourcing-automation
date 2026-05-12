"""AI Agent for intelligent web page extraction.

The agent receives DOM snapshots from the content script, decides what
interactions are needed (click, scroll, wait), and progressively extracts
structured product data.
"""
from __future__ import annotations

import json as _json
from typing import Any

from src.llm.service import LLMService

AGENT_SYSTEM_PROMPT = """You are a web extraction agent for Chinese e-commerce product pages (Taobao, Tmall, 1688).

You receive DOM snapshots. Your job: extract structured product data with the fewest steps.

TARGET OUTPUT:
{
  "title_cn": "the product title",
  "price_cn": "¥129.00",
  "image_urls": ["url", ...],
  "sku_prices": [{"name": "Black/S", "price": "¥129", "images": ["sku_img_url"]}, ...],
  "desc_images": ["url", ...],
  "description_cn": "product description text"
}

YOU CONTROL THE BROWSER by returning JSON with actions. The content script will execute them and send you the updated DOM snapshot.

RESPOND ONLY WITH THIS JSON STRUCTURE (no markdown, no explanation):

To continue exploring — return an action plan:
{"done": false, "actions": [{"type": "...", ...}]}

To finish — return the extracted data:
{"done": true, "data": { ... }}

AVAILABLE ACTIONS:

1. click — click an element
   {"type": "click", "text": "商品详情", "reason": "open detail tab"}
   "text" can be the exact visible text OR substring of a clickable element.

2. scroll — scroll the page
   {"type": "scroll", "pixels": 500, "reason": "load more detail images"}

3. wait — wait for content to load
   {"type": "wait", "ms": 2000, "reason": "wait for tab content to render"}

4. click_sku — click a specific SKU variant by index
   {"type": "click_sku", "index": 0, "reason": "select first variant to see price"}

5. extract — extract data from the CURRENT page state
   {"type": "extract", "reason": "have enough data now"}

SCOPING RULES — what to focus on and what to ignore:

- TITLE: Usually in <h1> or element with class containing "title". Extract the product name, not the shop name.
- PRICE: Look for ¥ symbol or elements with "price" in class. SKU prices are often in elements that appear AFTER clicking a SKU option.
- MAIN IMAGES: Find the product gallery/thumbnail area. Images from alicdn.com, taobaocdn.com are good. Filter out icons, logos, buttons (words like "share", "cart", "collect", "arrow" in URL = skip).
- SKU: Each variant (color/size combo) has a name AND price. The price only appears after selecting that variant. Strategy: click each SKU one by one, extract the updated price after each click.
- DESC IMAGES: These are the long detail/description images below the product info. Usually you need to: (1) click the "商品详情"/"产品详情"/"图文详情" tab, (2) scroll down to trigger lazy loading. These images are typically full-width and come from alicdn.com.
- DESCRIPTION TEXT: Collect visible text from the product info area (not navigation, not footer).

IMPORTANT RULES:
- If SKU prices are missing, you MUST click SKU options one by one to trigger price display.
- If desc_images is empty, you MUST click the detail tab then scroll.
- Never return done=true with empty required fields — keep exploring.
- After clicking a tab, wait 1-2 seconds before reading results.
- After scrolling, you may need to scroll again to load more lazy images.
"""


def _missing_fields(extracted: dict) -> list[str]:
    missing = []
    for key in ("title_cn", "price_cn", "image_urls", "desc_images", "description_cn"):
        val = extracted.get(key)
        if not val or (isinstance(val, list) and len(val) == 0):
            missing.append(key)
    if not extracted.get("sku_prices"):
        missing.append("sku_prices")
    return missing


async def agent_step(
    llm: LLMService,
    platform: str,
    round_num: int,
    dom_state: dict[str, Any],
    history: list[dict[str, Any]],
    max_rounds: int = 6,
    debug: bool = False,
) -> dict[str, Any]:
    if round_num >= max_rounds:
        return {
            "done": True,
            "data": dom_state.get("extracted", {}),
            "warning": f"Reached max rounds ({max_rounds})",
        }

    # Build history summary
    history_lines = []
    for h in history[-6:]:
        act = h.get("action", {})
        res = str(h.get("result", ""))[:200]
        history_lines.append(f"  Round {h['round']}: {act.get('type','?')} → {res}")

    missing = _missing_fields(dom_state.get("extracted", {}))

    user_msg_parts = [
        f"Platform: {platform}",
        f"Round: {round_num}/{max_rounds}",
        "",
        "=== DOM STATE ===",
        _json.dumps(dom_state, ensure_ascii=False, indent=2),
        "",
        "=== ACTION HISTORY ===",
        "\n".join(history_lines) if history_lines else "  No actions taken yet.",
        "",
        f"=== MISSING DATA: {', '.join(missing) if missing else 'all fields extracted'} ===",
    ]
    user_msg = "\n".join(user_msg_parts)

    # Truncate if too long (~12000 chars max for user message)
    if len(user_msg) > 12000:
        dom_json = _json.dumps(dom_state, ensure_ascii=False, indent=1)
        dom_truncated = dom_json[:8000] + (
            "\n... (truncated, " + str(len(dom_json) - 8000) + " chars omitted)" if len(dom_json) > 8000 else ""
        )
        user_msg = "\n".join([
            f"Platform: {platform}",
            f"Round: {round_num}/{max_rounds}",
            "",
            "=== DOM STATE (truncated) ===",
            dom_truncated,
            "",
            f"=== MISSING: {', '.join(missing)} ===",
        ])

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    if debug:
        from loguru import logger
        logger.info(
            f"[AI Agent] Step {round_num} | platform={platform} | "
            f"msg_len={len(user_msg)} | missing={missing}"
        )

    try:
        result = await llm.chat_json(messages, max_tokens=3000)
    except Exception as exc:
        return {"done": True, "data": dom_state.get("extracted", {}), "error": f"LLM error: {exc}"}

    if result.get("_parse_error"):
        return {
            "done": True,
            "data": dom_state.get("extracted", {}),
            "error": f"LLM parse error: {result.get('_parse_error')}",
        }

    if debug:
        logger.info(
            f"[AI Agent] Step {round_num} response | done={result.get('done')} | "
            f"actions={len(result.get('actions', []))} | "
            f"data_keys={list(result.get('data', {}).keys()) if result.get('data') else 'none'}"
        )
        result["_debug"] = {
            "platform": platform,
            "round": round_num,
            "user_msg_len": len(user_msg),
            "user_msg_preview": user_msg[:500] + ("..." if len(user_msg) > 500 else ""),
            "response_preview": _json.dumps(result, ensure_ascii=False)[:500],
        }

    return result
