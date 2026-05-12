"""AI Agent — Conversational DOM Drilling.

The agent explores a product page progressively: it sees top-level DOM nodes first,
then "drills down" into specific paths that look relevant, accumulating context
until it can extract all product data. No scrolling — only SKU clicks for prices.
"""
from __future__ import annotations

import json as _json
from typing import Any

from src.llm.service import LLMService

AGENT_SYSTEM_PROMPT = """You are a DOM exploration agent for Chinese e-commerce product pages (Taobao, Tmall, 1688, AliExpress).

## HOW IT WORKS

You explore the page DOM PROGRESSIVELY — like a developer using DevTools:
1. You start by seeing body's top-level children (tag, class, text preview, image count, dimensions)
2. You choose which node to "expand" to see ITS children
3. You keep drilling deeper until you find the data you need
4. Once you locate SKU buttons, you click them one by one to collect prices

## PATH SYSTEM

Nodes are addressed by index path:
- "root" = document.body's direct visible children
- "2" = body's 3rd visible child
- "2.0" = that node's 1st visible child
- "2.0.3" = go one level deeper

Each node summary shows: path, tag, cls (class), text (preview), imgs (image count), rect (w/h/t), child_count, has_sku, has_price

## YOUR ACTIONS

Respond with ONLY valid JSON (no markdown):

To continue: {"done": false, "actions": [...]}
To finish:   {"done": true, "data": {...}}

Available actions (7 types):

1. expand_dom — see a node's children
   {"type": "expand_dom", "path": "2.1"}

2. click_sku — click SKU by INDEX, auto-reads price
   {"type": "click_sku", "index": 0}
   Use for simple (single-dimension) SKUs.

3. click_sku_label — click SKU by TEXT LABEL, auto-reads price
   {"type": "click_sku_label", "label": "红色"}

4. click_sku_combo — COMPOUND SKU: click MULTIPLE labels, then read price
   {"type": "click_sku_combo", "labels": ["红色", "XL"]}
   Returns price AND _sku_state showing which SKUs are now available/disabled.

5. check_sku_state — check which SKU options are currently clickable vs disabled
   {"type": "check_sku_state"}
   Returns: {available: [...], disabled: [...]}. Use this to discover valid combinations.

6. read_price — read current displayed price
   {"type": "read_price"}

7. collect_images — collect images from a node subtree
   {"type": "collect_images", "path": "2.0", "category": "image_urls"}
   category = "image_urls" (main gallery, strict filter) or "desc_images" (detail images, full collect)

## COMPOUND SKU STRATEGY

Many products have multi-dimensional SKUs (color + size). Key concept: NOT all color×size combinations are valid.
Some sizes are only available for specific colors — clicking one color may disable certain sizes.

skus_available structure:
{
  "flat": [{"index":0,"text":"红色","disabled":false}, {"index":1,"text":"XL","disabled":true}, ...],
  "groups": [
    {"dimension":"颜色","options":[{"index":0,"text":"红色"},{"index":1,"text":"蓝色"}]},
    {"dimension":"尺码","options":[{"index":6,"text":"XL"},{"index":7,"text":"2XL"}]}
  ],
  "is_compound": true,
  "available": 8,
  "disabled": 3
}

WORKFLOW for compound SKU discovery:
  Step A: Use check_sku_state to see which options are currently active
  Step B: Click a color with click_sku_label → the script returns price + _sku_state
  Step C: Read _sku_state to see which sizes are now available (not disabled) for this color
  Step D: Use click_sku_combo with valid color+size pairs: {"type":"click_sku_combo","labels":["红色","XL"]}
  Step E: Repeat for each valid combination you discover
  Step F: If some combinations fail (SKU label not found = that combo doesn't exist), skip them

You can issue MULTIPLE click_sku_combo actions per round to collect several combinations at once.

## STRATEGY

Round 0: expand_dom on 2-3 top-level nodes that look like product content.

Round 1-2: Continue expanding BUT start collecting AS SOON AS you see:
  - A node with cls containing "gallery", "pic", "thumb" + many imgs → IMMEDIATELY call collect_images(category="image_urls")
  - A node with has_price=true or cls containing "price" → IMMEDIATELY call read_price
  - A node with cls containing "detail", "desc" + many imgs → IMMEDIATELY call collect_images(category="desc_images")

Round 3-4: You MUST be collecting data by now. If you haven't used collect_images, read_price, or extract by round 3, you are wasting rounds.

Round 5+: FORCE COLLECT. Stop expanding. Use whatever explored nodes you have:
  - collect_images on any node with >2 imgs
  - read_price to get current price
  - click_sku for each remaining SKU
  - If you have title + price + images, set done=true

MAX DEPTH: Do not expand beyond 6 levels deep (path like "0.0.1.0.1.0" is already too deep). Switch to collecting.

## HINTS FOR IDENTIFICATION

- TITLE: Look for nodes with text containing product name (not shop name, not "淘宝"/"天猫")
- PRICE: Nodes with has_price=true, or text containing ¥/￥
- GALLERY: Upper-half node with 3-8 large images (alicdn.com URLs are good). Usually cls contains "gallery"/"pic"/"thumb".
- SKU AREA: Node with has_sku=true, or many small child nodes with short text labels
- DESC IMAGES: Look for nodes that contain many full-width images BELOW the product info area.
  These are typically in a div with cls containing "detail", "desc", "content", "description".
  The images are already in the DOM (not lazy-loaded via scroll). They are just in a different
  container node — usually in the lower half of the page. Expand into this area and call
  collect_images(path="detail_node_path", category="desc_images").
  Common class patterns: "detailContent", "descContent", "mod-detail", "J_Detail", "description"
- NAVIGATION/FOOTER: Skip nodes with cls containing "nav", "footer", "header", "sidebar", "recommend"

## TARGET OUTPUT (when done=true)

{
  "done": true,
  "data": {
    "title_cn": "Chinese product title",
    "price_cn": "¥129.00",
    "image_urls": ["url1", "url2", ...],
    "sku_prices": [{"name": "颜色/尺寸", "price": "¥129"}, ...],
    "desc_images": ["url1", ...],
    "description_cn": "product description text"
  }
}

## FORBIDDEN ACTIONS (these will FAIL — do NOT use them!)

- wait — REMOVED. Do not use.
- scroll — REMOVED. Not needed.
- click — REMOVED. Use click_sku/click_sku_label/click_sku_combo instead.

The ONLY valid action types are: expand_dom, click_sku, click_sku_label, click_sku_combo, check_sku_state, read_price, collect_images, extract

If you return an invalid action type, the script will return an error and you'll waste a round.

## RULES

- ROUND 0 MANDATORY: You MUST use expand_dom in round 0. The EXPLORED DOM section shows top-level nodes — pick the most promising 2-3 nodes and expand them.
- NEVER issue extract before expanding — you don't have the data yet.
- Be EFFICIENT: expand at most 2-3 nodes per round, prioritize nodes likely to contain data
- SKU COLLECTION: click each SKU one by one. The script auto-waits 1.2s and reads price.
- If skus_available is empty, look for SKU area by expanding nodes with has_sku=true
- Do NOT return done=true with empty image_urls — keep exploring if images not found
- You can issue multiple actions per round (they execute sequentially)
- After 6 rounds, wrap up with whatever data you have — don't waste rounds
"""


