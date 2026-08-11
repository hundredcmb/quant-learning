# ETF 十大持有人模块

本目录为 A 股 ETF 的十大持有人分析模块。**ETF 相关数据全部手动维护，不从 Tushare 获取任何数据**（包括 ETF 基础信息），与股票模块（`../stock/`）严格区分。

## 目录说明

| 文件 | 说明 |
| --- | --- |
| `import_etf_data.py` | 从 Excel 导入 ETF 基础信息 + 十大持有人到缓存的脚本 |
| `etf_top10_holders_raw.json` | 持有人缓存（结构与股票缓存一致） |
| `etf_basic.json` | ETF 基础信息缓存（代码、名称、成立日） |
| `etf_data_example.xlsx` | 本地 Excel 数据源（**不入库**，由 .gitignore 忽略） |

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
    "159001.OF": [
      {"ts_code": "159001.OF", "rank": 1, "holder_name": "...", "hold_amount": 560800, "hold_ratio": 3.8}
    ]
  }
}
```

记录字段 = 股票字段（`ts_code` / `holder_name` / `hold_amount` / `hold_ratio`）+ `rank`（Excel 模板的持有人排名，保留官方顺序信息）。

### 基础信息缓存 `etf_basic.json`

```json
{
  "159001.OF": {"name": "易方达保证金A", "found_date": "2013-03-29"}
}
```

## 更新流程

1. 把最新一期（半年报 / 年报）数据整理进 Excel，保持 8 列格式
2. 运行导入脚本：新报告期用默认 `keep` 追加；同一报告期修正数据用 `--on-conflict overwrite` 覆盖
3. 提交更新后的两个缓存 JSON（Excel 数据源不入库）

## 注意事项

- ETF 与股票代码都是 6 位，但属于不同标的类型，处理数据时严禁混淆
- ETF 十大持有人一年只更新两次（半年报 + 年报），不要期待季度数据
- 缓存是手动数据的唯一存储，请勿删除；更新缓存后保持两个 JSON 文件同步提交
