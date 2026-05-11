"""Streamlit dashboard — review and approve products. Tmall-inspired layout."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from src.db.repository import ProductRepository
from src.models.product import PipelineStatus

IMAGE_DIR = Path("data/images")


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
    repo = get_repo()
    return await repo.list_by_status(status)


# ── Page config ──────────────────────────────────────────────
st.set_page_config(page_title="Product Sourcing — Review", page_icon="🛒", layout="wide")

# ── Custom CSS ───────────────────────────────────────────────
st.markdown("""
<style>
    /* ── Image containers ── */
    .stImage {
        border-radius: 8px;
        overflow: hidden;
    }
    .stImage img {
        border-radius: 8px;
        border: 1px solid #e8e8e8;
        object-fit: cover;
    }
    /* ── Thumbnail strip ── */
    .thumb-wrap {
        position: relative;
        border-radius: 6px;
        overflow: hidden;
        cursor: pointer;
        transition: all 0.15s;
    }
    .thumb-wrap:hover { opacity: 0.85; }
    .thumb-wrap.active { box-shadow: 0 0 0 2px #ff5000; }
    /* ── Price ── */
    .price-current { font-size: 28px; font-weight: 700; color: #ff5000; }
    .price-original { font-size: 14px; color: #999; text-decoration: line-through; margin-left: 8px; }
    .price-usd { font-size: 20px; font-weight: 600; color: #333; margin-top: 4px; }
    /* ── Score card ── */
    .score-card {
        text-align: center; padding: 12px 8px; border-radius: 10px;
        border: 2px solid #e8e8e8; background: #fff;
    }
    /* ── SKU grid ── */
    .sku-card {
        border: 1px solid #e8e8e8; border-radius: 8px; padding: 6px;
        text-align: center; background: #fafafa; transition: border-color 0.15s;
    }
    .sku-card:hover { border-color: #ff5000; }
    .sku-name { font-size: 11px; color: #666; margin-top: 4px; line-height: 1.3; word-break: break-all; }
    .sku-price-tag { font-size: 13px; font-weight: 600; color: #ff5000; }
    /* ── Section titles ── */
    .section-title {
        font-size: 15px; font-weight: 600; margin-bottom: 10px;
        padding-bottom: 4px; border-bottom: 2px solid #ff5000; display: inline-block;
    }
    /* ── Tags ── */
    .tag-pill {
        display: inline-block; background: #fff0f0; color: #ff5000;
        padding: 2px 10px; border-radius: 12px; font-size: 12px;
        margin: 2px; border: 1px solid #ffd4c4;
    }
    /* ── Progress bar labels ── */
    .bar-label { font-size: 12px; color: #555; white-space: nowrap; }
    /* ── Expanders ── */
    div[data-testid="stExpander"] {
        border: 1px solid #e8e8e8 !important; border-radius: 10px !important;
        margin-bottom: 14px !important;
    }
    /* ── Reduce column gaps ── */
    div[data-testid="column"] { padding-left: 6px; padding-right: 6px; }
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
        gap: 0.4rem;
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ──────────────────────────────────────────────────
repo = get_repo()
st.sidebar.title("🛒 Product Sourcing")

status_filter = st.sidebar.selectbox(
    "Status",
    [s.value for s in PipelineStatus],
    index=3,  # "review_pending"
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
    imgs = [img for img in product.images if img and Path(img).exists()]
    skus = product.sku_prices if isinstance(product.sku_prices, list) else []

    score = product.market_score.total if product.market_score else 0
    score_color = "#52c41a" if score >= 70 else "#faad14" if score >= 50 else "#ff4d4f"
    score_emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"

    platform_name = product.platform.value.upper() if product.platform else "?"
    title_display = product.title_en or product.title_cn[:80]

    with st.expander(
        f"{score_emoji} [{platform_name}] {title_display}  —  Score: {score}/100",
        expanded=(len(products) <= 3),
    ):
        # ═══════════════════ TOP ROW: Image Gallery + Info ═══════════
        col_left, col_right = st.columns([1.2, 1], gap="medium")

        with col_left:
            # ── Main image ──
            if imgs:
                thumb_key = f"thumb_idx_{product.id}"
                if thumb_key not in st.session_state:
                    st.session_state[thumb_key] = 0
                thumb_idx = max(0, min(st.session_state[thumb_key], len(imgs) - 1))
                st.image(str(imgs[thumb_idx]), use_container_width=True)

                # Thumbnail strip — compact row
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
                                f'padding:1px; opacity:{opacity}; cursor:pointer; '
                                f'transition:opacity 0.15s;">',
                                unsafe_allow_html=True,
                            )
                            st.image(str(img), use_container_width=True)
                            st.markdown('</div>', unsafe_allow_html=True)
                            if st.button(" ", key=f"thumb_{product.id}_{i}", help=f"View image {i+1}"):
                                st.session_state[thumb_key] = i
                                st.rerun()

                st.caption(f"📷 {len(imgs)} main images | {len(product.desc_images)} detail images")
            else:
                st.info("No images available")

            # ── Description (CN) ──
            if product.description_cn:
                st.markdown(
                    '<span class="section-title">📝 商品描述</span>',
                    unsafe_allow_html=True,
                )
                desc_text = product.description_cn[:500]
                if len(product.description_cn) > 500:
                    desc_text += "..."
                st.markdown(
                    f'<div style="font-size:13px; color:#666; line-height:1.7; '
                    f'padding:10px; background:#fafafa; border-radius:8px; '
                    f'border:1px solid #f0f0f0;">{desc_text}</div>',
                    unsafe_allow_html=True,
                )

        with col_right:
            # ── Price ──
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
                    current_price = f"¥{prices[0]}"
                    original_price = f"¥{prices[1]}"
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
                st.markdown(
                    f'<span class="price-usd">💲 {usd_str} USD</span>',
                    unsafe_allow_html=True,
                )
            with p_col2:
                st.markdown(
                    f'<div class="score-card" style="border-color:{score_color};">'
                    f'<div style="font-size:28px; font-weight:700; color:{score_color};">{score}</div>'
                    f'<div style="font-size:11px; color:#999;">/ 100</div></div>',
                    unsafe_allow_html=True,
                )

            # ── Market score breakdown — native st.progress ──
            if product.market_score:
                st.markdown(
                    '<span class="section-title">📊 AI 分析</span>',
                    unsafe_allow_html=True,
                )
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
                        st.markdown(
                            f'<span class="bar-label">{name}</span>',
                            unsafe_allow_html=True,
                        )
                    with bc2:
                        st.progress(min(pct, 1.0))
                    with bc3:
                        st.caption(f"{val:.0f}")

                if ms.reasoning:
                    with st.expander("💡 Reasoning", expanded=False):
                        st.caption(ms.reasoning)

            # ── Tags ──
            if product.tags:
                tag_html = " ".join(
                    f'<span class="tag-pill">{t}</span>' for t in product.tags[:12]
                )
                st.markdown(
                    f'<div style="margin-top:8px;">{tag_html}</div>',
                    unsafe_allow_html=True,
                )

        # ═══════════════════ SKU Gallery ═══════════════════════
        if skus:
            st.markdown("---")
            st.markdown('<span class="section-title">📦 SKU 规格</span>', unsafe_allow_html=True)

            real_skus = [
                s
                for s in skus
                if s.get("name", "") and "推荐" not in s.get("name", "")
                and "颜色分类" not in s.get("name", "")
                and "⭐" not in s.get("name", "")
                and "宝贝" != s.get("name", "")
            ]
            if not real_skus:
                real_skus = skus

            sku_cols_per_row = 5
            for row_start in range(0, len(real_skus), sku_cols_per_row):
                row_skus = real_skus[row_start : row_start + sku_cols_per_row]
                cols = st.columns(sku_cols_per_row, gap="small")
                for ci, sku in enumerate(row_skus):
                    if ci >= len(cols):
                        break
                    with cols[ci]:
                        sku_name = sku.get("name", "?")[:30]
                        sku_price = sku.get("price", "")
                        sku_imgs = sku.get("images", [])

                        sku_img_path = None
                        if isinstance(sku_imgs, list) and sku_imgs:
                            for si in sku_imgs:
                                if isinstance(si, str):
                                    if si.startswith(("http://", "https://")) or Path(si).exists():
                                        sku_img_path = si
                                        break

                        if sku_img_path:
                            st.image(str(sku_img_path), use_container_width=True)

                        price_display = sku_price if sku_price else "—"
                        st.markdown(
                            f'<div class="sku-card">'
                            f'<div class="sku-name">{sku_name}</div>'
                            f'<div class="sku-price-tag">{price_display}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

        # ═══════════════════ Detail Images ═════════════════════
        if product.desc_images:
            desc_imgs_exist = [
                img for img in product.desc_images if img and Path(img).exists()
            ]
            if desc_imgs_exist:
                st.markdown("---")
                st.markdown(
                    '<span class="section-title">🖼️ 图文详情</span>',
                    unsafe_allow_html=True,
                )
                d_cols = st.columns(min(len(desc_imgs_exist), 3), gap="small")
                for di, dimg in enumerate(desc_imgs_exist[:12]):
                    col_idx = di % 3
                    if col_idx < len(d_cols):
                        with d_cols[col_idx]:
                            st.image(str(dimg), use_container_width=True)
                if len(desc_imgs_exist) > 12:
                    st.caption(f"... and {len(desc_imgs_exist) - 12} more detail images")

        # ═══════════════════ Edit & Actions ═══════════════════
        st.markdown("---")
        st.markdown(
            '<span class="section-title">✏️ 编辑 & 操作</span>',
            unsafe_allow_html=True,
        )

        ec1, ec2 = st.columns([3, 1], gap="medium")
        with ec1:
            title_en = st.text_input(
                "English Title", product.title_en or "", key=f"t_{product.id}"
            )
            price = st.number_input(
                "Price (USD)", value=product.price_usd, step=0.99, key=f"p_{product.id}"
            )
            desc = st.text_area(
                "Optimized Description",
                product.optimized_description or product.description_en or "",
                height=120,
                key=f"d_{product.id}",
            )
            tags = st.text_input(
                "Tags (comma-separated)",
                ", ".join(product.tags),
                key=f"tg_{product.id}",
            )
        with ec2:
            st.markdown("<br>" * 2, unsafe_allow_html=True)
            if st.button("✅ Approve", key=f"app_{product.id}", type="primary"):
                product.title_en = title_en
                product.price_usd = price
                product.optimized_description = desc
                product.tags = [t.strip() for t in tags.split(",") if t.strip()]
                product.status = PipelineStatus.APPROVED
                _run_async(repo.save(product))
                st.rerun()

            if st.button("❌ Reject", key=f"rej_{product.id}"):
                product.status = PipelineStatus.REJECTED
                _run_async(repo.save(product))
                st.rerun()

            if st.button("📦 Archive", key=f"arc_{product.id}"):
                product.status = PipelineStatus.ARCHIVED
                _run_async(repo.save(product))
                st.rerun()
