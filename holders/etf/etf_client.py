"""ETF 十大持有人公共模块。

数据来源约定：
- ETF 十大持有人与基础信息：只从本地缓存读取（手动导入，不调用 Tushare）
- ETF 日线行情：从 Tushare `fund_daily` 直接获取（至少 5000 积分），
  按 `trade_date` 一次请求拉全市场，**不建价格缓存、不做限流**
- ETF 复权因子：从 Tushare `fund_adj` 直接获取（2000 积分可调，5000 积分以上频次更高），
  按 `trade_date` 一次请求拉全市场，用于把不同交易日价格修正到同一复权系数水平

代码兼容规则：
- 持有人缓存与基础信息缓存的 key 均为**无后缀代码**（如 `159001`）
- `etf_basic.json` 的 value 记录 `import_code`（Excel 导入格式代码，如 `159001.OF`，
  导入时更新）与 `ts_code`（tushare 代码，如 `159001.SZ`，拉取日线时回填）
- 需要 tushare 代码时**优先取 `etf_basic` 里的 `ts_code`**；若为空（尚未拉取过），
  按沪深两个市场枚举后缀（`.SH` / `.SZ`）解析

注意：ETF 与股票代码都是 6 位，但属于不同标的类型，严禁混淆。
"""
import datetime
import json
import os
import sys
import time

import tushare as ts
from tushare.pro.client import DataApi

# Windows 控制台编码兼容：避免 GBK 下 emoji 打印崩溃
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLDERS_CACHE_FILE = os.path.join(BASE_DIR, "etf_top10_holders_raw.json")
BASIC_CACHE_FILE = os.path.join(BASE_DIR, "etf_basic.json")

# 图片等运行产物统一输出到仓库根目录 output/（已在 .gitignore 中忽略）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 引入仓库根公共配置模块（须先把仓库根加入 sys.path）：token 与申万行业模块共享
_REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from config_store import config_path, resolve_token
from rate_limiter import (
    TIER_AT_LEAST_5000,
    TIER_BELOW_2000,
    InterfaceRateLimiter,
    is_rate_limit_error,
    probe_credit_tier,
)

# 接口限流：ETF 域必须 5000 积分档（官方约 500 次/分 -> 7.5 次/秒留 10% 余量，与申万一致）
# 对 fund_daily / fund_adj 各自独立节流、跨接口并行；init_tushare 会用公共积分档
# 探测强制确认该档位后才放行（见根 rate_limiter.py）
_limiter = InterfaceRateLimiter(7.5)

# 席位关键词-折算比例（示例默认值，按你的分析需要启用/调整；
# ETF 十大持有人常见 券商/资管/理财/基金/保险/汇金 等）
KEY_WORD_RATIO = {
    # =========T0国家队=========
    # "中央汇金投资": 0.0,
    # "中央汇金资产": 1.0,
    # "中国证券金融": 1.0,
    # "中证金融资产": 1.0,
    # "中国国新": 1.0,
    # "中国诚通": 1.0,
    # "中国信达资产": 1.0,
    # "中国东方资产": 1.0,
    # "中国长城资产": 1.0,

    # =========T0社保基金=========
    # "社保基金": 1.0,
    # "社会保障基金": 1.0,

    # =========T1平安险资=========
    "恒毅持盈": 1.0,
    "平安资管": 1.0,
    "平安人寿保险": 1.0,
    "平安养老保险": 1.0,
    "中国平安保险(集团)股份有限公司": 1.0,

    # =========T1国寿险资=========
    # "国丰兴华": 0.5,
    # "中国人寿保险股份": 1.0,
    # "中国人寿保险(集团)公司": 1.0,

    # =========T2新华险资=========
    # "国丰兴华": 0.5,
    # "新华资管": 1.0,
    # "新华养老": 1.0,
    # "新华人寿保险股份有限公司": 1.0,
    # "新华人寿保险股份有限公司-分红": 1.0,
    # "新华人寿保险股份有限公司-传统": 1.0,
    # "新华人寿保险股份有限公司-自有资金": 1.0,

    # =========T2太保险资=========
    # "太保致远": 1.0,
    # "太平洋人寿保险": 1.0,
    # "太平洋财产保险": 1.0,

    # =========T2人保险资=========
    # "启元惠众": 1.0,
    # "人民财产保险": 1.0,
    # "人民人寿保险": 1.0,
}
# ====================================================

