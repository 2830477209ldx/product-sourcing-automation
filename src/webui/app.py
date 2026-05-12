"""Streamlit dashboard — review, approve, process images + text, export."""

from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from src.config import config
from src.db.repository import ProductRepository
from src.llm.service import LLMService
from src.models.product import PipelineStatus, Product
from src.processing.image_api import ImageAPIClient
from src.webui.excel_exporter import export_products_to_xlsx

IMAGE_DIR = Path("data/images")

METAFIELDS_GEN_PROMPT = """You are a Shopify content writer. Generate 4 metafield sections for this product in English.

Product Title: {title_en}
Description: {description_en}
Tags: {tags}

Generate:
1. custom.description (rich_text): Full formatted product description (HTML: <p>, <ul>, <li>).
2. custom.inspiration: Brand story / lifestyle paragraph.
3. custom.highlights: 3-5 bullet points (use * prefix) of key selling points.
4. custom.notices: Care instructions, safety warnings (use * prefix).

Return ONLY JSON:
{{"description": "...", "inspiration": "...", "highlights": "...", "notices": "..."}}"""


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


@st.cache_resource
def get_repo():
    return ProductRepository()


async def load_products(status: PipelineStatus):
    return await get_repo().list_by_status(status)


def _process_single_image(api_client: ImageAPIClient, image_path: Path, prompt: str) -> tuple[str, bytes]:
    """Process one image and return (filename, data). Raises on failure."""
    data = _run_async(api_client.process(image_path, prompt=prompt))
    return image_path.name, data


def _get_all_image_paths(product: Product) -> list[tuple[str, Path]]:
    """Collect all local image paths. Returns [(label, path)]."""
    results: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for p in product.images:
        pp = Path(p)
        if pp.exists() and pp not in seen:
            seen.add(pp)
            results.append((f"main_{pp.stem}", pp))
    for p in product.desc_images:
        pp = Path(p)
        if pp.exists() and pp not in seen:
            seen.add(pp)
            results.append((f"desc_{pp.stem}", pp))
    skus = product.sku_prices if isinstance(product.sku_prices, list) else []
    for sku in skus:
        for img_url in sku.get("images", []):
            pp = Path(str(img_url))
            if pp.exists() and pp not in seen:
                seen.add(pp)
                results.append((f"sku_{sku.get('name','')[:20]}_{pp.stem}", pp))
    return results


async def _generate_metafields(product: Product) -> dict[str, str]:
    cfg = config.ai
    llm = LLMService(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url", ""),
        model_text=cfg.get("model_text", "deepseek-chat"),
        temperature=0.3,
    )
    prompt = METAFIELDS_GEN_PROMPT.format(
        title_en=product.title_en or product.title_cn,
        description_en=product.description_en or product.description_cn[:1000],
        tags=", ".join(product.tags) if product.tags else "",
    )
    result = await llm.chat_json([{"role": "user", "content": prompt}], max_tokens=2000)
    if result.get("_parse_error"):
        return {}
    return {
        "description": result.get("description", ""),
        "inspiration": result.get("inspiration", ""),
        "highlights": result.get("highlights", ""),
        "notices": result.get("notices", ""),
    }


# ── Page config ──────────────────────────────────────────────
st.set_page_config(page_title="Product Sourcing — Review", page_icon="🛒", layout="wide")

