"""
数据缓存提交前检查入口（轻量变体，不动 git 配置）

三个持久数据缓存（data/dividend_history.json / share_change_events.json /
repurchase_records.json）每次跑单日榜都会被刷新线程自动增量推进，其中多数日子只是
水位/时间戳字段变化、无信息增量（每个版本约 0.2~1MB 压缩历史、提交即永久累积）。
本工具在提交前给出精确判定，避免"纯时间戳刷新"进仓库历史：

- 无变化：工作区与 HEAD 完全一致
- 纯时间戳刷新：剥离水位/记账字段后与 HEAD 完全一致（零信息增量）
- 有实质变化：剥离后仍有差异（真实分红/股本/回购事件变化，可提交）

各文件剥离的字段（均为增量探测水位或纯记账字段，不参与任何计算——逐股 updated
已验证全仓库无读取方）：

- dividend_history.json：顶层 last_refresh（探测水位）、逐股 updated（记账）
- share_change_events.json：snapshot_date（逐日快照水位）
- repurchase_records.json：months_done（月度拉取水位）

--revert-pure 把纯时间戳刷新的文件恢复为 HEAD 版本（git checkout HEAD -- <path>，
工作区与暂存区一起还原）。语义安全：被丢弃的只是水位，下次运行会从 HEAD 里的旧水位
重放增量探测（多几次探测请求）自愈，数据无损；有实质变化的文件不动，由提交人决定。

用法（仓库根目录）：
    python -m shenwan_industry.cache_commit_guard               # 只读检查
    python -m shenwan_industry.cache_commit_guard --revert-pure # 检查并丢弃纯时间戳刷新
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 受检缓存清单：git 路径（相对仓库根，posix 风格）+ 剥离字段说明
CACHE_SPECS: list[dict[str, Any]] = [
    {
        "rel": "shenwan_industry/data/dividend_history.json",
        "label": "分红缓存",
        "strip_top": ("last_refresh",),
        # stocks 下每只股票 dict 里的 updated 记账字段
        "strip_nested": (("stocks", "updated"),),
    },
    {
        "rel": "shenwan_industry/data/share_change_events.json",
        "label": "股本台阶缓存",
        "strip_top": ("snapshot_date",),
        "strip_nested": (),
    },
    {
        "rel": "shenwan_industry/data/repurchase_records.json",
        "label": "回购公告缓存",
        "strip_top": ("months_done",),
        "strip_nested": (),
    },
]

STATUS_CLEAN = "clean"
STATUS_PURE = "pure"
STATUS_REAL = "real"
STATUS_NO_HEAD = "no_head"


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _strip(obj: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """深拷贝后剥离水位/记账字段，返回用于比较的视图（不改原对象）"""
    stripped = copy.deepcopy(obj)
    for key in spec["strip_top"]:
        stripped.pop(key, None)
    for section, field in spec["strip_nested"]:
        section_obj = stripped.get(section)
        if isinstance(section_obj, dict):
            for value in section_obj.values():
                if isinstance(value, dict):
                    value.pop(field, None)
    return stripped


def _diff_notes(head: dict[str, Any], work: dict[str, Any]) -> list[str]:
    """剥离视图逐顶层段汇总差异规模（供人工判断是否值得提交）"""
    notes: list[str] = []
    for key in sorted(set(head) | set(work)):
        head_value = head.get(key)
        work_value = work.get(key)
        if head_value == work_value:
            continue
        if key not in head:
            notes.append(f"{key}: 新增")
        elif key not in work:
            notes.append(f"{key}: 移除")
        elif isinstance(head_value, dict) and isinstance(work_value, dict):
            changed = sum(
                1 for k in set(head_value) | set(work_value)
                if head_value.get(k) != work_value.get(k)
            )
            notes.append(f"{key}: {changed} 处条目不同")
        elif isinstance(head_value, list) and isinstance(work_value, list):
            notes.append(f"{key}: {len(head_value)}→{len(work_value)} 条")
        else:
            notes.append(f"{key}: 值不同")
    return notes


def check_cache(spec: dict[str, Any]) -> dict[str, Any]:
    """检查单个缓存：返回 {status, notes, error}"""
    result: dict[str, Any] = {"status": None, "notes": [], "error": None}
    work_path = _REPO_ROOT / spec["rel"]

    head = _git(["show", f"HEAD:{spec['rel']}"])
    if head.returncode != 0:
        result["status"] = STATUS_NO_HEAD
        result["notes"] = ["HEAD 中无此文件（从未提交过）"]
        return result
    if not work_path.exists():
        result["status"] = STATUS_REAL
        result["notes"] = ["工作区文件缺失"]
        return result

    try:
        head_obj = json.loads(head.stdout)
        work_obj = json.loads(work_path.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001 - JSON 解析失败提示人工排查
        result["status"] = STATUS_REAL
        result["error"] = f"JSON 解析失败（HEAD 版或工作区版损坏？）: {err}"
        return result

    if head_obj == work_obj:
        result["status"] = STATUS_CLEAN
        return result
    head_view = _strip(head_obj, spec)
    work_view = _strip(work_obj, spec)
    if head_view == work_view:
        result["status"] = STATUS_PURE
        return result
    result["status"] = STATUS_REAL
    result["notes"] = _diff_notes(head_view, work_view)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="数据缓存提交前检查：判定纯时间戳刷新/实质变化，可选丢弃纯刷新"
    )
    parser.add_argument(
        "--revert-pure",
        action="store_true",
        help="把'纯时间戳刷新'的文件恢复为 HEAD 版本（git checkout HEAD -- <path>）",
    )
    args = parser.parse_args()

    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        print("错误：不在 git 仓库内", file=sys.stderr)
        raise SystemExit(1)

    revert_targets: list[str] = []
    pure_any = False
    for spec in CACHE_SPECS:
        result = check_cache(spec)
        name = Path(spec["rel"]).name
        if result["status"] == STATUS_CLEAN:
            print(f"{name}（{spec['label']}）: 无变化")
        elif result["status"] == STATUS_PURE:
            pure_any = True
            print(f"{name}（{spec['label']}）: 纯时间戳刷新（剥离水位字段后无差异，零信息增量）")
            if args.revert_pure:
                revert_targets.append(spec["rel"])
        elif result["status"] == STATUS_NO_HEAD:
            print(f"{name}（{spec['label']}）: HEAD 无版本（首次入库按实质变化处理）")
        else:
            detail = "；".join(result["notes"]) or "（差异不在顶层段）"
            extra = f"  [异常: {result['error']}]" if result["error"] else ""
            print(f"{name}（{spec['label']}）: 有实质变化（{detail}）{extra}")

    if args.revert_pure and revert_targets:
        for rel in revert_targets:
            done = _git(["checkout", "HEAD", "--", rel])
            if done.returncode != 0:
                print(f"错误：还原 {rel} 失败: {done.stderr.strip()}", file=sys.stderr)
                raise SystemExit(1)
            print(f"已还原 {rel} 为 HEAD 版本（纯时间戳刷新已丢弃，本地水位下次运行自动重放）")
    elif pure_any and not args.revert_pure:
        print("提示：以上纯时间戳刷新可用 --revert-pure 丢弃（恢复为 HEAD 版本，数据无损）")


if __name__ == "__main__":
    main()