# ===================== 标的级精细化比例覆盖 =====================
# 仅当席位先命中 KEY_WORD_RATIO 关键词后，再按标的代码覆盖该关键词的比例。
# 键为无后缀 ETF 代码（与缓存 key 一致）：
#   {"*": 比例}                     该标的所有关键词统一覆盖
#   {关键词: 比例}                  仅该关键词覆盖（优先于 "*"）
# 示例：
#   SPECIFIC_RATIO = {
#       "512930": {"*": 0.5},
#       "159915": {"*": 0.8, "新华资管": 0.2},
#   }
SPECIFIC_RATIO: dict = {}
# ====================================================

# ===================== 初始化Tushare接口（懒初始化） =====================
# token / pro 由 init_tushare() 赋值；各脚本启动时必须先调用一次
token: str = ""
pro: DataApi | None = None


# ===================== 披露闸门 =====================
# ETF 十大持有人一年只披露两期，法定截止日：半年报当年 8/31、年报次年 4/30
_ETF_PERIOD_MMDD = ("0630", "1231")
_DEADLINE_OFFSET_DAYS = {"0630": (0, 8, 31), "1231": (1, 4, 30)}   # (年偏移, 月, 日)


def assert_report_periods_disclosed(periods: list[str], *, _today: datetime.date | None = None) -> None:
    """启动闸门：报告期类型与披露进度双重校验，任一不满足即直接报错退出。

    - 类型白名单：ETF 十大持有人仅有半年报(*0630)与年报(*1231)两期
    - 截止日次日起该期才算可用——此前只有部分 ETF 完成手动录入/披露，
      此时查询会把大量「未录入」的空结果当正常数据处理，误导分析结论
    """
    today = _today or datetime.date.today()
    errors = []
    for p in periods:
        if len(p) != 8 or not p.isdigit() or p[4:] not in _ETF_PERIOD_MMDD:
            errors.append(f"报告期 {p} 非法（ETF 仅支持 *0630 / *1231 两类）")
            continue
        year_offset, month, day = _DEADLINE_OFFSET_DAYS[p[4:]]
        deadline = datetime.date(int(p[:4]) + year_offset, month, day)
        if today <= deadline:
            errors.append(f"报告期 {p} 未到法定披露截止 {deadline.isoformat()}（今天 {today.isoformat()}）")
    if not errors:
        return
    print("❌ 报告期配置存在无法使用或未到可用时点的项，本次运行终止：")
    for line in errors:
        print(f"   {line}")
    print("   请调整脚本顶部 REPORT_PERIOD 配置后重试")
    sys.exit(1)


def init_tushare(cli_token: str | None = None) -> DataApi:
    """初始化 Tushare 客户端，各脚本启动时必须先调用一次。

    token 解析优先级（见仓库根 config_store.resolve_token）：
    命令行 --token > 已保存配置，两者皆无时直接报错。
    初始化完成后用公共积分档探测确认 >=5000 档位（未达即报错退出，
    全部接口保持 7.5 次/秒节流）。
    """
    global token, pro
    token = resolve_token(cli_token)
    if not token:
        raise ValueError(
            "未获取到 Tushare token：请用命令行参数指定 --token <你的token>（传入后自动保存），"
            f"或先在配置文件 {config_path()} 中写入 tushare_token 字段"
        )
    pro = ts.pro_api(token=token)

    tier = probe_credit_tier(pro, min_tier_points=5000, limiter=_limiter)
    if tier is None:
        print("⚠️ 无法确认积分档位，本次跳过档位检查继续运行")
    elif tier != TIER_AT_LEAST_5000:
        points = "不足 2000" if tier == TIER_BELOW_2000 else "在 2000~5000 之间"
        print(f"❌ 当前 Tushare token 积分{points}，无权调用 fund_daily 接口")
        print("   ETF 十大持有人分析的日线行情获取至少需要 5000 积分，请到 tushare.pro 提升积分后重试")
        sys.exit(1)
    else:
        print(f"✅ 积分档位：{tier}，全部接口按 7.5/s 节流")
    return pro
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


