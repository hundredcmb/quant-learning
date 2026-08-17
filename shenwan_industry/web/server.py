"""申万行业研究台的本地 FastAPI 服务。"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import service
from .jobs import JobManager
from .schemas import DailyRankingRequest, RangeRankingRequest, TokenConfigRequest


STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="申万行业研究台", version="0.1.0")
job_manager = JobManager(service.run_worker)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """本地开发工具：页面与静态资源不缓存，避免修改后浏览器拿到旧文件。"""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "ready": service.service_is_ready(),
        "queue_length": job_manager.queue_length(),
    }


@app.get("/api/defaults")
def defaults() -> dict:
    return service.get_default_dates()


@app.get("/api/config")
def get_config() -> dict:
    return service.get_token_config()


@app.post("/api/config")
def save_config(request: TokenConfigRequest) -> dict:
    token = request.token.strip()
    service.save_token(token)
    return {"configured": bool(token)}


@app.post("/api/config/test")
def test_config() -> dict:
    ok, message = service.test_token()
    return {"ok": ok, "message": message}


@app.get("/api/index/available")
def index_available() -> dict:
    """可查看 K 线的行业指数代码列表（L1 + 有官方日线的 L2/L3）"""
    return {"codes": service.get_available_index_codes()}


@app.get("/api/index/{index_code}/kline")
def get_index_kline(
    index_code: str,
    start_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
) -> dict:
    try:
        return service.get_index_kline(index_code, start_date, end_date)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.post("/api/rankings/daily")
def submit_daily(request: DailyRankingRequest) -> dict:
    job = job_manager.submit("daily", request.model_dump())
    return {"job_id": job.id, "status": job.status}


@app.post("/api/rankings/range")
def submit_range(request: RangeRankingRequest) -> dict:
    job = job_manager.submit("range", request.model_dump())
    return {"job_id": job.id, "status": job.status}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    snapshot = job_manager.snapshot(job_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在")
    return snapshot


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    result = job_manager.cancel(job_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@app.get("/api/jobs/{job_id}/constituents/{level}/{index_code}")
def get_constituents(
    job_id: str,
    level: int,
    index_code: str,
    weight: str = Query(default="float", pattern="^(float|equal)$"),
) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "success":
        raise HTTPException(status_code=409, detail="任务尚未完成或已失败")
    if level not in (1, 2, 3):
        raise HTTPException(status_code=422, detail="行业层级必须是 1、2 或 3")

    try:
        return service.build_constituents(job, level, index_code, weight)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="启动申万行业研究台本地 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
