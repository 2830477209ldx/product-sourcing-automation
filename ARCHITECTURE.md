# Product Sourcing Automation — Architecture Document

> AI-driven cross-border product sourcing pipeline: scrape CN platforms, analyze US market potential, publish to Shopify.
> Version 0.2.0 | Python 3.11+ | MIT License

---

## 1. Project Structure

```
product-sourcing-automation/
├── run.py                          # CLI entry point (Click)
├── pyproject.toml                  # Project metadata & dependencies
├── ARCHITECTURE.md                 # This document
│
├── config/
│   └── settings.yaml               # Main configuration (AI, market, paths)
│
├── src/
│   ├── config.py                   # Config singleton (YAML + .env loader)
│   ├── utils.py                    # Utility helpers (platform detect, handle, sanitize)
│   ├── agents/
│   │   ├── product_agent.py        # Browser automation + extraction agent
│   │   └── slimdom_extractor.py    # AI-driven DOM layout extractor (no hardcoded CSS selectors)
│   ├── db/
│   │   └── repository.py           # Async SQLite product repository
│   ├── llm/
│   │   └── service.py              # Unified LLM service (DeepSeek/Gemini)
│   ├── models/
│   │   └── product.py              # Pydantic data models & enums
│   ├── pipeline/
│   │   ├── __init__.py             # StageResult[T] generic type
│   │   ├── pipeline.py             # Pipeline orchestrator
│   │   └── stages.py               # 5 pipeline stages
│   ├── processing/
│   │   ├── image_styler.py         # Local image adaptation (OpenCV)
│   │   └── image_api.py            # External image API client (Gemini)
│   ├── shopify/
│   │   └── csv_exporter.py         # Shopify CSV exporter
│   ├── api/
│   │   └── server.py               # FastAPI server (Chrome extension backend)
│   └── webui/
│       ├── app.py                  # Streamlit review dashboard
│       └── excel_exporter.py       # Shopify 75-column Excel exporter
│
├── chrome-extension/               # Chrome extension (one-click import)
│   ├── manifest.json
│   ├── background.js
│   ├── content-script.js
│   └── popup/
│       └── popup.js
│
└── data/
    ├── products.db                  # SQLite database (runtime)
    ├── browser_profile/            # Playwright browser profile (persistent cookies)
    ├── cookies.json                # Saved browser cookies
    ├── images/                     # Downloaded product images (per-product folder)
    ├── processed/                  # US-adapted images
    └── exports/                    # CSV/XLSX export output
```

---

## 2. Module Reference

### 2.1 `run.py` — CLI Entry Point

**Tech:** Click

| Function/Command | Description |
|---|---|
| `_make_pipeline(headless=True)` | Factory: creates LLMService + Pipeline instance |
| `cli()` | Click group root |
| `add -u URL` | Full pipeline from single URL |
| `add --visible` | Show browser window (for QR login) |
| `add-i -u URL` | Interactive import (browser stays open, cookies saved) |
| `batch-add-i -u URL -f FILE` | Batch import in ONE browser session (cookie reuse, no re-login) |
| `batch-add -f FILE` | Batch import from URL file |
| `review` | Launch Streamlit dashboard |
| `export` | Export approved products to CSV |
| `status` | Show pipeline status summary |
| `_download_images(folder, urls)` | Async download images with SSRF protection |
| `_download_sku_images(folder, sku_prices)` | Download SKU-specific variant images |

**Call chain (add-i path):**
```
run.py:add_interactive()
  → ProductAgent.extract() (browser with persistent profile)
  → SlimDOMExtractor.extract() → AI → JS SKU clicks
  → _download_images() / _download_sku_images() (local files)
  → ProductRepository.save()
  → ExtractStage + AnalyzeStage (LLM enrichment)
  → status=REVIEW_PENDING
  → Browser stays open (no close, session preserved)
```

---

### 2.2 `src/utils.py` — Utilities

| Function | Description |
|---|---|
| `detect_platform(url)` | Regex match platform (taobao/alibaba/xiaohongshu) |
| `make_handle_from_title(title)` | Generate URL/filesystem-safe Shopify handle slug |
| `sanitize_filename(name)` | Sanitize string for safe filename use |
| `clean_price(v)` | Normalize price strings (¥/$ → float) |

---

### 2.3 `src/agents/slimdom_extractor.py` — AI-Driven Layout Extractor

**Key innovation:** NO hardcoded CSS selectors. AI reads page structure, identifies where things are.

