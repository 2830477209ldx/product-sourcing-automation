# 产品智能提取架构

## 1. 现状痛点

当前 `product_agent.py` 采用硬编码 CSS 选择器：

```
JS_MAIN_GALLERY:  10 组选择器 (ul[class*="PicGallery"], .mainPic, #J-detail-img-list...)
JS_SKU_ITEMS:     6 组选择器 ([class*="skuItem"], [data-sku-id], .sku-item-wrapper...)
JS_CURRENT_PRICE:  8 组选择器 (.tm-price, .totalPrice, .promoPrice...)
JS_DESC_IMAGES:    8 组选择器 (#description, [class*="desc-detail"], iframe...)
```

**问题**：
- 淘宝/天猫不同活动页（双11、618、日常）DOM 结构不一致，同款选择器覆盖率 < 60%
- 1688、拼多多、抖音电商需要从头写一套新选择器，每个平台 ~200 行 JS 代码
- 平台改版后选择器全量失效，维护靠人工发现 → 修复 → 测试，周期 1-3 天
- 描述区图片（iframe 懒加载）经常漏抓

## 2. 目标架构：三层流水线

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  AI 分析层    │ ──→ │  脚本执行层   │ ──→ │  数据聚合层   │
│  接收 DOM     │     │  执行 JS 指令  │     │  返回 Product │
│  输出指令 JSON │     │  在页面提取    │     │              │
└──────────────┘     └──────────────┘     └──────────────┘
```

## 3. 核心流程

```
浏览器加载页面
  │
  ├─ ① 提取精简 DOM (SlimDOM)
  │     只保留: tagName, className 前 2 段, 可见文本前 80 字,
  │             img 的 src/data-src, 元素的 rect 位置(top/left/width/height)
  │     丢弃: style, script, svg, 不可见元素, 纯布局 div(无文本/无img)
  │     目标: 100KB 以内
  │
  ├─ ② AI 分析 SlimDOM → 输出 ExtractionPlan JSON
  │
  ├─ ③ 脚本执行层逐条运行 ExtractionPlan 中的 JS 代码
  │     失败项自动 fallback 到通用策略
  │
  └─ ④ 数据聚合层汇总 → 返回完整 Product
```

## 4. SlimDOM 规范

页面加载完成后，JS 脚本在浏览器端执行 DOM 精简，输出结构：

```json
{
  "url": "https://item.taobao.com/item.htm?id=...",
  "title": "document.title",
  "nodes": [
    {
      "t": "div",           // tagName
      "c": "PicGallery",    // className 前 2 段 (空格分隔取前2)
      "v": "红色 M码",       // 可见文本前 80 字
      "r": [0, 200, 400, 600],  // rect [top, left, width, height] ← 整数
      "a": {                // 关键属性
        "src": "https://...",
        "data-src": "https://..."
      }
    }
  ]
}
```

**精简规则**：
- 只保留 `display !== 'none'` 且 `visibility !== 'hidden'` 的节点
- `tagName` 为 `div/span/a/img/li/ul/dl/dt/dd/button/select/option/em/strong/h1-h6/p/table/tr/td` 之外的元素直接丢弃
- `className` 最多保留空格分隔的前 2 段（如 `"skuItem selected disable"` → `"skuItem selected"`）
- 深度限制：最多 15 层嵌套
- 纯布局容器（无文本、无 img、无 a 标签）不进入输出

## 5. ExtractionPlan — AI 输出指令格式 (严格 JSON Schema)

AI 收到 SlimDOM 后，必须输出如下结构的 JSON。5 个 section 按序执行：

```json
{
  "platform": "taobao",
  "confidence": 0.85,

  "title": {
    "selector": "h1[class*='title']",
    "fallback": "document.title.split(' - ')[0]",
    "extract": "el?.textContent?.trim()"
  },

  "price": {
    "selector": "[class*='price'] [class*='value']",
    "fallback": "[class*='PriceBox'] span",
    "extract": "el?.textContent?.trim()"
  },

  "main_images": {
    "container": "ul[class*='PicGallery']",
    "item_selector": "li img",
    "src_attr": ["src", "data-src", "data-original"],
    "url_prefix": "https:",
    "min_count": 1
  },

  "skus": {
    "container": "[class*='skuWrapper']",
    "option_selector": "span[title], a",
    "label_attr": ["title", "textContent"],
    "click_strategy": "querySelector",
    "max_options": 50,
    "price_after_click": {
      "selector": "[class*='price'] [class*='value']",
      "wait_ms": 800
    },
    "image_after_click": {
      "selector": "ul[class*='PicGallery'] li img",
      "take_first": true
    }
  },

  "desc_images": {
    "container": "#description, [class*='desc-detail'], [class*='detail-content']",
    "item_selector": "img",
    "src_attr": ["src", "data-src", "data-ks-lazyload", "data-original"],
    "url_prefix": "https:",
    "fallback_if_empty": "iframe img"
  }
}
```

**字段约束**：
- `selector` / `item_selector` 必须是 CSS 选择器字符串，不接受 XPath
- `fallback` 字段可选，用于主选择器失败时的兜底
- `src_attr` 按优先级排序，逐个尝试取到非空值
- SKU 的 `price_after_click.wait_ms` 建议 500-1500ms
- `confidence` 为 AI 对自己的选择器成功率的预估 (0-1)，<0.6 时触发人工复核

## 6. 脚本执行层

解析 ExtractionPlan，逐项执行 JS 代码。每项有独立的 try-catch + fallback：

```
for section in [title, price, main_images, skus, desc_images]:
    try:
        result = page.evaluate(plan[section].to_js())
        if not result:
            result = page.evaluate(plan[section].fallback_js())
    except:
        result = _generic_fallback(section, page)
