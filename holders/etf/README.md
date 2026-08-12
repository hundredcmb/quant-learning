# ETF 十大持有人模块

本目录为 A 股 ETF 的十大持有人分析模块。**ETF 十大持有人与基础信息全部手动维护、只从缓存读取**；**ETF 日线行情从 Tushare `fund_daily` 接口直接获取（至少需要 5000 积分），按交易日一次拉全市场，不建价格缓存、不做限流**。与股票模块（`../stock/`）严格区分。

## 目录说明

| 文件 | 说明 |
| --- | --- |
| `import_etf_data.py` | 从 Excel 导入 ETF 基础信息 + 十大持有人到缓存的脚本 |
| `etf_client.py` | 公共模块：只读持有人/基础信息缓存；日线直查（`fund_daily`）、`ts_code` 回填、后缀枚举 |
| `etf_top10_holders_value.py` | 单报告期关键词筛选 + 份额/市值统计（对标股票 value） |
| `etf_top10_holders_change.py` | 双报告期份额变动对比，生成表格图片（对标股票 change） |
| `etf_top10_holders_change_merged.py` | 同 change，另含按代码合并多席位统计（对标股票 change_merged） |
| `etf_top10_return_between_dates.py` | 两个交易日公允价值变动 + 收益率，生成表格/汇总图（对标股票 return_between_dates） |
| `etf_top10_holders_raw.json` | 持有人缓存（结构与股票缓存一致） |
| `etf_basic.json` | ETF 基础信息缓存（代码、名称、成立日） |
| `etf_data_example.xlsx` | 本地 Excel 数据源（**不入库**，由 .gitignore 忽略） |

## 日线行情获取

- 接口：Tushare `fund_daily`（ETF 日线行情），**至少需要 5000 积分**，8000 积分频次更高
- 按 `trade_date` **一次请求拉全市场** ETF 日线（实测 ~1800-2100 只，单次最多 5000 行），**不建缓存、不做限流**；也可按 `ts_code` 或日期区间获取单只历史（备用）
- `etf_client.get_daily_prices(trade_date)` 返回 `{无后缀代码: close}`，并顺带把返回的 tushare 代码回填到 `etf_basic.ts_code`
- 需要 tushare 代码时优先用 `etf_basic.ts_code`；为空时按沪深两个市场枚举后缀（`.SH` / `.SZ`）解析（`resolve_ts_code`）

## 复权因子修正（return_between_dates）

- 接口：Tushare `fund_adj`（ETF 复权因子），2000 积分可调、5000 积分以上频次更高
- 背景：`return_between_dates` 只用报告期披露的持有份额，若两个交易日之间发生**份额折算/送转/分红**，日2价格会被机械地压低（或抬高），但缓存里没有新份额，直接计算会导致市值和收益率失真；其他 change 类脚本有双报告期份额，不受影响
- 规则：按 `trade_date` 各拉一次全市场复权因子（`get_adj_factors`），把日2价格修正到日1相同复权系数水平：
  `修正后日2价 = 原始日2价 × F(日2) / F(日1)`，日1市值不变；无复权事件时 F 相等，结果与旧口径一致
- 标注：发生分红/份额折算的 ETF 在名称后以 `＊` 标注，图片与控制台均带图例说明；缺失因子时回退为不修正（比例=1）

## 标的级精细化比例覆盖（SPECIFIC_RATIO）

`KEY_WORD_RATIO` 是“关键词 → 折算比例”的全局默认；如需对**个别标的**手工调整比例，可在 `etf_client.SPECIFIC_RATIO` 中配置（仅当席位先命中关键词后生效）：

```python
SPECIFIC_RATIO = {
    # 该标的所有关键词统一覆盖为 0.5（"*" 表示全部）
    "512930": {"*": 0.5},
    # 只覆盖单个关键词（比 "*" 更精确，优先于 "*"）
    "159915": {"*": 0.8, "新华资管": 0.2},
}
```