**Architecture phases:**
1.  `JS_COLLECT_PAGE_DATA`: Pure DOM walk → collects:
    - all visible text (no script/style/svg)
    - images with (position, size, parent context text)
    - SKU-like interactive elements (data-value, class*=sku, etc.)
    - Chinese section markers (图文详情, 产品详情, etc.) with Y-position
2.  LLM receives raw JSON, outputs structured product data:
    - `title_cn`, `price_cn`
    - `image_urls` (top half, large product gallery)
    - `desc_images` (detail images below markers)
    - `sku_prices` (variant labels, price initially empty)
3.  **SKU click phase** (unique feature):
    - JS clicks each SKU button identified by AI
    - 1.2s wait, then collect post-click price + main image
    - Reset to first SKU after completion to restore default state

**Layout-based fallback JS strategies (no CSS selectors):**
- `JS_LAYOUT_GALLERY`: Find largest top-half image-rich container (area * img count score)
- `JS_LAYOUT_SKU_CLUSTER`: Cluster nearby clickable small elements, find best SKU group
- `JS_EXTRACT_BETWEEN_MARKERS`: Extract images between two text markers (图文详情 → 本店推荐)

---

### 2.4 `src/config.py` — Configuration

**Tech:** PyYAML, python-dotenv

| Function/Method | Description |
|---|---|
| `_env_replace(value)` | Replace `${ENV_VAR}` in strings |
| `_resolve_refs(data)` | Recursively resolve env vars in dict/list |
| `_deep_merge(base, overlay)` | Deep merge two dicts |
| `Config.__init__()` | Load settings.yaml + optional settings.local.yaml + .env |
| `Config.instance()` | Singleton accessor |
| `Config.ai` | Property → `self._data["ai"]` |
| `Config.market_judge` | Property → market analysis config |
| `Config.platforms` | Property → platform configs |
| `Config.paths` | Property → data paths |
| `Config.shopify` | Property → Shopify config |
| `Config.currency` | Property → currency conversion rate |

**Config file (`settings.yaml`):**
```yaml
ai:            # LLM provider settings + API key
market_judge:  # Scoring threshold + dimension weights
currency:      # CNY→USD rate
paths:         # Data/image/export directories
logging:       # Loguru log level/format/rotation
```

---

### 2.5 `src/models/product.py` — Data Models

**Tech:** Pydantic v2

| Model/Enum | Fields |
|---|---|
| `PipelineStatus` (Enum) | `scraped → analyzed → processed → review_pending → approved / rejected → pushed_to_shopify → csv_exported → archived` |
| `Platform` (Enum) | `xiaohongshu`, `taobao`, `alibaba` |
| `MarketScore` | `total`, `visual_appeal`, `category_demand`, `uniqueness`, `price_arbitrage`, `trend_alignment`, `reasoning` |
| `Product` | `id`, `platform`, `source_url`, `title_cn`, `price_cn`, `description_cn`, `images[]`, `desc_images[]`, `sku_prices[]`, `market_score`, `title_en`, `description_en`, `optimized_description`, `price_usd`, `tags[]`, `status`, `shopify_product_id`, `created_at`, `updated_at` |

**Key additions:** `desc_images` (separate product detail images, not gallery), sku entries can have `.images[]` for variant-specific photos.

**Key methods:**
| Method | Description |
|---|---|
| `Product.make_handle()` | Generate Shopify handle from title |
| `Product.dict_for_db()` | Serialize for database storage |

---

### 2.6 `src/db/repository.py` — Database Layer

**Tech:** aiosqlite (async SQLite, WAL mode)

| Method | SQL | Description |
|---|---|---|
| `__init__(db_path)` | — | Init with path, create parent dirs |
| `_get_conn()` | — | Lazy init: open connection, set WAL, run migration |
| `_migrate()` | `CREATE TABLE IF NOT EXISTS products (...)` + `CREATE INDEX IF NOT EXISTS idx_status` | Schema creation |
| `save(product)` | `INSERT OR REPLACE INTO products (...)` | Upsert product |
| `get(product_id)` | `SELECT * WHERE id = ?` | Get single product by ID |
| `list_by_status(status)` | `SELECT * WHERE status = ? ORDER BY updated_at DESC` | Get products by status |
| `list_all()` | `SELECT * ORDER BY updated_at DESC` | Get all products |
| `count_by_status(status)` | `SELECT COUNT(*) WHERE status = ?` | Count by status |
| `close()` | — | Close connection |
| `_row_to_product(row)` | — | Deserialize SQLite row → Product |

---

### 2.7 `src/llm/service.py` — LLM Service

**Tech:** openai (AsyncOpenAI → DeepSeek API)

