# AGENTS.md

本文件是给 AI 编码代理（如 Codex）在本仓库工作时看的项目说明与约定。

## 项目简介

quant-learning 是一个 A 股量化学习项目（“量化小白从零开始学习量化”），目前包含四块内容：

- `holders/`：基于 Tushare 的十大股东席位关键词分析（国家队、社保、险资等），含多报告期对比与持仓公允价值变动统计
- `shenwan_industry/`：申万 2021 三级行业分类树，以及单日行业涨幅榜（等权 / 流通市值加权）
- `vnpy_examples/`：vnpy 学习示例（配置、K 线入库、数据服务下载、图表、指标、日线回测策略）
- `database/`：SQLAlchemy + MySQL 的会话封装

## 运行环境

- Python 3.10+（代码使用 `X | None`、`dict[str, ...]` 等新语法）
- 必须使用 vnpy 客户端（veighna studio）自带的 Python 运行；vnpy / vnpy_tushare / vnpy_ctastrategy / tushare / pandas / numpy / Pillow / TA-Lib 等由客户端环境提供，不要用 pip 单独安装；[requirements.txt](requirements.txt) 只包含项目自身依赖（pymysql、sqlalchemy）
- 图形界面（GUI）开发优先使用 vnpy 自带环境：客户端环境已内置 PySide6（vnpy 4.1.0 对应 PySide6 6.8）与 qdarkstyle，直接 `from PySide6.QtWidgets import ...` 开发窗口，不要额外安装 PyQt / PySide 等 GUI 依赖；需要与 vnpy 风格一致时优先复用 `vnpy.trader.ui` 与 `vnpy.chart` 的现成组件
- 项目已**弃用 `.env` 环境变量**，所有配置统一从 vnpy 全局配置 `~/.vntrader/vt_setting.json` 动态读取：
  - `datafeed.password` 存放 Tushare token，`datafeed.name` / `database.name` 决定数据源与数据库类型；`holders/` 和申万示例都从这里读 token
  - 数据库连接（`database.user` / `database.password` / `database.host` / `database.port` / `database.database`）同样从 vnpy 配置动态获取，`database/session.py` 据此构建 SQLAlchemy 连接串
- 脚本运行需要联网访问 Tushare Pro API，且 token 需开通对应接口权限（如 `top10_holders`）

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `config.py` | 仅提供全局 `logger`（配置统一从 vnpy `SETTINGS` 动态获取，无环境变量） |
| `database/session.py` | SQLAlchemy 引擎与 `db_session` 上下文管理器（自动提交/回滚/关闭），另有 FastAPI 风格 `get_db` |
| `holders/stock/tushare_client.py` | 股票公共模块：Tushare token、原始数据缓存、限流、指数成分股、收盘价、单报告期关键词筛选、并发查询 |
| `holders/stock/top10_holders_value.py` | 基础版：按关键词筛选十大股东席位，计算报告期持仓市值（原始/折算，单位亿元），仅控制台输出 |
| `holders/stock/top10_holders_change.py` | 双报告期对比：`REPORT_PERIOD1 -> REPORT_PERIOD2` 的持股变动统计，生成表格图片 |
| `holders/stock/top10_holders_change_merged.py` | 同 top10_holders_change，另含 `merge_holders_by_stock`（同一股票多个匹配席位合并统计） |
| `holders/stock/top10_return_between_dates.py` | 单报告期、两个交易日（`REPORT_TRADE_DATE` vs `NEW_TRADE_DATE`）的公允价值变动与收益率，生成完整表格 + 汇总两张 PNG |
| `holders/stock/tushare_top10_holders_raw.json` | Tushare 原始数据缓存（约 8 MB，已提交进仓库，请勿删除；覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后） |
| `holders/etf/import_etf_data.py` | ETF 数据导入脚本：从 Excel 导入 ETF 基础信息与十大持有人到 `holders/etf/etf_top10_holders_raw.json` 和 `holders/etf/etf_basic.json`，支持冲突策略 `--on-conflict overwrite/keep` |
| `holders/etf/etf_client.py` | ETF 公共模块：只读持有人/基础信息缓存；日线直查（`fund_daily` 按交易日一次拉全市场）、`ts_code` 回填、未知后缀枚举 `.SH`/`.SZ` |
| `holders/etf/etf_top10_holders_value.py` | 单报告期关键词筛选 + 份额/市值统计（对标股票 value） |
| `holders/etf/etf_top10_holders_change.py` | 双报告期份额变动对比，生成表格图片（对标股票 change） |
| `holders/etf/etf_top10_holders_change_merged.py` | 同 change，另含按代码合并多席位统计（对标股票 change_merged） |
| `holders/etf/etf_top10_return_between_dates.py` | 两个交易日公允价值变动 + 收益率，生成表格/汇总图（对标股票 return_between_dates） |
| `holders/etf/etf_top10_holders_raw.json` | ETF 持有人缓存（结构与股票缓存一致） |
| `holders/etf/etf_basic.json` | ETF 基础信息缓存（代码、名称、成立日） |
| `holders/etf/README.md` | ETF 模块说明：Excel 导入格式、缓存结构、更新流程 |
| `holders/etf/etf_data_example.xlsx` | 本地 Excel 数据源（已被 gitignore，不入库） |
| `output/` | 图片运行产物目录（已被 `.gitignore` 忽略） |
| `skills/` | Tushare 官方 skills（已克隆到本地，含 `SKILL.md` 与完整接口文档 `references/数据接口.md`；已被 gitignore，不随仓库提交） |
| `shenwan_industry/classification.py` | `ShenWanIndustryTree`：行业树构建、成分股加载、等权/流通市值加权涨幅排名 |
| `shenwan_industry/SW2021.json` | 申万 2021 行业分类本地数据（推荐的数据源） |
| `vnpy_examples/` | vnpy 学习示例目录（配置、数据、图表、指标、回测等），按编号顺序学习 |

