"""ETF 十大持有人公共模块。

数据来源约定：
- ETF 十大持有人与基础信息：只从本地缓存读取（手动导入，不调用 Tushare）
- ETF 日线行情：从 Tushare `fund_daily` 直接获取（至少 5000 积分），
  按 `trade_date` 一次请求拉全市场，**不建价格缓存、不做限流**

代码兼容规则：
- 持有人缓存与基础信息缓存的 key 均为**无后缀代码**（如 `159001`）
- `etf_basic.json` 的 value 记录 `import_code`（Excel 导入格式代码，如 `159001.OF`，
  导入时更新）与 `ts_code`（tushare 代码，如 `159001.SZ`，拉取日线时回填）
- 需要 tushare 代码时**优先取 `etf_basic` 里的 `ts_code`**；若为空（尚未拉取过），
  按沪深两个市场枚举后缀（`.SH` / `.SZ`）解析

注意：ETF 与股票代码都是 6 位，但属于不同标的类型，严禁混淆。
"""
import json
import os
import time

import tushare as ts
from tushare.pro.client import DataApi
from vnpy.trader.setting import SETTINGS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLDERS_CACHE_FILE = os.path.join(BASE_DIR, "etf_top10_holders_raw.json")
BASIC_CACHE_FILE = os.path.join(BASE_DIR, "etf_basic.json")

# 图片等运行产物统一输出到仓库根目录 output/（已在 .gitignore 中忽略）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 席位关键词-折算比例（示例默认值，按你的分析需要启用/调整；
# ETF 十大持有人常见 券商/资管/理财/基金/保险/汇金 等）
KEY_WORD_RATIO = {
    # =========机构席位（示例默认）=========
    "证券": 1.0,  # 证券公司/证券投资基金
    "资管": 1.0,  # 资管计划/资管公司
    "理财": 1.0,  # 理财公司产品
    "基金": 1.0,  # 基金/联接基金
    "保险": 1.0,  # 保险公司/保险资管

    # =========国家队/特殊机构（按需启用）=========
    # "中央汇金": 1.0,
    # "中国证券金融": 1.0,
    # "社保基金": 1.0,
}
# ====================================================

# ===================== 初始化Tushare接口 =====================
token: str = SETTINGS["datafeed.password"]
if not token:
    raise ValueError("请先在 vnpy 的 datafeed.password 配置中设置你的 tushare token")

pro: DataApi = ts.pro_api(token=token)
# ====================================================


def load_json(path: str) -> dict:
    """加载 JSON 缓存，文件不存在或损坏时返回 {}"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ 缓存加载失败（{path}）：{e}")
        return {}


HOLDERS_CACHE: dict = load_json(HOLDERS_CACHE_FILE)
BASIC_CACHE: dict = load_json(BASIC_CACHE_FILE)


def save_basic_cache() -> None:
    """保存基础信息缓存（ts_code 回填后调用）"""
    with open(BASIC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(BASIC_CACHE, f, ensure_ascii=False, indent=2)


def _call_with_retry(fn, retries: int = 3):
    """轻量重试：瞬时错误（网络/超时等）指数退避重试；权限/参数类错误不重试直接抛"""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            msg = str(e)
            if any(k in msg for k in ("权限", "积分", "抱歉")):
                raise
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_exc


# ===================== ETF 基础信息 =====================

def get_combined_etfs() -> dict:
    """ETF 样本池：基础信息缓存中的所有 ETF，返回 {无后缀代码: 名称}"""
    return {code: info.get("name", "") for code, info in BASIC_CACHE.items()}


# ===================== ETF 十大持有人（只读缓存） =====================

def get_etf_holders(ts_code: str, period: str) -> list:
    """获取单只 ETF 指定报告期的持有人记录（仅缓存，空列表=未录入）"""
    return HOLDERS_CACHE.get(period, {}).get(ts_code, [])


def query_single_etf(
    ts_code: str,
    etf_name: str,
    period: str,
    key_word_ratio: dict,
) -> list:
    """单只 ETF 单报告期业务处理：从缓存读取原始记录 → 筛选关键词"""
    raw_holders = get_etf_holders(ts_code, period)
    match_list = []

    for row in raw_holders:
        holder_name = row["holder_name"]
        match_ratio = None
        # 业务筛选逻辑
        for keyword, ratio in key_word_ratio.items():
            if keyword in holder_name:
                match_ratio = ratio
                break
        if match_ratio is not None:
            match_list.append({
                "ts_code": ts_code,
                "etf_name": etf_name,
                "rank": row.get("rank", 0),
                "holder_name": holder_name,
                "hold_amount": row["hold_amount"],
                "hold_ratio": row["hold_ratio"],
                "ratio": match_ratio,
            })
    return match_list


# ===================== ETF 日线行情（Tushare fund_daily） =====================

def _strip_suffix(code: str) -> str:
    """去掉 tushare 代码后缀：159001.SZ -> 159001"""
    return code.split(".")[0]


def resolve_ts_code(code: str, trade_date: str | None = None) -> str | None:
    """解析 ETF 的 tushare 代码：
    1) 优先取 etf_basic 中已回填的 ts_code（避免猜后缀）
    2) 若为空，按沪深两个市场枚举 .SH / .SZ，命中后回填缓存
    """
    info = BASIC_CACHE.get(code)
    if info and info.get("ts_code"):
        return info["ts_code"]

    for suffix in (".SH", ".SZ"):
        candidate = code + suffix
        try:
            df = pro.fund_daily(ts_code=candidate, trade_date=trade_date, fields="ts_code")
        except Exception:
            continue
        if df is not None and not df.empty:
            real = str(df["ts_code"].iloc[0])
            if info is not None:
                info["ts_code"] = real
                save_basic_cache()
            return real
    return None


def get_daily_prices(trade_date: str) -> dict:
    """获取指定交易日全市场 ETF 收盘价：{无后缀代码: close}。

    - 一次请求（fund_daily(trade_date=...)），不建缓存、不做限流
    - 顺带把返回的 tushare 代码回填到 etf_basic.ts_code 并保存
    - 非交易日/无数据返回空 dict
    """
    df = _call_with_retry(
        lambda: pro.fund_daily(trade_date=trade_date, fields="ts_code,close")
    )
    if df is None or df.empty:
        return {}

    price_map: dict = {}
    basic_updated = False
    for row_ts_code, close in zip(df["ts_code"], df["close"]):
        code = _strip_suffix(str(row_ts_code))
        price_map[code] = float(close)
        info = BASIC_CACHE.get(code)
        if info is not None and info.get("ts_code") != str(row_ts_code):
            info["ts_code"] = str(row_ts_code)
            basic_updated = True

    if basic_updated:
        save_basic_cache()
    return price_map


def get_etf_daily(code: str, start_date: str, end_date: str) -> list:
    """获取单只 ETF 一段历史的日线（备用/诊断用），返回 Tushare 原始记录列表。

    会先用 etf_basic.ts_code，未知时枚举 .SH/.SZ 解析。
    """
    ts_code = resolve_ts_code(code, trade_date=end_date)
    if not ts_code:
        print(f"⚠️ 无法解析 {code} 的 tushare 代码，可能不在场内市场")
        return []
    df = _call_with_retry(
        lambda: pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    )
    return [] if df is None or df.empty else df.to_dict("records")