```

### 通用 Fallback 策略

当 AI 选择器全部失效时，不直接报错，而是降级到通用提取：

| Section       | Fallback 逻辑                                            |
|---------------|----------------------------------------------------------|
| title         | `document.title.split(' - ')[0].split(' | ')[0]`       |
| price         | 全页搜索第一个匹配 `¥\s*[\d,.]+` 的文本节点              |
| main_images   | 取页面顶部 60% 区域内所有 `width>=200 && height>=200` 的 img |
| skus          | 搜索所有 `[data-value], [data-sku-id]` 元素              |
| desc_images   | 取页面底部 40% 区域内所有 img，排除已入 main_images 的    |

## 7. 数据聚合层

```
AgentOutput (5 个 section 的原始结果)
  │
  ├─ 去重 (URL 去重、SKU label 去重)
  ├─ URL 标准化 (// → https://, 去掉 _200x200 尺寸后缀)
  ├─ 图片过滤 (排除 icon/logo/btn/banner/qr_code)
  ├─ 价格清洗 (保留数字和 ¥ 符号)
  │
  └─ 输出 Product 对象
```

## 8. 与现有系统的集成点

| 现有模块             | 改动                                       |
|---------------------|--------------------------------------------|
| `product_agent.py`   | 替换 4 个硬编码 JS 为 SlimDOM + ExtractionPlan 执行器 |
| 导航层               | ~~browser_use Agent~~ → 直接 `browser.new_page(url)` + 手动登录检测，消除 Agent tab detach/reattach 循环 |
| `stages.py`          | 不变，ExtractionPlan 输出直接对接 ExtractStage |
| `config.py`          | 不变                                       |
| 数据库 Schema         | 不变                                       |
| Streamlit WebUI      | 不变                                       |

## 9. 性能 & 成本估算

| 环节               | 耗时    | LLM Token |
|---------------------|---------|-----------|
| 浏览器导航 + 登录    | 2-8s    | 0         |
| SlimDOM 提取         | 0.5-1s  | 0         |
| AI 分析 SlimDOM → Plan | 2-5s | ~3K input / ~500 output |
| 脚本执行层 (5 项)    | 0.5-3s  | 0         |
| 数据聚合             | 0.1s    | 0         |
| **总计**            | **5-17s** | **~3500 tokens** |

对比当前方案：硬编码执行 3-8s，但失败时需人工介入（数小时）。

## 10. 迁移计划

| Phase | 范围                 | 策略                                    |
|-------|----------------------|------------------------------------------|
| 1     | 淘宝/天猫             | 双轨运行：硬编码 + AI 并行，对比结果 2 周  |
| 2     | 1688                 | 直接切换到 AI，硬编码仅作 fallback        |
| 3     | 拼多多、抖音电商      | 纯 AI 方案，零硬编码                      |
| 4     | 全平台               | 下线所有硬编码选择器                      |

## 11. 错误处理

```
ExecutionResult {
    section: "main_images" | "price" | "skus" | "title" | "desc_images",
    status: "ok" | "fallback" | "failed",
    source: "ai_plan" | "generic_fallback" | "none",
    data: [...],
    error?: string,
    duration_ms: number
}
```

- 3/5 section `ok` → 视为成功，标记缺少的 section
- <3/5 section `ok` → 记录完整 SlimDOM + ExtractionPlan 到日志，触发人工复核
- `confidence` < 0.6 的 Plan → 自动标记为需审核，但不阻塞流水线
