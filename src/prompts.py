"""Shared LLM prompt templates — single source of truth for pipeline and API."""

STRUCTURED_EXTRACT_PROMPT = """You are a product data extraction expert. Given raw page text from a Chinese e-commerce product page, extract structured data in English for a US Shopify listing.

Extract these fields:
1. title_en: Clean English product title (max 70 chars, SEO-optimized)
2. description_en: HTML formatted product description with bullet points (<ul><li>...)
3. material: Product material
4. dimensions: Dimensions converted to inches
5. weight_oz: Weight in ounces
6. color_options: Available colors in English
7. size_options: Available sizes
8. features: List of 5-7 key product features/benefits in English
9. price_cn: Original price as shown on the page
10. suggested_price_usd: Suggested US retail price (CN price / 7.2, add reasonable margin)
11. category: Best Shopify product category
12. tags: 5-8 SEO tags for Shopify

Actual scraped SKU variants (for context, do NOT output sku_prices):
{sku_prices}

Page content:
{page_text}

Return ONLY a JSON object with all these fields."""

MARKET_ANALYZE_PROMPT = """You are a cross-border e-commerce analyst. Evaluate this product's US market potential.

Product:
{product_info}

Score each dimension 0-100 and compute weighted total:
1. Visual Appeal (25%): Western consumer aesthetic appeal
2. Category Demand (25%): US market demand for this category
3. Uniqueness (20%): Differentiation vs US competitors
4. Price Arbitrage (15%): Margin potential
5. Trend Alignment (15%): Current US market trends

Return ONLY a JSON object:
{{"total": N, "visual_appeal": N, "category_demand": N, "uniqueness": N, "price_arbitrage": N, "trend_alignment": N, "reasoning": "...", "target_audience": "...", "suggested_price_usd": "...", "competitive_notes": "..."}}"""

DESCRIPTION_BUILD_PROMPT = """You are an expert Shopify copywriter. Generate an SEO-optimized HTML product description.

Product: {title_en}
Details: {description_en}
Target keywords: {tags}

Generate a complete Shopify product description:
1. Start with a compelling headline (h2)
2. 3-5 bullet points of key features/benefits (ul > li)
3. Persuasive closing paragraph with call-to-action (p)
4. SEO meta description (under 160 chars)

Return ONLY a JSON object:
{{"description_html": "<h2>...</h2><ul>...</ul><p>...</p>", "seo_title": "...", "seo_description": "...", "suggested_tags": ["tag1", "tag2"]}}"""
