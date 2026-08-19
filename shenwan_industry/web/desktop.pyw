"""申万行业研究台桌面窗口启动器。

双击本文件时：
1. 检查本机 9010 端口是否已有可用的申万行业 Web 服务；
2. 如果没有，后台启动 `shenwan_industry.web.server`；
3. 窗口从创建起就直接是 QWebEngineView（无任何原生加载页/骨架屏过渡），
   引擎冷启动与后端启动并行进行，期间窗口为白屏（可接受的代价）；
   创建时立即预载 about:blank，让渲染器冷启动协商在画面呈现前完成，
   避免后端就绪后首次加载时的"缩小再放大"首帧协商闪烁（已实测确认）；
4. 后端就绪后一次性加载正式页面 http://127.0.0.1:9010/（渲染器已热，首帧干净），
   不存在任何中间页面切换；
5. 关闭窗口时，只结束由本启动器拉起的后端进程。
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
PORT = 9010
BASE_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{BASE_URL}/api/health"
REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = REPO_ROOT / "output" / "desktop_backend.log"


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


class AppWebView(QWebEngineView):
    """覆盖 sizeHint: QWebEngineView 默认返回 800x600, 可能干扰窗口布局"""

    def sizeHint(self) -> object:
        window = self.window()
        if window is not None and window is not self:
            return window.size()
        return super().sizeHint()


class DesktopWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.backend_process: subprocess.Popen | None = None
        self.backend_log_file: object | None = None
        self.started_by_us = False
        self._backend_stopped = False
        self._frontend_loaded = False

        self.setWindowTitle("申万行业研究台")
        self.resize(1280, 800)
        self.setMinimumSize(960, 640)

        # 窗口从启动起就是 WebView: 无任何中间过渡页, 从根上避免切换闪烁
        self.web_view = AppWebView()
        self.setCentralWidget(self.web_view)
        # 立即预载 about:blank: 渲染器冷启动(表面/缩放协商)在窗口尚未呈现
        # 有意义画面时完成; 若等后端就绪才首次加载, 冷启动协商会暴露成
        # "窗口先缩小再放大"的一帧(已实测: 几何恒不变, 是渲染器内部协商)
        self.web_view.load(QUrl("about:blank"))

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
        """后端就绪后加载正式页面(只加载一次)"""
        if self._frontend_loaded:
            return
        self._frontend_loaded = True
        self.web_view.load(QUrl(BASE_URL))

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
