"""仓库级公共配置存储（Tushare token），holders 与 shenwan_industry 共享。

配置保存在**项目根目录**的 `.quant-learning/settings.json`（已被 `.gitignore` 忽略、
不随仓库提交），文件权限 600（Windows 除外）。token 解析优先级（见 resolve_token）：

1. 命令行参数 `--token`（供定时任务等非交互环境使用）
2. 已保存的本地配置
3. 终端交互输入（输入后自动保存，下次无需重复输入）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_KEY_TOKEN = "tushare_token"
_CONFIG_DIR_NAME = ".quant-learning"


def config_path() -> Path:
    """返回本地配置文件路径（项目根目录下，不随仓库提交）。"""
    return Path(__file__).resolve().parent / _CONFIG_DIR_NAME / "settings.json"


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


def resolve_token(cli_token: str | None = None) -> str:
    """解析可用的 Tushare token：命令行参数 > 已保存配置 > 终端交互输入。

    - cli_token：来自脚本命令行参数 `--token`；非空时直接采用并自动保存
    - 已有保存配置时直接返回，不做任何改动
    - 两者皆无时在终端提示输入，输入非空后自动保存供未来使用
    - 非交互终端（stdin 已关闭）无法输入时返回空串并打印指引，
      调用方应据此报错退出
    """
    token = (cli_token or "").strip()
    if token:
        if token != get_token():
            set_token(token)
            print(f"✅ token 已保存到 {config_path()}，后续运行无需再传 --token")
        return token

    token = get_token()
    if token:
        return token

    print(f"未找到 Tushare token 配置（{config_path()}）")
    try:
        token = input("请输入 Tushare token（回车确认，将自动保存供未来使用）：").strip()
    except EOFError:
        print("\n⚠️ 当前是非交互终端，无法手动输入；请改用命令行参数指定：--token <你的token>")
        return ""
    if not token:
        print("⚠️ 未输入任何内容，本次不保存")
        return ""
    set_token(token)
    print(f"✅ token 已保存到 {config_path()}，后续运行无需重复输入")
    return token
