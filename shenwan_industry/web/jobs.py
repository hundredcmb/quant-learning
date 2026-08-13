"""后台任务队列与 Job 状态管理。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue
from typing import Any, Callable


class JobCancelled(Exception):
    """任务已被用户取消。"""


ProgressFunction = Callable[[float, str, str | None], None]
WorkerFunction = Callable[
    ["Job", ProgressFunction, Callable[[], None]],
    tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, int]],
]


@dataclass
class Job:
    """单个排行任务。"""

    id: str
    mode: str
    payload: dict[str, Any]
    status: str = "queued"
    cancel_requested: bool = False
    progress: float = 0.0
    phase: str = "排队中"
    message: str = ""
    queue_position: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    error: str | None = None
    timings: dict[str, Any] | None = None
    api_calls: dict[str, int] | None = None


class JobManager:
    """单 worker 串行任务队列。"""

    def __init__(self, worker_fn: WorkerFunction):
        self._worker_fn = worker_fn
        self._jobs: dict[str, Job] = {}
        self._queue: Queue[str] = Queue()
        self._lock = threading.RLock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, mode: str, payload: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex, mode=mode, payload=payload)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job.id)
        self._refresh_queue_positions()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            return {}
        with self._lock:
            return {
                "job_id": job.id,
                "mode": job.mode,
                "status": job.status,
                "cancel_requested": job.cancel_requested,
                "progress": job.progress,
                "phase": job.phase,
                "message": job.message,
                "queue_position": job.queue_position,
                "created_at": job.created_at.isoformat(),
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "result": job.result,
                "error": job.error,
                "timings": job.timings,
                "api_calls": job.api_calls,
            }

    def queue_length(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status == "queued")

    def cancel(self, job_id: str) -> dict[str, str]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"status": "not_found"}
            if job.status in ("success", "error", "cancelled"):
                return {"status": job.status}
            if job.status == "queued":
                job.status = "cancelled"
                job.phase = "已取消"
                job.progress = 100.0
                job.message = ""
                job.finished_at = datetime.now()
                job.queue_position = 0
            else:
                job.cancel_requested = True
                job.phase = "正在取消"
            status = job.status
        self._refresh_queue_positions()
        return {"status": status}

    def _refresh_queue_positions(self) -> None:
        with self._lock:
            queued = list(self._queue.queue)
            position = 0
            for job_id in queued:
                job = self._jobs.get(job_id)
                if job is not None and job.status == "queued":
                    position += 1
                    job.queue_position = position
                elif job is not None:
                    job.queue_position = 0

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                job.status = "running"
                job.started_at = datetime.now()
                job.queue_position = 0

            self._refresh_queue_positions()

            def progress(percent: float, message: str, phase: str | None = None) -> None:
                with self._lock:
                    job.progress = round(min(max(percent, 0.0), 100.0), 1)
                    job.message = message
                    if phase is not None:
                        job.phase = phase

            def cancel_check() -> None:
                with self._lock:
                    if job.cancel_requested:
                        raise JobCancelled()

            try:
                result, context, timings, api_calls = self._worker_fn(job, progress, cancel_check)
                with self._lock:
                    job.status = "success"
                    job.progress = 100.0
                    job.phase = "完成"
                    job.message = ""
                    job.result = result
                    job.context = context
                    job.timings = timings
                    job.api_calls = api_calls
            except JobCancelled:
                with self._lock:
                    job.status = "cancelled"
                    job.phase = "已取消"
                    job.message = ""
                    job.error = None
                    job.progress = 100.0
            except Exception as err:  # noqa: BLE001 - 后台任务需要兜底并记录错误
                with self._lock:
                    job.status = "error"
                    job.phase = "失败"
                    job.message = str(err)
                    job.error = str(err)
            finally:
                with self._lock:
                    job.finished_at = datetime.now()
                self._refresh_queue_positions()
