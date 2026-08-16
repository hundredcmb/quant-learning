"""申万模块本地配置存储（Tushare token 等）。

配置保存在**项目根目录**的 `.quant-learning/settings.json`（已被 `.gitignore` 忽略、
不随仓库提交），文件权限 600（Windows 除外）。本模块不依赖 vnpy，供申万 CLI 与
Web 服务统一读取/保存 Tushare token。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_KEY_TOKEN = "tushare_token"
_CONFIG_DIR_NAME = ".quant-learning"


def config_path() -> Path:
    """返回本地配置文件路径（项目根目录下，不随仓库提交）。"""
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / _CONFIG_DIR_NAME / "settings.json"


def _load() -> dict[str, str]:
    path = config_path()
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, str]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def get_token() -> str:
    """读取已保存的 Tushare token，未配置返回空字符串。"""
    return str(_load().get(_KEY_TOKEN, "")).strip()


def set_token(token: str) -> None:
    """保存 Tushare token；传入空字符串表示清除已保存的 token。"""
    data = _load()
    token = (token or "").strip()
    if token:
        data[_KEY_TOKEN] = token
    else:
        data.pop(_KEY_TOKEN, None)
    _save(data)