def _missing_fields(collected: dict) -> list[str]:
    missing = []
    for key in ("title_cn", "price_cn", "image_urls", "desc_images", "description_cn"):
        val = collected.get(key)
        if not val or (isinstance(val, list) and len(val) == 0):
            missing.append(key)
    if not collected.get("sku_prices"):
        missing.append("sku_prices")
    return missing


def _compact_explored(explored: dict) -> str:
    parts = []
    for path, data in explored.items():
        if isinstance(data, dict) and "children" in data:
            children_summary = []
            for ch in data["children"][:20]:
                s = f"  [{ch.get('path')}] <{ch.get('tag')}>"
                if ch.get('cls'):
                    s += f" .{ch['cls'].split()[0]}"
                if ch.get('id'):
                    s += f" #{ch['id']}"
                if ch.get('text'):
                    s += f" \"{ch['text'][:60]}\""
                if ch.get('imgs'):
                    s += f" [{ch['imgs']}imgs]"
                if ch.get('has_sku'):
                    s += " [SKU!]"
                if ch.get('has_price'):
                    s += " [PRICE!]"
                if ch.get('rect'):
                    s += f" {ch['rect']['w']}x{ch['rect']['h']}@{ch['rect']['t']}"
                if ch.get('child_count'):
                    s += f" ({ch['child_count']}ch)"
                children_summary.append(s)
            parts.append(f"[{path}] → {len(data['children'])} children:\n" + "\n".join(children_summary))
        else:
            parts.append(f"[{path}] → {_json.dumps(data, ensure_ascii=False)[:200]}")
    return "\n\n".join(parts)