## 常用命令

```bash
# 十大股东分析（token 在 vnpy 的 datafeed.password 中配置）
python holders/stock/top10_holders_value.py
python holders/stock/top10_holders_change.py
python holders/stock/top10_holders_change_merged.py
python holders/stock/top10_return_between_dates.py

# ETF 十大持有人（数据源为手动导入缓存，日线从 fund_daily 拉取）
python holders/etf/import_etf_data.py
python holders/etf/etf_top10_holders_value.py
python holders/etf/etf_top10_holders_change.py
python holders/etf/etf_top10_holders_change_merged.py
python holders/etf/etf_top10_return_between_dates.py

# 申万行业涨幅示例
python shenwan_industry/classification.py

# vnpy 示例（需先配置好数据库与 tushare）
python vnpy_examples/01_settings.py
python vnpy_examples/02_bardata.py
python vnpy_examples/03_datafeed.py
python vnpy_examples/05_indicator.py
python vnpy_examples/06_ma_strategy.py
```

## 代码约定

- 注释、文档字符串、日志和输出均为中文；脚本通过 `if __name__ == "__main__":` 直接运行
- 脚本型代码把可调参数集中放在文件顶部“核心配置”区，注释掉的分支表示备选方案
- 日期统一为 `YYYYMMDD` 字符串；股票代码统一为 Tushare 格式（如 `600036.SH`）
- 持仓市值计算：`市值(亿) = hold_amount * close / 1e8`，折算市值 = 市值 × 席位折算比例
- 类型注解尽量完整；遇到 PyPI 包缺失，先确认是否由 vnpy 客户端环境提供，只有项目自身依赖才加入 requirements.txt

## 注意事项

### 十大股东分析（holders/）

- 公共数据获取逻辑（token、缓存、限流、指数成分股、收盘价、并发查询）已抽到 `holders/stock/tushare_client.py`，四个脚本只保留各自的业务配置与逻辑；修改公共逻辑只改 `tushare_client.py` 一处即可
- `KEY_WORD_RATIO` 是“席位关键词 -> 折算比例”，按 T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组，统一在 `tushare_client.py` 中配置（改一处即可）；如需某脚本单独调整，可在该脚本 import 后重新定义覆盖
- 生成的图片输出到仓库根目录 `output/`（已 gitignore）；缓存文件保持在 `holders/stock/tushare_top10_holders_raw.json`（已提交进仓库，请勿删除），缓存结构为 `{报告期: {股票代码: [Tushare 原始记录列表]}}`，仅存接口原始数据、不含业务处理；修改业务逻辑时优先复用缓存，不要改变该结构
- 十大股东接口（如 `top10_holders`）至少需要 2000 Tushare 积分才有权限调用；低于 2000 积分时没有任何接口权限，只能使用仓库缓存分析其中已包含的数据（覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后）
- 接口失败时脚本会 `save_raw_cache()` 后 `os._exit(-1)` 退出；Tushare 有限流，默认 `MAX_REQUESTS_PER_MINUTE=180`（建议比官方限制低 20）、`MAX_WORKERS=5`（上限 20）
- 缓存 JSON 约 8 MB 且已提交进仓库，避免无谓地让缓存文件进一步膨胀