st.markdown("""
<style>
    .stImage { border-radius: 8px; overflow: hidden; }
    .stImage img { border-radius: 8px; border: 1px solid #e8e8e8; object-fit: cover; }
    .price-current { font-size: 28px; font-weight: 700; color: #ff5000; }
    .price-original { font-size: 14px; color: #999; text-decoration: line-through; margin-left: 8px; }
    .price-usd { font-size: 20px; font-weight: 600; color: #333; margin-top: 4px; }
    .score-card {
        text-align: center; padding: 12px 8px; border-radius: 10px;
        border: 2px solid #e8e8e8; background: #fff;
    }
    .sku-card {
        border: 1px solid #e8e8e8; border-radius: 8px; padding: 6px;
        text-align: center; background: #fafafa;
    }
    .sku-name { font-size: 11px; color: #666; margin-top: 4px; line-height: 1.3; word-break: break-all; }
    .sku-price-tag { font-size: 13px; font-weight: 600; color: #ff5000; }
    .section-title {
        font-size: 15px; font-weight: 600; margin-bottom: 10px;
        padding-bottom: 4px; border-bottom: 2px solid #ff5000; display: inline-block;
    }
    .tag-pill {
        display: inline-block; background: #fff0f0; color: #ff5000;
        padding: 2px 10px; border-radius: 12px; font-size: 12px;
        margin: 2px; border: 1px solid #ffd4c4;
    }
    .bar-label { font-size: 12px; color: #555; white-space: nowrap; }
    div[data-testid="stExpander"] {
        border: 1px solid #e8e8e8 !important; border-radius: 10px !important;
        margin-bottom: 14px !important;
    }
    div[data-testid="column"] { padding-left: 6px; padding-right: 6px; }
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] { gap: 0.4rem; }
    .processing-panel {
        background: #fafafa; border: 1px solid #ffd4c4; border-radius: 10px;
        padding: 16px; margin-top: 12px;
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ──────────────────────────────────────────────────
repo = get_repo()
st.sidebar.title("🛒 Product Sourcing")

status_filter = st.sidebar.selectbox(
    "Status",
    [s.value for s in PipelineStatus],
    index=3,
)

products = _run_async(load_products(PipelineStatus(status_filter)))
st.sidebar.metric("Products", len(products))

st.sidebar.markdown("---")
if st.sidebar.button("📤 Export Approved CSV"):
    from src.shopify.csv_exporter import CSVExporter
    approved = _run_async(load_products(PipelineStatus.APPROVED))
    if approved:
        path = CSVExporter().export(approved, "data/exports/approved.csv")
        st.sidebar.success(f"Exported {len(approved)}")
    else:
        st.sidebar.warning("No approved products")

st.title("📋 Product Review Dashboard")

if not products:
    st.info(f"No products with status: {status_filter}")
    st.stop()

# ── Product cards ────────────────────────────────────────────
for idx, product in enumerate(products):
    imgs = [img for img in product.images if img and (img.startswith(("http://", "https://")) or Path(img).exists())]
    skus = product.sku_prices if isinstance(product.sku_prices, list) else []
    pid = product.id or f"p{idx}"
    handle = product.make_handle()

    score = product.market_score.total if product.market_score else 0
    score_color = "#52c41a" if score >= 70 else "#faad14" if score >= 50 else "#ff4d4f"
    score_emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"

    platform_name = product.platform.value.upper() if product.platform else "?"
    title_display = product.title_en or product.title_cn[:80]

    is_processing = st.session_state.get(f"processing_{pid}", False)

    with st.expander(
        f"{score_emoji} [{platform_name}] {title_display}  —  Score: {score}/100",
        expanded=(len(products) <= 3 or is_processing),
    ):
        # ═════════ TOP: Image Gallery + Info ═════════
        col_left, col_right = st.columns([1.2, 1], gap="medium")

        with col_left:
            if imgs:
                thumb_key = f"thumb_{pid}"
                if thumb_key not in st.session_state:
                    st.session_state[thumb_key] = 0
                thumb_idx = max(0, min(st.session_state[thumb_key], len(imgs) - 1))
                st.image(str(imgs[thumb_idx]), width="stretch")

                if len(imgs) > 1:
                    n_cols = min(len(imgs), 6)
                    cols = st.columns(n_cols, gap="small")
                    for i, img in enumerate(imgs[:n_cols]):
                        with cols[i]:
                            active = i == thumb_idx
                            border = "2px solid #ff5000" if active else "2px solid #e8e8e8"
                            opacity = "1" if active else "0.55"
                            st.markdown(
                                f'<div style="border:{border}; border-radius:6px; '
                                f'padding:1px; opacity:{opacity};">', unsafe_allow_html=True)
                            st.image(str(img), width="stretch")
                            st.markdown('</div>', unsafe_allow_html=True)
                            if st.button(" ", key=f"thumb_{pid}_{i}", help=f"View {i+1}"):
                                st.session_state[thumb_key] = i
                                st.rerun()
                st.caption(f"📷 {len(imgs)} main | {len(product.desc_images)} detail images")
            else:
                st.info("No images")

            if product.description_cn:
                st.markdown('<span class="section-title">📝 商品描述</span>', unsafe_allow_html=True)
                desc_text = product.description_cn[:500]
                if len(product.description_cn) > 500:
                    desc_text += "..."
                st.markdown(
                    f'<div style="font-size:13px;color:#666;line-height:1.7;'
                    f'padding:10px;background:#fafafa;border-radius:8px;'
                    f'border:1px solid #f0f0f0;">{desc_text}</div>',
                    unsafe_allow_html=True,
                )

        with col_right:
            # Price
            st.markdown('<span class="section-title">💰 价格</span>', unsafe_allow_html=True)
            price_str = product.price_cn or ""
            current_price = price_str
            original_price = ""
            coupon_match = re.search(r"券后[￥¥](\d+\.?\d*)", price_str)
            orig_match = re.search(r"优惠前[￥¥](\d+\.?\d*)", price_str)
            if coupon_match:
                current_price = f"¥{coupon_match.group(1)}"
                if orig_match:
                    original_price = f"¥{orig_match.group(1)}"
            else:
                prices = re.findall(r"[￥¥](\d+\.?\d*)", price_str)
                if len(prices) >= 2:
                    current_price, original_price = f"¥{prices[0]}", f"¥{prices[1]}"
                elif prices:
                    current_price = f"¥{prices[0]}"

            p_col1, p_col2 = st.columns([3, 1], gap="small")
            with p_col1:
                st.markdown(
                    f'<span class="price-current">{current_price}</span>'
                    + (f'<span class="price-original">{original_price}</span>' if original_price else ""),
                    unsafe_allow_html=True,
                )
                usd_str = f"${product.price_usd:.2f}" if product.price_usd else "—"
                st.markdown(f'<span class="price-usd">💲 {usd_str} USD</span>', unsafe_allow_html=True)
            with p_col2:
                st.markdown(
                    f'<div class="score-card" style="border-color:{score_color};">'
                    f'<div style="font-size:28px;font-weight:700;color:{score_color};">{score}</div>'
                    f'<div style="font-size:11px;color:#999;">/ 100</div></div>',
                    unsafe_allow_html=True,
                )

            # Score breakdown
            if product.market_score:
                st.markdown('<span class="section-title">📊 AI 分析</span>', unsafe_allow_html=True)
                ms = product.market_score
                dims = [
                    ("Visual Appeal", ms.visual_appeal, 25),
                    ("Category Demand", ms.category_demand, 25),
                    ("Uniqueness", ms.uniqueness, 20),
                    ("Price Arbitrage", ms.price_arbitrage, 15),
                    ("Trend Alignment", ms.trend_alignment, 15),
                ]
                for name, val, max_val in dims:
                    pct = val / max_val if max_val else 0
                    bc1, bc2, bc3 = st.columns([2.4, 5, 0.8], gap="small")
                    with bc1:
                        st.markdown(f'<span class="bar-label">{name}</span>', unsafe_allow_html=True)
                    with bc2:
                        st.progress(min(pct, 1.0))
                    with bc3:
                        st.caption(f"{val:.0f}")

            if product.tags:
                tag_html = " ".join(f'<span class="tag-pill">{t}</span>' for t in product.tags[:12])
                st.markdown(f'<div style="margin-top:8px;">{tag_html}</div>', unsafe_allow_html=True)

        # ── SKU Gallery ──
        if skus:
            st.markdown("---")
            st.markdown('<span class="section-title">📦 SKU 规格</span>', unsafe_allow_html=True)
            real_skus = [s for s in skus if s.get("name","") and "推荐" not in s.get("name","")
                         and "颜色分类" not in s.get("name","") and "宝贝" != s.get("name","")]
            if not real_skus:
                real_skus = skus
            for row_start in range(0, len(real_skus), 5):
                row_skus = real_skus[row_start:row_start+5]
                cols = st.columns(5, gap="small")
                for ci, sku in enumerate(row_skus):
                    if ci >= len(cols):
                        break
                    with cols[ci]:
                        sku_imgs = sku.get("images", [])
                        sku_img_path = None
                        if isinstance(sku_imgs, list) and sku_imgs:
                            for si in sku_imgs:
                                if isinstance(si, str) and (si.startswith(("http","https")) or Path(si).exists()):
                                    sku_img_path = si
                                    break
                        if sku_img_path:
                            st.image(str(sku_img_path), width="stretch")
                        price_display = sku.get("price", "") or "—"
                        st.markdown(
                            f'<div class="sku-card"><div class="sku-name">{sku.get("name","?")[:30]}</div>'
                            f'<div class="sku-price-tag">{price_display}</div></div>',
                            unsafe_allow_html=True,
                        )

        # ── Detail Images ──
        if product.desc_images:
            desc_imgs_exist = [img for img in product.desc_images if img and (img.startswith(("http://", "https://")) or Path(img).exists())]
            if desc_imgs_exist:
                st.markdown("---")
                st.markdown('<span class="section-title">🖼️ 图文详情</span>', unsafe_allow_html=True)
                d_cols = st.columns(min(len(desc_imgs_exist), 3), gap="small")
                for di, dimg in enumerate(desc_imgs_exist[:12]):
                    col_idx = di % 3
                    if col_idx < len(d_cols):
                        with d_cols[col_idx]:
                            st.image(str(dimg), width="stretch")

        # ═══════════════════ ACTIONS ═══════════════════════════
        st.markdown("---")
        st.markdown('<span class="section-title">✏️ 编辑 & 操作</span>', unsafe_allow_html=True)

        ec1, ec2 = st.columns([3, 1], gap="medium")
        with ec1:
            title_en = st.text_input("English Title", product.title_en or "", key=f"t_{pid}")
            price = st.number_input("Price (USD)", value=product.price_usd, step=0.99, key=f"p_{pid}")
            desc = st.text_area(
                "Optimized Description", product.optimized_description or product.description_en or "",
                height=100, key=f"d_{pid}",
            )
            tags = st.text_input("Tags (comma-separated)", ", ".join(product.tags), key=f"tg_{pid}")
        with ec2:
            st.markdown("<br>" * 2, unsafe_allow_html=True)

            if not is_processing:
                if st.button("✅ Approve & Process", key=f"app_{pid}", type="primary"):
                    product.title_en = title_en
                    product.price_usd = price
                    product.optimized_description = desc
                    product.tags = [t.strip() for t in tags.split(",") if t.strip()]
                    product.status = PipelineStatus.APPROVED
                    _run_async(repo.save(product))
                    st.session_state[f"processing_{pid}"] = True
                    st.rerun()

                col_r, col_a = st.columns(2, gap="small")
                with col_r:
                    if st.button("❌ Reject", key=f"rej_{pid}"):
                        product.status = PipelineStatus.REJECTED
                        _run_async(repo.save(product))
                        st.rerun()
                with col_a:
                    if st.button("📦 Archive", key=f"arc_{pid}"):
                        product.status = PipelineStatus.ARCHIVED
                        _run_async(repo.save(product))
                        st.rerun()
            else:
                # After save edits
                if st.button("💾 Save Edits", key=f"save_{pid}"):
                    product.title_en = title_en
                    product.price_usd = price
                    product.optimized_description = desc
                    product.tags = [t.strip() for t in tags.split(",") if t.strip()]
                    _run_async(repo.save(product))
                    st.success("Saved")
                    st.rerun()

        # ═══════════ PROCESSING PANEL (after Approve) ═══════════
        if not is_processing:
            continue

        # Initialize processing session state
        PP = f"proc_{pid}"
        image_paths = _get_all_image_paths(product)
        if f"{PP}_names" not in st.session_state:
            st.session_state[f"{PP}_names"] = {i: f"{handle}_{i+1:02d}" for i in range(len(image_paths))}
        if f"{PP}_selected" not in st.session_state:
            st.session_state[f"{PP}_selected"] = set(range(len(image_paths)))
        if f"{PP}_processed" not in st.session_state:
            st.session_state[f"{PP}_processed"] = {}
        if f"{PP}_webp" not in st.session_state:
            st.session_state[f"{PP}_webp"] = {}
        if f"{PP}_meta" not in st.session_state:
            st.session_state[f"{PP}_meta"] = {}
        if f"{PP}_img_prompts" not in st.session_state:
            default_prompt = f"Background removal, US-market color grading, 2048x2048 resize, English text overlay if applicable."
            st.session_state[f"{PP}_img_prompts"] = {i: default_prompt for i in range(len(image_paths))}
        if f"{PP}_step" not in st.session_state:
            st.session_state[f"{PP}_step"] = "image"

        current_step = st.session_state[f"{PP}_step"]

        # ═══════════════════════════════════════════════════════
        #  STEP 1: IMAGE PROCESSING
        # ═══════════════════════════════════════════════════════
        if current_step == "image":
            st.markdown("---")
            st.markdown("## 🖼️ Step 1: Image Processing")

            if not image_paths:
                st.warning("No local images found for this product. Proceed to text processing.")
                if st.button("⏭ Skip to Text Processing", key=f"{PP}_skip_img", type="primary"):
                    st.session_state[f"{PP}_step"] = "text"
                    st.rerun()
            else:
                # ── Image cards (per-image: input↔output + prompt + retry) ──
                st.markdown('<span class="section-title">📷 Image Processing Cards</span>', unsafe_allow_html=True)
                st.caption("Each image has its own prompt. Process individually or batch all.")

                cur_sel = st.session_state[f"{PP}_selected"]
                all_selected = len(cur_sel) == len(image_paths)
                select_all = st.checkbox("Select All", value=all_selected, key=f"{PP}_all")
                if select_all and not all_selected:
                    cur_sel = set(range(len(image_paths)))
                elif not select_all and all_selected:
                    cur_sel = set()
                st.session_state[f"{PP}_selected"] = cur_sel

                for i, (label, path) in enumerate(image_paths):
                    if not path.exists():
                        continue
                    proc_data = st.session_state[f"{PP}_processed"].get(i)
                    webp_data = st.session_state[f"{PP}_webp"].get(i)
                    names = st.session_state[f"{PP}_names"]
                    prompts = st.session_state[f"{PP}_img_prompts"]

                    st.markdown("---")
                    with st.container():
                        # ── Row 1: checkbox + name + batch-select ──
                        r1c1, r1c2 = st.columns([0.1, 5], gap="small")
                        with r1c1:
                            st.markdown("<br>", unsafe_allow_html=True)
                            sel = st.checkbox("✓", value=i in cur_sel, key=f"{PP}_sel_{i}", label_visibility="visible")
                            if sel:
                                cur_sel.add(i)
                            else:
                                cur_sel.discard(i)
                        with r1c2:
                            current_name = names.get(i, "")
                            new_name = st.text_input(
                                "Image Name", value=current_name,
                                key=f"{PP}_nm_{i}", placeholder="e.g. product_main_01",
                                label_visibility="visible",
                            )
                            names[i] = new_name.strip() or current_name
                            st.session_state[f"{PP}_names"] = names

                        # ── Row 2: Input ↔ Output side by side ──
                        i_col, arrow_col, o_col = st.columns([2, 0.15, 2], gap="small")
                        with i_col:
                            st.caption("📥 Original")
                            st.image(str(path), width="stretch")
                            st.caption(f"{path.suffix} | {path.stat().st_size // 1024}KB")
                        with arrow_col:
                            st.markdown(
                                "<div style='text-align:center;padding-top:60px;font-size:28px;color:#ff5000;'>→</div>",
                                unsafe_allow_html=True,
                            )
                        with o_col:
                            st.caption("📤 Processed")
                            if proc_data:
                                st.image(proc_data, width="stretch")
                                if webp_data:
                                    st.success("✅ WebP ready")
                                else:
                                    st.info("⚙️ Processed")
                            else:
                                st.markdown(
                                    "<div style='border:2px dashed #ddd;border-radius:8px;height:120px;"
                                    "display:flex;align-items:center;justify-content:center;color:#bbb;'>"
                                    "Waiting...</div>",
                                    unsafe_allow_html=True,
                                )
                                st.caption("Not processed")

                        # ── Row 3: Prompt + action buttons ──
                        st.caption("📝 Prompt")
                        pc1, pc2 = st.columns([4, 1], gap="small")
                        with pc1:
                            current_prompt = prompts.get(i, "")
                            new_prompt = st.text_area(
                                "Prompt",
                                value=current_prompt,
                                height=68,
                                key=f"{PP}_prompt_{i}",
                                placeholder="Describe how to process this image...",
                                label_visibility="collapsed",
                            )
                            prompts[i] = new_prompt
                            st.session_state[f"{PP}_img_prompts"] = prompts
                        with pc2:
                            st.markdown("<br>" * 1, unsafe_allow_html=True)
                            api_client_i = ImageAPIClient()
                            if st.button("🔄 Retry", key=f"{PP}_retry_{i}", type="secondary", disabled=not api_client_i.configured):
                                with st.spinner(f"Reprocessing {path.name}..."):
                                    try:
                                        _, retry_data = _process_single_image(api_client_i, path, new_prompt)
                                        st.session_state[f"{PP}_processed"][i] = retry_data
                                        if i in st.session_state[f"{PP}_webp"]:
                                            del st.session_state[f"{PP}_webp"][i]
                                    except Exception as exc:
                                        st.error(f"Failed: {exc}")
                                st.rerun()

                            if proc_data and not webp_data:
                                if st.button("💾 WebP", key=f"{PP}_webp_single_{i}"):
                                    from PIL import Image
                                    try:
                                        img = Image.open(io.BytesIO(proc_data))
                                        img = img.convert("RGBA") if img.mode in ("RGBA","P","LA") else img.convert("RGB")
                                        buf = io.BytesIO()
                                        img.save(buf, "WEBP", quality=85, method=6)
                                        st.session_state[f"{PP}_webp"][i] = buf.getvalue()
                                    except Exception as exc:
                                        st.error(f"WebP failed: {exc}")
                                    st.rerun()

                            if webp_data:
                                name = names.get(i, f"img_{i}")
                                st.download_button(
                                    f"⬇ {name}.webp", data=webp_data, file_name=f"{name}.webp",
                                    mime="image/webp", key=f"{PP}_dl_{i}",
                                )

                # ── Batch Operations ──
                st.markdown("---")
                st.markdown('<span class="section-title">🚀 Batch Operations</span>', unsafe_allow_html=True)

                selected_list = sorted(cur_sel)
                api_client = ImageAPIClient()
                api_ready = api_client.configured

                with st.expander("🔧 API Config Debug", expanded=not api_ready):
                    st.caption(f"base_url: `{api_client.base_url or '(empty)'}`")
                    st.caption(f"configured: {api_client.configured}")
                    st.caption(f"is_gemini: {api_client._is_gemini}")

                if not selected_list:
                    st.warning("No images selected")
                else:
                    st.caption(f"{len(selected_list)} image(s) selected")
                    bc1, bc2, bc3 = st.columns([1.5, 1, 1], gap="small")
                    with bc1:
                        if st.button("🚀 Process All Selected", key=f"{PP}_api", type="primary", disabled=not (api_ready and selected_list)):
                            with st.spinner(f"Processing {len(selected_list)}..."):
                                prompts = st.session_state[f"{PP}_img_prompts"]
                                for idx in selected_list:
                                    try:
                                        img_prompt = prompts.get(idx, "")
                                        _, data = _process_single_image(api_client, image_paths[idx][1], img_prompt)
                                        st.session_state[f"{PP}_processed"][idx] = data
                                    except Exception as exc:
                                        st.error(f"Failed {image_paths[idx][1].name}: {exc}")
                            st.rerun()
                    with bc2:
                        processed = st.session_state[f"{PP}_processed"]
                        proc_sel = [idx for idx in selected_list if idx in processed]
                        if st.button("🔄 Convert All to WebP", key=f"{PP}_webp_btn", type="secondary", disabled=not proc_sel):
                            from PIL import Image
                            with st.spinner(f"Converting {len(proc_sel)}..."):
                                for idx in proc_sel:
                                    try:
                                        img = Image.open(io.BytesIO(processed[idx]))
                                        img = img.convert("RGBA") if img.mode in ("RGBA","P","LA") else img.convert("RGB")
                                        buf = io.BytesIO()
                                        img.save(buf, "WEBP", quality=85, method=6)
                                        st.session_state[f"{PP}_webp"][idx] = buf.getvalue()
                                    except Exception as exc:
                                        st.error(f"WebP failed {idx}: {exc}")
                            st.rerun()
                    with bc3:
                        if not api_ready:
                            st.caption("⚠️ Set IMAGE_API_BASE_URL in .env")

                # ── Navigate to Text Processing ──
                st.markdown("---")
                cn1, cn2 = st.columns([1, 1], gap="medium")
                with cn1:
                    if st.button("📄 Proceed to Text Processing →", key=f"{PP}_to_text", type="primary"):
                        st.session_state[f"{PP}_step"] = "text"
                        st.rerun()
                with cn2:
                    webp_ready = bool(st.session_state.get(f"{PP}_webp"))
                    if not webp_ready:
                        st.caption("💡 Tip: Process images to WebP first for a complete Excel export.")

        # ═══════════════════════════════════════════════════════
        #  STEP 2: TEXT PROCESSING
        # ═══════════════════════════════════════════════════════
        else:
            st.markdown("---")
            st.markdown("## 📄 Step 2: Text Processing")

            # Back to image processing
            if st.button("← Back to Image Processing", key=f"{PP}_back_img"):
                st.session_state[f"{PP}_step"] = "image"
                st.rerun()

            # ── 2A: Product Info Summary ──
            st.markdown('<span class="section-title">📋 Product Summary</span>', unsafe_allow_html=True)
            with st.container(border=True):
                st.markdown(f"**{product.title_en or product.title_cn}**")
                st.caption(f"Handle: `{handle}`")
                st.caption(f"Price: {product.price_cn or 'N/A'}  |  USD: ${product.price_usd:.2f}" if product.price_usd else "")
                st.caption(f"Tags: {', '.join(product.tags[:8]) if product.tags else '—'}")

            # ── 2B: AI Metafields ──
            st.markdown("---")
            st.markdown('<span class="section-title">🤖 AI Metafields</span>', unsafe_allow_html=True)
            st.caption("Generate Shopify metafields (description, inspiration, highlights, notices) via AI")

            meta = st.session_state[f"{PP}_meta"]

            if not meta:
                if st.button("🧠 Generate with AI", key=f"{PP}_meta_gen", type="primary"):
                    with st.spinner("AI generating metafields..."):
                        meta = _run_async(_generate_metafields(product))
                        st.session_state[f"{PP}_meta"] = meta
                    st.rerun()
            else:
                st.success("Metafields generated. Edit below if needed.")

            m_desc = st.text_area("custom.description", value=meta.get("description",""), height=120, key=f"{PP}_desc")
            m_insp = st.text_area("custom.inspiration", value=meta.get("inspiration",""), height=80, key=f"{PP}_insp")
            m_high = st.text_area("custom.highlights", value=meta.get("highlights",""), height=80, key=f"{PP}_high")
            m_noti = st.text_area("custom.notices", value=meta.get("notices",""), height=80, key=f"{PP}_noti")

            if st.button("💾 Save Metafields", key=f"{PP}_meta_save"):
                st.session_state[f"{PP}_meta"] = {
                    "description": m_desc, "inspiration": m_insp,
                    "highlights": m_high, "notices": m_noti,
                }
                st.success("Metafields saved")
                st.rerun()

            # ── 2C: SKU Variants ──
            if skus:
                st.markdown("---")
                st.markdown('<span class="section-title">📦 SKU Variants</span>', unsafe_allow_html=True)
                real = [s for s in skus if s.get("name","") and "推荐" not in s.get("name","")
                        and "颜色分类" not in s.get("name","")]
                if real:
                    st.dataframe(
                        [{"#": i+1, "Name": s.get("name","")[:40],
                          "Price": re.sub(r"[^\d.]","",str(s.get("price",""))),
                          "Imgs": len(s.get("images",[]))} for i, s in enumerate(real)],
                        width="stretch", hide_index=True,
                    )

            # ── 2D: Excel Export ──
            st.markdown("---")
            st.markdown('<span class="section-title">📥 Export 导入表.xlsx</span>', unsafe_allow_html=True)
            st.caption("Generate the Shopify import Excel file (requires WebP images)")

            vendor = st.text_input("Vendor", key=f"{PP}_vendor")

            webp_ready = bool(st.session_state.get(f"{PP}_webp"))
            if st.button("📊 Generate Import Excel", key=f"{PP}_exp", type="primary", disabled=not webp_ready):
                names = st.session_state.get(f"{PP}_names", {})
                webp = st.session_state.get(f"{PP}_webp", {})
                img_list = [f"{names.get(idx, f'img_{idx}')}.webp" for idx in sorted(webp.keys())]
                current_meta = {
                    "description": m_desc, "inspiration": m_insp,
                    "highlights": m_high, "notices": m_noti,
                }
                output = export_products_to_xlsx(
                    products=[product],
                    image_paths={pid: img_list} if img_list else None,
                    metafields={pid: current_meta} if current_meta else None,
                    output_path="data/exports/import_table.xlsx",
                )
                st.success(f"Exported to {output}")
                with open(output, "rb") as f:
                    st.download_button(
                        "⬇ Download Excel", data=f, file_name="import_table.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"{PP}_dl_xlsx",
                    )
            if not webp_ready:
                st.caption("⚠️ Process images to WebP first (Step 1) — required for complete Excel generation")