VALID_ACTIONS = frozenset({"expand_dom", "click_sku", "click_sku_label", "click_sku_combo", "check_sku_state", "read_price", "collect_images", "extract"})


def _validate_and_fix_actions(result: dict, round_num: int, explored: dict) -> dict:
    """If AI returns only invalid actions, replace with safe expand_dom defaults."""
    actions = result.get("actions", [])
    if not actions or result.get("done"):
        return result

    valid_count = sum(1 for a in actions if a.get("type") in VALID_ACTIONS)

    if valid_count == 0:
        invalid_types = [a.get("type", "?") for a in actions]
        from loguru import logger
        logger.warning(f"[AI Agent] Round {round_num} ALL actions invalid: {invalid_types}. Auto-correcting to expand_dom.")

        explored_paths = sorted([p for p in explored.keys()], key=lambda x: (x.count('.'), x))
        if not explored_paths:
            explored_paths = ["0", "1", "2"]

        result["actions"] = [{"type": "expand_dom", "path": p} for p in explored_paths[:3]]
        result["_auto_corrected"] = True
        result["_original_actions"] = invalid_types
    elif valid_count < len(actions):
        invalid_types = [a.get("type", "?") for a in actions if a.get("type") not in VALID_ACTIONS]
        from loguru import logger
        logger.warning(f"[AI Agent] Round {round_num} filtering invalid: {invalid_types}")
        result["actions"] = [a for a in actions if a.get("type") in VALID_ACTIONS]
        result["_filtered_actions"] = invalid_types

    return result