- key 为**无后缀 ETF 代码**（与缓存 key 一致）；股票模块对应 `tushare_client.SPECIFIC_RATIO`，key 为带后缀 ts_code
- 匹配优先级：标的+关键词精确覆盖 > 标的全量 `*` > `KEY_WORD_RATIO` 默认
- 运行开始打印一行覆盖汇总（如 `标的特殊设定（共 2 个）：512930→全部 0.5；…`），表格图片中也会在“筛选关键词及折算比例”下方显示该提示；每条结果带 `ratio_source`（“标的覆盖”/“关键词默认”）留痕，正常输出不逐条展示
- 启动校验：覆盖中的具体关键词不在 `KEY_WORD_RATIO`、或标的代码不在缓存中时告警（防拼错）

## Excel 模板格式

单工作表，8 列：

| 列名 | 示例 | 说明 |
| --- | --- | --- |
| 证券代码 | 159001.OF | ETF 代码（6 位 + 交易所后缀），与股票代码严格区分 |
| 证券简称 | 易方达保证金A | ETF 名称 |
| 基金成立日 | 2013-03-29 | 成立日期 |
| 年度 | 2025-12-31 | 报告期（半年报 0630 / 年报 1231，一年两次） |
| 持有人排名 | 1 | 官方披露的第 N 大持有人 |
| 持有人名称 | 野村证券株式会社 | 持有人名称 |
| 持有份额 亿 | 0.005608 | 持有份额，单位**亿份** |
| 持有比例 % | 3.8 | 持有比例，单位 % |

## 导入

```powershell
C:\veighna_studio\python.exe holders\etf\import_etf_data.py
```

- 默认读取脚本顶部 `DEFAULT_EXCEL_FILE` 指定的 Excel；也可在命令行显式传路径
- 冲突策略：`--on-conflict overwrite`（覆盖旧数据）或 `keep`（保留旧数据，默认；也可改脚本顶部 `DEFAULT_ON_CONFLICT`）
- `--dry-run` 只解析和预演，不写文件

导入时持有份额自动从“亿份”换算为“份”（`hold_amount = 亿份 × 1e8`），持有比例保留 6 位小数以去掉 Excel 浮点噪声。

## 缓存结构

### 持有人缓存 `etf_top10_holders_raw.json`

与股票缓存 `../stock/tushare_top10_holders_raw.json` 结构完全一致：

```json
{
  "20251231": {
    "159001": [
      {"ts_code": "159001", "rank": 1, "holder_name": "...", "hold_amount": 560800, "hold_ratio": 3.8}
    ]
  }
}
```

记录字段 = 股票字段（`ts_code` / `holder_name` / `hold_amount` / `hold_ratio`）+ `rank`（Excel 模板的持有人排名，保留官方顺序信息）。`ts_code` 为**无后缀代码**（如 `159001`），与 tushare 代码的映射见基础信息缓存。

### 基础信息缓存 `etf_basic.json`

```json
{
  "159001": {
    "name": "易方达保证金A",
    "found_date": "2013-03-29",
    "import_code": "159001.OF",
    "ts_code": "159001.SZ"
  }
}
```

key 为**无后缀代码**（兼容导入格式与 tushare 代码后缀不同的情况）；`import_code` 是 Excel 导入格式代码（导入时更新），`ts_code` 是 tushare 代码（**拉取日线时由 `etf_client.get_daily_prices` 回填**，未知时枚举 `.SH`/`.SZ` 解析）。

## 更新流程

1. 把最新一期（半年报 / 年报）数据整理进 Excel，保持 8 列格式
2. 运行导入脚本：新报告期用默认 `keep` 追加；同一报告期修正数据用 `--on-conflict overwrite` 覆盖
3. 提交更新后的两个缓存 JSON（Excel 数据源不入库）

> 若缓存结构发生变更（如代码 key 去后缀），请**删除旧的缓存 JSON 后重新导入**，避免新旧格式条目残留。

## 注意事项

- ETF 与股票代码都是 6 位，但属于不同标的类型，处理数据时严禁混淆
- ETF 十大持有人一年只更新两次（半年报 + 年报），不要期待季度数据
- 缓存是手动数据的唯一存储，请勿删除；更新缓存后保持两个 JSON 文件同步提交
