"""申万行业研究台的本地 FastAPI 服务。"""

from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import port_picker, service
from .jobs import JobManager
from .schemas import DailyRankingRequest, RangeRankingRequest, TokenConfigRequest


def _parent_alive_windows(pid: int) -> bool:
    """Windows: 父进程是否存活(OpenProcess + GetExitCodeProcess == STILL_ACTIVE)"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 权限不足(打不开但进程可能在)视为存活, 避免误杀; 其余(无此进程)视为已退出
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _parent_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parent_alive(pid: int) -> bool:
    return _parent_alive_windows(pid) if os.name == "nt" else _parent_alive_posix(pid)


def _run_parent_watchdog(parent_pid: int) -> None:
    """后台看门狗(daemon): 父进程(桌面启动器)退出后自动结束本服务, 避免端口/进程残留。

    覆盖"启动器被强制终止(如 IDE 停止/杀进程)"等 closeEvent/atexit 均无法触发清理的场景。
    """
    while True:
        time.sleep(2)
        if not _parent_alive(parent_pid):
            os._exit(1)


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


@app.get("/api/index/{index_code}/valuation")
def get_index_valuation(index_code: str) -> dict:
    """查询行业指数估值走势(PE/PB)序列状态；ready 时携带序列数据"""
    try:
        return service.get_index_valuation(index_code)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.post("/api/index/{index_code}/valuation")
def start_index_valuation(index_code: str) -> dict:
    """启动(或并入)行业指数估值走势后台计算(同指数幂等)"""
    try:
        return service.start_index_valuation(index_code)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


@app.get("/api/stock/{ts_code}/kline")
def get_stock_kline(
    ts_code: str,
    start_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
) -> dict:
    try:
        return service.get_stock_kline(ts_code, start_date, end_date)
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
    weight: str = Query(default="float", pattern="^(total|total_tr|float|float_tr|equal|equal_tr)$"),
    sample: str = Query(default="full", pattern="^(full|csi800|csi1800)$"),
) -> dict:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "success":
        raise HTTPException(status_code=409, detail="任务尚未完成或已失败")
    if level not in (1, 2, 3):
        raise HTTPException(status_code=422, detail="行业层级必须是 1、2 或 3")

    try:
        return service.build_constituents(job, level, index_code, weight, sample)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="启动申万行业研究台本地 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9010)
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="父进程(桌面启动器) PID; 提供时启动后台看门狗, 父进程退出后本服务自动结束(防端口/进程残留)",
    )
    args = parser.parse_args()
    # 首选端口被占用或落在系统保留段（WinError 10013）时自动顺延，并打印实际端口
    port = port_picker.pick_free_port(args.host, args.port)
    if port != args.port:
        print(f"警告：端口 {args.port} 不可用，已自动改用端口 {port}", flush=True)
    print(f"申万行业研究台已启动：http://{args.host}:{port}/", flush=True)
    if args.parent_pid and args.parent_pid > 0:
        threading.Thread(
            target=_run_parent_watchdog, args=(args.parent_pid,), daemon=True, name="parent-watchdog"
        ).start()
    service.prebuild_context()  # 后台预建行业树(不阻塞启动), 首次查询即可就绪
    service.prebuild_sw_daily_available()  # 后台默默探测官方指数日线可用性(不阻塞启动, 前端无感)
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
