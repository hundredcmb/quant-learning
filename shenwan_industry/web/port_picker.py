"""端口自动选择：首选端口绑定失败时顺延下一个端口。

背景：Windows 的 Hyper-V/WSL2/WinNAT 会动态保留一段 TCP 端口（`netsh interface
ipv4 show excludedportrange protocol=tcp` 可查），保留段内的端口 bind 时报
WinError 10013（WSAEACCES），普通进程无法绑定；被其他进程占用时报 WinError
10048（WSAEADDRINUSE）。两类失败都意味着“当前不可用”。

方案 B：先对首选端口做一次真实 bind 实测，失败即顺延 +1 重试，最多试
`max_attempts` 个端口；探测成功后把端口交给 uvicorn 使用。仅依赖标准库
socket，.venv 与 .venv-vnpy 两个环境均可直接 import（server.py 与 desktop.pyw 共用）。
"""

from __future__ import annotations

import socket

# 绑定失败即视为“端口不可用”的 errno 集合：
#   Windows: WSAEACCES=10013（端口处于系统保留排除段）、WSAEADDRINUSE=10048（被占用）
#   POSIX:   EACCES=13 / EADDRINUSE=98
_BIND_BLOCKED_ERRNOS = {13, 98, 10013, 10048}


def pick_free_port(host: str, preferred: int, max_attempts: int = 200) -> int:
    """从 `preferred` 起逐端口实测 bind，返回第一个可成功绑定的端口。

    探测 socket 与 uvicorn 同样开启 SO_REUSEADDR，语义接近 uvicorn 的绑定；
    排除段端口在 SO_REUSEADDR 下同样会被系统拒绝，探测结果可靠。
    全部候选都不可用时抛 RuntimeError 并给出排查提示。
    """
    port = preferred
    for _ in range(max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError as err:
                if err.errno not in _BIND_BLOCKED_ERRNOS:
                    raise
        port += 1
    raise RuntimeError(
        f"端口 {preferred}~{preferred + max_attempts - 1} 均不可用"
        "（可能被进程占用或处于系统保留段）。"
        "可用 `netsh interface ipv4 show excludedportrange protocol=tcp` 排查，"
        "或用 --port 指定一个排除段外的端口。"
    )