| Method | Description |
|---|---|
| `__init__(api_key, base_url, model_text, model_vision, temperature)` | Create AsyncOpenAI client |
| `chat(messages, model_key, temperature, max_tokens)` | Plain text completion → str |
| `chat_json(messages, model_key, temperature, max_tokens)` | chat() + JSON parse (handles markdown code blocks) |
| `chat_vision(text_prompt, image_urls, model_key, temperature, max_tokens)` | Vision-capable multimodal completion |
| `_parse_json(raw)` | Strip markdown fences, parse JSON |

---

### 2.8 `src/agents/product_agent.py` — Browser Agent

**Tech:** browser-use, Playwright, DeepSeek LLM + SlimDOMExtractor

| Method | Description |
|---|---|
| `__init__(headless=False)` | Init with persistent browser profile path (no incognito) |
| `_get_browser()` | Lazy start browser with persistent user data dir (saves cookies/login) |
| `extract(url)` → `dict` | Full SlimDOM pipeline: navigate → collect → AI → click SKUs |
| `extract_batch(urls)` → `list[dict]` | Batch extract multiple URLs in single browser session (no re-login) |
| `close()` | Stop browser (profile preserved on disk) |

**Key feature:** `browser_profile/` persists between runs. After first QR scan, subsequent imports are auto-logged in. No repeat manual login.

---

### 2.9 `src/pipeline/stages.py` — Pipeline Stages

| Stage | Class | Input | Output | Key Logic |
|---|---|---|---|---|
| **1. Load** | `LoadStage` | `url: str` | `Product(scraped)` | ProductAgent.extract() → Product with raw data |
| **2. Extract** | `ExtractStage` | `Product` | `Product` | LLM structured extraction: title_en, description_en, price_usd, tags, sku_prices |
| **3. Analyze** | `AnalyzeStage` | `Product` | `Product` | LLM market scoring 0-100. Below threshold → ARCHIVED; above → ANALYZED |
| **4. Process** | `ProcessStage` | `Product` | `Product` | LLM SEO description + download images + ImageStyler.adapt() |
| **5. Publish** | `PublishStage` | `list[Product]` | `Path` | CSVExporter.export() → Shopify CSV |

**Image Download Security (`_download_images`):**
1. Only http/https URLs
2. DNS resolve → block private/loopback/reserved/multicast IPs
3. Validate Content-Type is `image/*`
4. Max file size: 20MB
5. Only allow image extensions: jpg/jpeg/png/webp/gif/bmp

---

### 2.10 `src/processing/image_styler.py` — Image Processor

**Tech:** OpenCV, Pillow (PIL)

| Method | Description |
|---|---|
| `adapt(input_path, output_dir, title_en, brand_name)` → `Path` | Full pipeline: background whiten → smart crop → color grade → English overlay → resize |
| `adapt_batch(image_paths, output_dir, **kwargs)` → `list[Path]` | Batch adapt |
| `_enhance_background(img)` | Whiten low-saturation/high-value regions |
| `_smart_center_crop(img)` | Contour detection → crop to largest object |
| `_us_color_grade(img)` | Warm/bright/contrast enhancement |
| `_add_english_overlay(img, title, brand)` | Black overlay bar + white text at bottom |
| `_resize_to_shopify(img)` | Resize to max 2048×2048 (Lanczos) |

---

### 2.11 `src/shopify/csv_exporter.py` — CSV Exporter

| Method | Description |
|---|---|
| `export(products, output_path)` → `Path` | Write Shopify-compatible CSV with 17 columns |
| `_product_to_row(product)` → `dict` | Map Product → CSV row dict |

**CSV Columns:** Handle, Title, Body (HTML), Vendor, Product Category, Type, Tags, Published, Option1 Name, Option1 Value, Variant SKU, Variant Price, Variant Compare At Price, Image Src, SEO Title, SEO Description, Status

---

### 2.12 `src/webui/app.py` — Streamlit Dashboard

**Tech:** Streamlit

| Function | Description |
|---|---|
| `_run_async(coro)` | Run async coroutine in sync Streamlit context |
| `get_repo()` | Cached ProductRepository singleton |
| `load_products(status)` | Load products by status filter |
| Main UI | Sidebar: status filter, product count, Export button. Main: expandable product cards with image preview, market score, SKU prices, edit form, Approve/Reject/Archive buttons |

---

