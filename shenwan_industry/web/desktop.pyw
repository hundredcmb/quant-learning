"""申万行业研究台桌面窗口启动器。

双击本文件时：
1. 检查本机 8080 端口是否已有可用的申万行业 Web 服务；
2. 如果没有，后台启动 `shenwan_industry.web.server`；
3. 等后端就绪后，用 Qt WebEngine 加载前端页面；
4. 关闭窗口时，只结束由本启动器拉起的后端进程。
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from PySide6.QtWebEngineWidgets import QWebEngineView


HOST = "127.0.0.1"
PORT = 8080
BASE_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{BASE_URL}/api/health"
REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = REPO_ROOT / "output" / "desktop_backend.log"
LOADING_HTML = Path(__file__).resolve().parent / "static" / "loading.html"


def is_backend_ready() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and data.get("status") == "ok"
    except Exception:
        return False


def start_backend() -> tuple[subprocess.Popen, object]:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = LOG_PATH.open("ab", buffering=0)
    command = [
        sys.executable,
        "-m",
        "shenwan_industry.web.server",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    return process, log_file


def wait_for_backend(timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_backend_ready():
            return True
        QApplication.processEvents()
        time.sleep(0.2)
    return False


class DesktopWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.backend_process: subprocess.Popen | None = None
        self.backend_log_file: object | None = None
        self.started_by_us = False
        self._backend_stopped = False
        self.web_view: QWebEngineView | None = None

        self.setWindowTitle("申万行业研究台")
        self.resize(1280, 800)
        self.setMinimumSize(960, 640)

        self.web_view = QWebEngineView()
        self.web_view.load(QUrl.fromLocalFile(str(LOADING_HTML)))
        self.setCentralWidget(self.web_view)

    def attach_backend(
        self,
        process: subprocess.Popen | None,
        log_file: object | None,
        started_by_us: bool,
    ) -> None:
        self.backend_process = process
        self.backend_log_file = log_file
        self.started_by_us = started_by_us

    def show_frontend(self) -> None:
        if self.web_view is None:
            self.web_view = QWebEngineView()
            self.setCentralWidget(self.web_view)
        self.web_view.load(QUrl(f"{BASE_URL}/"))

    def stop_owned_backend(self) -> None:
        if self._backend_stopped or not self.started_by_us or self.backend_process is None:
            return
        self._backend_stopped = True
        if self.backend_process.poll() is None:
            self.backend_process.terminate()
            try:
                self.backend_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.backend_process.kill()
                self.backend_process.wait(timeout=3)
        if self.backend_log_file is not None:
            try:
                self.backend_log_file.close()
            except Exception:
                pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 事件方法名约定
        self.stop_owned_backend()
        event.accept()


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("申万行业研究台")

    window = DesktopWindow()
    window.show()

    process = None
    log_file = None
    started_by_us = False

    if not is_backend_ready():
        process, log_file = start_backend()
        started_by_us = True
        if not wait_for_backend():
            if process.poll() is None:
                process.terminate()
            QMessageBox.critical(
                window,
                "启动失败",
                "后端服务启动超时，请查看：\n" + str(LOG_PATH),
            )
            window.close()
            return 1

    window.attach_backend(process, log_file, started_by_us)
    window.show_frontend()
    app.aboutToQuit.connect(window.stop_owned_backend)
    atexit.register(window.stop_owned_backend)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
