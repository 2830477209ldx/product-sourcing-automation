"""FastAPI server — DOM drilling agent for Chrome extension, feeds into Pipeline."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from src.db.repository import ProductRepository
from src.llm import create_llm_service, LLMService
from src.models.raw_data import ProductRawData
from src.api.ai_agent import agent_step

app = FastAPI(title="Product Sourcing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r".*",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f">>> {request.method} {request.url.path} from {request.client.host if request.client else '?'}")
    response = await call_next(request)
    logger.info(f"<<< {request.method} {request.url.path} -> {response.status_code}")
    return response


_llm: LLMService | None = None
_repo: ProductRepository | None = None
_pipeline = None


def _get_llm() -> LLMService:
    global _llm
    if _llm is None:
        _llm = create_llm_service()
    return _llm


def _get_repo() -> ProductRepository:
    global _repo
    if _repo is None:
        _repo = ProductRepository()
    return _repo


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from src.pipeline.pipeline import Pipeline
        _pipeline = Pipeline(llm=_get_llm(), repo=_get_repo())
    return _pipeline


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": str(exc)[:500]},
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "db": str(Path("data/products.db").exists())}


class AgentStepRequest(BaseModel):
    platform: str = ""
    round: int = 0
    max_rounds: int = 8
    initial: dict = {}
    explored: dict = {}
    collected: dict = {}
    history: list[dict] = []
    skus_available: dict = {}
    debug: bool = False


class ImportRequest(BaseModel):
    url: str = ""
    platform: str = ""
    title_cn: str = ""
    price_cn: str = ""
    image_urls: list[str] = []
    desc_images: list[str] = []
    sku_prices: list[dict] = []
    description_cn: str = ""


@app.post("/api/import")
async def import_product(data: ImportRequest):
    raw = ProductRawData(
        source_url=data.url,
        title_cn=data.title_cn,
        price_cn=data.price_cn,
        image_urls=data.image_urls,
        desc_images=data.desc_images,
        sku_prices=data.sku_prices,
        description_cn=data.description_cn,
    )
    pipeline = _get_pipeline()
    result = await pipeline.run_from_raw(raw)
    if result.failed or not result.data:
        return {"ok": False, "error": result.error or "pipeline failed"}
    p = result.data
    return {
        "ok": True,
        "product_id": p.id,
        "message": f"Imported: {p.title_en or p.title_cn[:50]}",
        "product": {
            "title_en": p.title_en,
            "price_usd": p.price_usd,
            "score": p.market_score.total if p.market_score else 0,
            "tags": p.tags[:8] if p.tags else [],
            "status": p.status.value,
        },
    }


@app.post("/api/ai-agent/step")
async def ai_agent_step(req: AgentStepRequest):
    llm = _get_llm()
    dom_state = {
        "initial": req.initial,
        "explored": req.explored,
        "collected": req.collected,
        "skus_available": req.skus_available,
    }
    logger.info(
        f"[Step {req.round}] platform={req.platform} | "
        f"collected_keys={list(req.collected.keys())} | "
        f"skus_total={req.skus_available.get('total', 0)} | "
        f"history_len={len(req.history)}"
    )
    result = await agent_step(
        llm=llm,
        platform=req.platform or "unknown",
        round_num=req.round,
        dom_state=dom_state,
        history=req.history,
        max_rounds=req.max_rounds,
        debug=req.debug,
    )

    if result.get("done"):
        data = result.get("data", {})
        if data:
            try:
                raw = ProductRawData(
                    source_url=data.get("source_url", req.initial.get("url", "")),
                    title_cn=data.get("title_cn", ""),
                    price_cn=data.get("price_cn", ""),
                    image_urls=data.get("image_urls", []),
                    desc_images=data.get("desc_images", []),
                    sku_prices=data.get("sku_prices", []),
                    description_cn=data.get("description_cn", ""),
                    sku_type=data.get("sku_type", ""),
                )
                pipeline = _get_pipeline()
                pipe_result = await pipeline.run_from_raw(raw)
                if pipe_result.data:
                    p = pipe_result.data
                    result["pipeline"] = {
                        "product_id": p.id,
                        "title_en": p.title_en,
                        "price_usd": p.price_usd,
                        "score": p.market_score.total if p.market_score else 0,
                        "tags": p.tags[:8] if p.tags else [],
                        "status": p.status.value,
                    }
            except Exception as exc:
                logger.error(f"Pipeline run failed on done: {exc}")
                result["pipeline"] = {"error": str(exc)[:200]}

    return result


@app.get("/api/product/{product_id}")
async def get_product(product_id: str):
    repo = _get_repo()
    product = await repo.get(product_id)
    if not product:
        return {"ok": False, "error": "not found"}
    return {
        "ok": True,
        "product": {
            "id": product.id,
            "title_cn": product.title_cn,
            "title_en": product.title_en,
            "price_cn": product.price_cn,
            "price_usd": product.price_usd,
            "score": product.market_score.total if product.market_score else 0,
            "status": product.status.value,
            "image_count": len(product.images),
        },
    }


@app.get("/api/recent")
async def recent_imports(limit: int = 10):
    repo = _get_repo()
    recent = await repo.list_recent(limit)
    return {
        "ok": True,
        "count": len(recent),
        "products": [
            {
                "id": p.id,
                "title_cn": p.title_cn[:80],
                "title_en": p.title_en[:80] if p.title_en else "",
                "price_cn": p.price_cn,
                "price_usd": p.price_usd,
                "score": p.market_score.total if p.market_score else 0,
                "status": p.status.value,
                "created_at": p.created_at,
            }
            for p in recent
        ],
    }


def _find_available_port(host: str, start: int = 8765, tries: int = 20) -> int:
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def _write_port_file(port: int):
    port_file = Path("data") / "api_port.txt"
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))


def run(host: str = "127.0.0.1", port: int = 0):
    import uvicorn

    if port == 0:
        port = _find_available_port(host)

    _write_port_file(port)
    logger.info(f"Starting Product Sourcing API on {host}:{port}")
    uvicorn.run("src.api.server:app", host=host, port=port, log_level="info")