## 3. Data Flow Diagram (Interactive Mode)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  INTERACTIVE SOURCING (add-i / batch-add-i)                │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Playwright Browser (PERSISTENT PROFILE)                            │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │  data/browser_profile/ → cookies, login state preserved    │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │         ↑                                                           │   │
│  │  [QR scan once → never login again]                                 │   │
│  └───────────────┬─────────────────────────────────────────────────────┘   │
│                  │                                                           │
│                  ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  SlimDOMExtractor:                                                  │   │
│  │    1. JS collect page (text, imgs+pos, SKU hints, markers)        │   │
│  │    2. LLM → structured JSON (title, price, imgs, SKU labels)     │   │
│  │    3. JS click each SKU → capture real price + variant img        │   │
│  └───────────────┬─────────────────────────────────────────────────────┘   │
│                  │                                                           │
│                  ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Local File System (data/images/{handle}/)                         │   │
│  │    - main_01.jpg ... main_08.jpg (gallery)                         │   │
│  │    - variant-name.jpg (SKU-specific images)                        │   │
│  │    - handle_desc_01.jpg ... (detail section)                       │   │
│  └───────────────┬─────────────────────────────────────────────────────┘   │
│                  │                                                           │
│                  ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  ProductRepository (SQLite) → status=REVIEW_PENDING                │   │
│  └───────────────┬─────────────────────────────────────────────────────┘   │
│                  │                                                           │
│                  ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Streamlit Dashboard → Approve → CSV Export                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Key Design Decisions (No Hardcoded CSS Selectors)

1.  **Persistent browser profile**: No repeated QR login. First scan once, reuse session forever.
2.  **SlimDOM**: AI-driven, no platform-specific CSS selectors. Resilient to site DOM changes.
3.  **SKU click automation**: Don't guess prices; physically click each variant button, read the updated DOM price.
4.  **Layout-based fallbacks**: Find galleries/SKUs by size/proximity, not class names.
5.  **Separate desc_images**: Detail section images are distinct from main gallery — extracted between text markers.

---

## 5. Database Schema (Updated)

```sql
CREATE TABLE IF NOT EXISTS products (
    id                  TEXT PRIMARY KEY,
    platform            TEXT,
    source_url          TEXT,
    title_cn            TEXT,
    price_cn            TEXT,
    description_cn      TEXT,
    images              TEXT,     -- JSON array: local paths (main + SKU variants)
    desc_images         TEXT,     -- NEW: JSON array: local paths (detail section images)
    sku_prices          TEXT,     -- JSON array: [{name, price, images[]}]
    market_score        TEXT,
    title_en            TEXT,
    description_en      TEXT,
    optimized_description TEXT,
    price_usd           REAL,
    tags                TEXT,
    status              TEXT DEFAULT 'scraped',
    shopify_product_id  TEXT,
    error_message       TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_status ON products(status);
```

---

## 6. Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek API key |
| `SHOPIFY_STORE` | No | Shopify store subdomain |
| `SHOPIFY_ACCESS_TOKEN` | No | Shopify Admin API token |

File: `.env` (in `.gitignore`)

---

## 7. Changelog (2026-05-11 — v0.2.0)

| # | Change | What it does |
|---|---|---|
| 1 | New `src/agents/slimdom_extractor.py` | AI-driven DOM layout extraction (zero hardcoded CSS selectors) |
| 2 | New CLI `add-i` command | Interactive mode: browser stays open after extract, profile saved |
| 3 | New CLI `batch-add-i` command | Batch multiple URLs in single browser session (cookie reuse) |
| 4 | ProductAgent.extract_batch() | Batch extraction in same browser, no repeated navigation/login |
| 5 | SKU click automation | Physically click each SKU button → capture real price + variant photo |
| 6 | New `desc_images` field | Separate product detail images from main gallery (extracted between markers) |
| 7 | `run.py` local download | Integrated `_download_images` / `_download_sku_images` (not in stages.py now) |
| 8 | `src/utils.py` extracted | Platform detect, filename sanitize, price normalize — reusable helpers |

---

## 8. Roadmap / TODO

### Phase 1 — Stabilization (Current)
- [x] SlimDOM extractor (no CSS selectors)
- [x] Persistent browser profile (one QR scan forever)
- [x] SKU click → real price collection
- [x] Interactive `add-i` / `batch-add-i` CLI commands
- [ ] Add retry + exponential backoff for LLM calls
- [ ] Add URL deduplication (unique index on source_url)
- [ ] Write unit tests for Pipeline stages

### Phase 2+ (unchanged from v0.1.0)
- [ ] Dockerfile + docker-compose.yml
- [ ] GitHub Actions CI
- [ ] Real Shopify Admin API integration
- [ ] PostgreSQL support
- [ ] REST API (FastAPI)

---

*Document updated: 2026-05-11 | Version: 0.2.0*