async def agent_step(
    llm: LLMService,
    platform: str,
    round_num: int,
    dom_state: dict[str, Any],
    history: list[dict[str, Any]],
    max_rounds: int = 8,
    debug: bool = False,
) -> dict[str, Any]:
    if round_num >= max_rounds:
        return {
            "done": True,
            "data": dom_state.get("collected", {}),
            "warning": f"Reached max rounds ({max_rounds})",
        }

    explored = dom_state.get("explored", {})
    collected = dom_state.get("collected", {})
    skus_available = dom_state.get("skus_available", {})
    skus_total = skus_available.get("total", 0) if isinstance(skus_available, dict) else len(skus_available)
    skus_compound = skus_available.get("is_compound", False) if isinstance(skus_available, dict) else (skus_total > 1)
    missing = _missing_fields(collected)

    history_lines = []
    for h in history[-8:]:
        line = f"  R{h.get('round', '?')}: {h.get('action', '?')}"
        if h.get('path'):
            line += f" path={h['path']}"
        if h.get('result_summary'):
            line += f" → {h['result_summary'][:100]}"
        history_lines.append(line)

    explored_text = _compact_explored(explored)

    round_instruction = ""
    explored_children = sum(
        len(v.get("children", [])) for v in explored.values()
        if isinstance(v, dict) and "children" in v
    )
    if round_num == 0:
        round_instruction = (
            "\nRound 0 — You MUST explore the DOM. The EXPLORED DOM below shows body's top-level children.\n"
            "Pick 2-3 promising nodes (look for has_price, has_sku, many imgs in upper half) "
            "and issue expand_dom actions on them.\n"
            "Example: {\"done\": false, \"actions\": [{\"type\": \"expand_dom\", \"path\": \"2\"}, {\"type\": \"expand_dom\", \"path\": \"4\"}]}\n"
        )
    elif round_num >= 5:
        round_instruction = (
            f"\nROUND {round_num}/{max_rounds} — You are running out of rounds! "
            f"You have explored {len(explored)} nodes ({explored_children} children). "
            f"STOP expanding. Start collecting NOW:\n"
            f"- Use collect_images on gallery nodes (category='image_urls')\n"
            f"- Use collect_images on detail nodes (category='desc_images')\n"
            f"- Use read_price to get the price\n"
            f"- Use extract to store title and description\n"
            f"- Use click_sku for remaining SKUs\n"
            f"If you have at least title and price, set done=true.\n"
        )
    elif round_num >= 3 and explored_children > 6:
        gallery_nodes = [p for p in explored.keys() if any(
            "allery" in ch.get("cls", "") or "pic" in ch.get("cls", "")
            for ch in explored.get(p, {}).get("children", [])
        )]
        sku_nodes = [p for p in explored.keys() if any(
            ch.get("has_sku") for ch in explored.get(p, {}).get("children", [])
        )]
        hints = []
        if gallery_nodes:
            hints.append(f"collect_images on gallery node paths: {gallery_nodes[:2]}")
        if sku_nodes:
            hints.append(f"click_sku buttons from skus_available list")
        if hints:
            round_instruction = (
                f"\nYou've explored deeply. Time to COLLECT data, not just expand.\n"
                f"Hints: {', '.join(hints)}\n"
                f"Also use read_price and extract for title/description.\n"
            )

    user_parts = [
        f"Platform: {platform} | Round: {round_num}/{max_rounds}",
        f"URL: {dom_state.get('initial', {}).get('url', '')}",
        f"Page title: {dom_state.get('initial', {}).get('title', '')}" if round_num == 0 else "",
        round_instruction,
        "",
        "═══ EXPLORED DOM ═══",
        explored_text,
        "",
        "═══ COLLECTED DATA ═══",
        _json.dumps(collected, ensure_ascii=False, indent=1) if collected else "  (nothing yet)",
        "",
        f"═══ SKU BUTTONS ({skus_total}) | compound={skus_compound} ═══",
        _json.dumps(skus_available, ensure_ascii=False, indent=1) if skus_total else "  (none found — try expanding nodes with has_sku=true)",
        "",
        "═══ HISTORY ═══",
        "\n".join(history_lines) if history_lines else "  (first round)",
        "",
        f"═══ MISSING: {', '.join(missing) if missing else 'ALL FIELDS COLLECTED'} ═══",
    ]
    user_msg = "\n".join(p for p in user_parts if p is not None)

    if len(user_msg) > 15000:
        over = len(user_msg) - 12000
        explored_text = explored_text[:len(explored_text) - over] + "\n... (truncated)"
        user_msg = "\n".join([
            f"Platform: {platform} | Round: {round_num}/{max_rounds}",
            "",
            "═══ EXPLORED DOM (truncated) ═══",
            explored_text,
            "",
            "═══ COLLECTED ═══",
            _json.dumps(collected, ensure_ascii=False)[:2000],
            "",
            f"═══ MISSING: {', '.join(missing)} ═══",
        ])

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    if debug:
        from loguru import logger
        logger.info(
            f"[AI Agent] Round {round_num} | platform={platform} | "
            f"msg_len={len(user_msg)} | missing={missing} | explored_paths={list(explored.keys())}"
        )
        # Always log a preview of what the AI sees (first 800 chars)
        logger.info(f"[AI Agent] Round {round_num} user_msg preview:\n{user_msg[:800]}")
        if len(user_msg) > 800:
            logger.info(f"[AI Agent] Round {round_num} user_msg tail:\n...{user_msg[-400:]}")

    try:
        result = await llm.chat_json(messages, max_tokens=3000)
    except Exception as exc:
        return {"done": True, "data": collected, "error": f"LLM error: {exc}"}

    if result.get("_parse_error"):
        return {"done": True, "data": collected, "error": f"LLM parse error: {result.get('_parse_error')}"}

    if debug:
        from loguru import logger
        logger.info(
            f"[AI Agent] Round {round_num} response | done={result.get('done')} | "
            f"actions={len(result.get('actions', []))}"
        )

    # Guard: auto-correct invalid actions so the loop never stalls
    result = _validate_and_fix_actions(result, round_num, explored)

    return result