def _call_with_retry(fn, api_name: str, retries: int = 3):
    """轻量重试：先按「接口独立」节流发射；瞬时错误指数退避重试；
    触发官方频次限制则该接口速率自动减半后重试；权限/参数类错误不重试直接抛。

    不同接口的限额各自独立（fund_daily / fund_adj 两桶并行互不等待）。
    """
    last_exc = None
    for attempt in range(retries):
        _limiter.acquire(api_name)
        try:
            return fn()
        except Exception as e:
            last_exc = e
            msg = str(e)
            if any(k in msg for k in ("权限", "积分", "抱歉")):
                raise
            if is_rate_limit_error(msg):
                _limiter.halve(api_name)
                print(f"⚠️ {api_name} 触发官方频次限制，该接口速率自动减半后重试")
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


def _sorted_keywords(key_word_ratio: dict) -> list:
    """按关键词长度降序排序（同长度保持配置顺序），实现最长关键词优先匹配"""
    order = {k: i for i, k in enumerate(key_word_ratio)}
    return sorted(key_word_ratio, key=lambda k: (-len(k), order[k]))


_checked_keyword_ids: set = set()


def _validate_keywords_once(key_word_ratio: dict) -> None:
    """启动校验（每个配置只检查一次）：检测互为子串的关键词并告警，避免权重歧义"""
    ratio_id = id(key_word_ratio)
    if ratio_id in _checked_keyword_ids:
        return
    _checked_keyword_ids.add(ratio_id)

    keys = list(key_word_ratio)
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if k1 in k2 or k2 in k1:
                shorter, longer = (k1, k2) if len(k1) < len(k2) else (k2, k1)
                print(f"⚠️ KEY_WORD_RATIO 存在子串关键词：{shorter} 是 {longer} 的子串，"
                      f"匹配时将按最长关键词「{longer}」优先（权重 {key_word_ratio[longer]}）")


_checked_specific_ids: set = set()


def _validate_specific_ratio_once(key_word_ratio: dict, specific_ratio: dict) -> None:
    """标的级覆盖校验（每个配置只检查一次）：关键词有效性、标的存在性，并打印覆盖汇总"""
    config_id = (id(key_word_ratio), id(specific_ratio))
    if config_id in _checked_specific_ids:
        return
    _checked_specific_ids.add(config_id)

    if not specific_ratio:
        return

    # 关键词有效性：覆盖中的具体关键词必须存在于 KEY_WORD_RATIO（"*" 表示全部，恒合法）
    for code, overrides in specific_ratio.items():
        for keyword in overrides:
            if keyword != "*" and keyword not in key_word_ratio:
                print(f"⚠️ SPECIFIC_RATIO 关键词 {keyword} 不在 KEY_WORD_RATIO 中（{code}），该覆盖不会生效")

    # 标的存在性：检查基础信息缓存（软告警，防拼错）
    for code in specific_ratio:
        if code not in BASIC_CACHE:
            print(f"⚠️ SPECIFIC_RATIO 标的 {code} 不在 ETF 基础信息缓存中，请检查代码是否拼错")

    # 覆盖汇总（简化显示：一行）
    summary = format_specific_ratio_summary(specific_ratio)
    if summary:
        print(summary)


def format_specific_ratio_summary(specific_ratio: dict | None = None) -> str:
    """生成标的特殊设定汇总文本（无覆盖时返回空串），用于控制台与图片提示"""
    if specific_ratio is None:
        specific_ratio = SPECIFIC_RATIO
    if not specific_ratio:
        return ""

    parts = []
    for code, overrides in specific_ratio.items():
        if set(overrides) == {"*"}:
            parts.append(f"{code}→全部 {overrides['*']}")
        else:
            keyword_parts = []
            if "*" in overrides:
                keyword_parts.append(f"全部 {overrides['*']}")
            for keyword, value in overrides.items():
                if keyword != "*":
                    keyword_parts.append(f"{keyword} {value}")
            parts.append(f"{code}→{'、'.join(keyword_parts)}")
    return f"标的特殊设定（共 {len(specific_ratio)} 个）：{'；'.join(parts)}"