### 股票与 ETF 十大持有人（严格区分）

- **数据来源不同**：A 股股票的十大持有人可通过 Tushare 接口获取（也可以查询缓存）；**A 股 ETF 的十大持有人无法从 Tushare 获取**，只能手动录入到缓存中
- **代码格式相同但概念不同**：股票和 ETF 都是六位代码，但属于不同标的类型，开发和处理数据时不要混淆，务必严格区分股票与 ETF
- **更新周期不同**：A 股 ETF 的十大持有人一年只更新两次（半年报 + 年报）；股票一年四个财报期都会公布
- **数据来源约定**：ETF 十大持有人与基础信息（代码、名称、成立日）仍只从缓存读取（手动导入，不调用 Tushare 的持有人/基础信息接口）；**ETF 日线行情从 Tushare `fund_daily` 直接获取**（**至少需要 5000 积分**，低于 5000 积分无法拉取），按 `trade_date` 一次请求拉全市场，**不建价格缓存、不做限流**
- **ETF 数据缓存文件**：持有人缓存 `holders/etf/etf_top10_holders_raw.json`（结构与股票缓存完全一致：`{报告期: {代码: [{ts_code, holder_name, hold_amount(份), hold_ratio(%), rank}]}}`，rank 是 Excel 模板额外的排名字段，`ts_code` 为**无后缀代码**）；基础信息缓存 `holders/etf/etf_basic.json`（key 为**无后缀代码**，value 为 `{name, found_date, import_code(导入格式代码，导入时更新), ts_code(tushare 代码，拉取日线时回填)}`）；均由 `holders/etf/import_etf_data.py` 从 Excel 导入维护，`hold_amount` 已统一为“份”（Excel 中为“亿份”）。日线查询、ts_code 回填与未知后缀枚举（`.SH`/`.SZ`）由 `etf_client.py` 提供（`get_daily_prices` / `resolve_ts_code`）

### Tushare 数据获取

- 本仓库已克隆 Tushare 官方 skills 到 `skills/`（已被 `.gitignore` 忽略，不随仓库提交）。开发中需要获取 Tushare 数据时，**优先查阅** `skills/tushare/references/数据接口.md`（或 `skills/tushare-data/` 版本）确认接口名、必填/可选参数、返回字段与积分/频率限制，确保参数和结果解析正确，不要仅凭记忆硬写字段名
- `fund_daily`（ETF 日线行情）的 `ts_code` 与 `trade_date` 均为可选参数：**支持像股票 `daily` 一样按 `trade_date` 获取全市场 ETF 日线**（单次最多 5000 行，场内 ETF 数量足够一次拉取），也支持按 `ts_code` 或 `start_date/end_date` 区间获取单只历史
- Tushare token **统一通过 vnpy 接口动态获取**：`SETTINGS["datafeed.password"]`（写法见 holders 脚本），不要在代码中硬编码 token，也不要从其他环境变量读取

### 申万行业（shenwan_industry/）

- 行业树优先用本地 `SW2021.json` 构建（`build_industries()`），tushare 版本 `build_industries_by_tushare()` 仅作备用
- 流通市值加权算法对停牌股票做了特殊处理（回退查询停牌前最近流通市值），改动时不要破坏该逻辑
- **自建申万行业指数是项目未来核心工作**（官方指数不稳定且种类少）；历史成分缓存与指数构建规划见 `shenwan_industry/AGENTS.md`「未来规划」节
- 本模块的算法权威描述与强制核对流程见 `shenwan_industry/AGENTS.md`；涉及申万行业的任务在完成通知用户前，必须先对照该文件核对算法一致性

### vnpy 示例（vnpy_examples/）

- 按编号顺序学习即可，具体功能与参数说明见各脚本内注释

### 环境与 Git

- 本机是 Windows，PIL 图片默认用 `msyh.ttc` 字体（代码已做跨平台兜底）；PowerShell 读写中文文件时注意 UTF-8 编码
- `.gitignore` 已忽略 `.idea/` 与 `output/`（运行产物图片）；缓存 JSON 位于 `holders/` 且随仓库提交，不要忽略；新增生成文件时先确认是否应提交，`~/.vntrader/vt_setting.json` 中的真实 token / 数据库信息严禁提交
- 提交信息使用中文 Conventional Commits 风格（如 `feat:`、`fix:`），单行主题、简洁描述；开发分支建议使用 `codex/` 前缀