def _resolve_ratio(key_word_ratio: dict, specific_ratio: dict, ts_code: str, match_keyword: str):
    """解析最终比例：标的+关键词精确覆盖 > 标的全量(*) > 关键词默认"""
    overrides = (specific_ratio or {}).get(ts_code)
    if overrides:
        if match_keyword in overrides:
            return overrides[match_keyword], "标的覆盖"
        if "*" in overrides:
            return overrides["*"], "标的覆盖"
    return key_word_ratio[match_keyword], "关键词默认"


def query_single_etf(
    ts_code: str,
    etf_name: str,
    period: str,
    key_word_ratio: dict,
    specific_ratio: dict | None = None,
) -> list:
    """单只 ETF 单报告期业务处理：从缓存读取原始记录 → 筛选关键词（最长关键词优先）

    specific_ratio：标的级精细化覆盖（{代码: {"*" 或 关键词: 比例}}），
    仅当席位命中关键词后生效；未传时使用模块级 SPECIFIC_RATIO。
    """
    if specific_ratio is None:
        specific_ratio = SPECIFIC_RATIO
    _validate_keywords_once(key_word_ratio)
    _validate_specific_ratio_once(key_word_ratio, specific_ratio)
    sorted_keywords = _sorted_keywords(key_word_ratio)
    raw_holders = get_etf_holders(ts_code, period)
    match_list = []

    for row in raw_holders:
        holder_name = row["holder_name"]
        match_keyword = None
        # 业务筛选逻辑：最长关键词优先，避免子串歧义
        for keyword in sorted_keywords:
            if keyword in holder_name:
                match_keyword = keyword
                break
        if match_keyword is not None:
            final_ratio, ratio_source = _resolve_ratio(key_word_ratio, specific_ratio, ts_code, match_keyword)
            match_list.append({
                "ts_code": ts_code,
                "etf_name": etf_name,
                "rank": row.get("rank", 0),
                "holder_name": holder_name,
                "hold_amount": row["hold_amount"],
                "hold_ratio": row["hold_ratio"],
                "ratio": final_ratio,
                "match_keyword": match_keyword,
                "ratio_source": ratio_source,
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
            df = _call_with_retry(lambda c=candidate: pro.fund_daily(
                ts_code=c, trade_date=trade_date, fields="ts_code"), api_name="fund_daily")
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
        lambda: pro.fund_daily(trade_date=trade_date, fields="ts_code,close"),
        api_name="fund_daily",
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


def get_adj_factors(trade_date: str) -> dict:
    """获取指定交易日全市场 ETF 复权因子：{无后缀代码: adj_factor}。

    - 按 trade_date 一次请求拉全市场（fund_adj），单次上限 2000 行，超限自动翻页
    - 不建缓存、不做限流；非交易日/无数据返回空 dict
    - 复权因子随份额折算/分红变化，用于把不同交易日价格修正到同一系数水平
    """
    factors: dict = {}
    offset = 0
    for _ in range(10):  # 翻页保护：最多 10 页
        df = _call_with_retry(
            lambda o=offset: pro.fund_adj(trade_date=trade_date, offset=o, fields="ts_code,adj_factor"),
            api_name="fund_adj",
        )
        if df is None or df.empty:
            break
        for row_ts_code, adj in zip(df["ts_code"], df["adj_factor"]):
            factors[_strip_suffix(str(row_ts_code))] = float(adj)
        if len(df) < 2000:
            break
        offset += len(df)
    return factors


def get_etf_daily(code: str, start_date: str, end_date: str) -> list:
    """获取单只 ETF 一段历史的日线（备用/诊断用），返回 Tushare 原始记录列表。

    会先用 etf_basic.ts_code，未知时枚举 .SH/.SZ 解析。
    """
    ts_code = resolve_ts_code(code, trade_date=end_date)
    if not ts_code:
        print(f"⚠️ 无法解析 {code} 的 tushare 代码，可能不在场内市场")
        return []
    df = _call_with_retry(
        lambda: pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date),
        api_name="fund_daily",
    )
    return [] if df is None or df.empty else df.to_dict("records")
