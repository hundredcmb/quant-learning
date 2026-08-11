# quant-learning

量化小白从零开始学习量化，主要用于 A 股市场。

本项目是一套围绕 A 股量化学习整理的、可直接运行的 Python 脚本，覆盖：

- **十大股东席位分析**（`holders/`）：基于 Tushare 数据，在中证 800 + 中证 1000 样本池内按席位关键词（国家队、社保、险资等）筛选十大股东持仓，支持多报告期对比、公允价值变动与收益率统计，并输出控制台报表和图片
- **申万行业分析**（`shenwan_industry/`）：申万 2021 三级行业分类树，计算单日行业涨幅榜（等权 / 流通市值加权）
- **vnpy 入门示例**（`vnpy_examples/`）：从配置、K 线入库、数据服务下载，到图表绘制、技术指标计算和日线双均线回测策略
- **数据库封装**（`database/`）：SQLAlchemy + MySQL 的会话管理

## 运行前提（必须）

本项目依赖 vnpy 完整环境（vnpy、vnpy_tushare、vnpy_ctastrategy、tushare、pandas、numpy、Pillow、TA-Lib 等），**必须安装 vnpy 客户端（veighna studio），并使用其自带的 Python 运行**。不要用系统 Python 单独 pip 安装 vnpy 相关依赖。

- vnpy 客户端下载：https://www.vnpy.com/
- 客户端安装后自带 Python 3.13 和完整 vnpy 环境；Windows 默认安装目录为 `C:\veighna_studio`，自带 Python 位于 `C:\veighna_studio\python.exe`

## 目录结构

```text
quant-learning/
├── config.py                  # 仅提供全局 logger（配置统一从 vnpy 全局配置读取）
├── requirements.txt           # 项目自身依赖（vnpy 相关库由客户端提供）
├── database/
│   └── session.py             # SQLAlchemy 引擎与 db_session 上下文管理器
├── holders/
│   ├── stock/                          # 股票十大股东
│   │   ├── tushare_client.py           # 公共模块：Tushare 客户端、缓存、限流、并发查询
│   │   ├── top10_holders_value.py      # 十大股东关键词筛选 + 持仓市值统计（单报告期）
│   │   ├── top10_holders_change.py     # 双报告期持股变动对比
│   │   ├── top10_holders_change_merged.py  # 双报告期持股变动（同股多席位合并统计）
│   │   ├── top10_return_between_dates.py   # 两交易日公允价值变动 + 收益率，生成图片报表
│   │   └── tushare_top10_holders_raw.json  # Tushare 原始数据缓存（随仓库提交）
│   └── etf/                            # ETF 十大持有人
│       ├── import_etf_data.py          # 从 Excel 导入 ETF 基础信息 + 十大持有人
│       ├── etf_top10_holders_raw.json  # 持有人缓存（结构与股票缓存一致）
│       └── etf_basic.json              # ETF 基础信息缓存（代码、名称、成立日）
├── output/                    # 运行产物（生成的图片，已 gitignore）
├── shenwan_industry/
│   ├── classification.py      # 申万行业树构建与行业涨幅排名
│   └── SW2021.json            # 申万 2021 行业分类本地数据
├── skills/                    # Tushare 官方 skills（接口文档，供 AI Agent 查阅；已 gitignore）
└── vnpy_examples/
    ├── 01_settings.py         # 查看 vnpy 配置，初始化数据库 / 数据服务
    ├── 02_bardata.py          # BarData 写入 / 读取 vnpy 数据库
    ├── 03_datafeed.py         # 数据服务下载 K 线入库（含沪深 300 批量导入）
    ├── 04_chart.py            # 图表组件绘制 K 线 + 成交量（需 GUI）
    ├── 05_indicator.py        # KDJ / MACD / BBI 指标计算
    └── 06_ma_strategy.py      # 不做空的日线双均线回测策略
```

## 快速开始

### 1. 安装 vnpy 客户端

下载并安装 veighna studio，然后确认自带 Python 可用：

```powershell
C:\veighna_studio\python.exe --version
```

### 2. 安装项目自身依赖

使用 vnpy 自带 Python 在项目根目录安装 `requirements.txt`（只包含项目自身依赖）：

```powershell
cd C:\Users\LSY\Desktop\lsy_projects\quant-learning
C:\veighna_studio\python.exe -m pip install -r requirements.txt
```

### 3. 配置 Tushare token 与数据库（vt_setting.json）

项目通过 vnpy 全局配置读取 Tushare token 和数据库连接。配置文件位于 `~/.vntrader/vt_setting.json`（Windows 下即 `C:\Users\<用户名>\.vntrader\vt_setting.json`），首次运行 vnpy 客户端时会自动生成；也可以直接在客户端界面中修改。

配置示例（**把 `<>` 中的内容替换为你自己的 token 和数据库信息，切勿把真实信息提交到 git**）：

```json
{
    "font.family": "微软雅黑",
    "font.size": 12,
    "log.active": true,
    "log.level": 50,
    "log.console": true,
    "log.file": true,
    "email.server": "smtp.qq.com",
    "email.port": 465,
    "email.username": "",
    "email.password": "",
    "email.sender": "",
    "email.receiver": "",
    "datafeed.name": "tushare",
    "datafeed.username": "token",
    "datafeed.password": "<你的Tushare token>",
    "database.timezone": "Asia/Shanghai",
    "database.name": "mysql",
    "database.database": "<数据库名>",
    "database.host": "<数据库地址>",
    "database.port": 3306,
    "database.user": "<数据库用户名>",
    "database.password": "<数据库密码>"
}
```

说明：

- `datafeed.*`：数据服务配置。`name` 固定为 `tushare`，`password` 填你的 Tushare token（在 [tushare.pro](https://tushare.pro) 注册后获取，并需开通 `top10_holders`、`index_weight`、`stock_basic`、`daily`、`index_classify`、`index_member_all`、`daily_basic` 等接口权限）；`holders/` 和申万示例都从这里读取 token
- `database.*`：数据库连接配置，同时供两处使用——vnpy 示例读写 K 线（`vnpy_examples/02、03、05、06` 通过 `get_database()`），以及 `database/session.py`（SQLAlchemy 据此动态构建连接串）
- 修改配置前请先关闭 vnpy 客户端，避免配置被覆盖

### 4. 运行脚本

所有脚本都用 vnpy 自带 Python 运行，例如：

```powershell
C:\veighna_studio\python.exe holders\top10_holders_value.py
```

## 使用 AI Agent 开发

如果使用 AI 编码代理（如 Codex 等）在本仓库进行开发，推荐安装 Tushare 官方提供的 skills：

```bash
git clone https://github.com/waditu-tushare/skills.git skills
```

skills 中包含完整的数据接口文档（`skills/tushare/references/数据接口.md`，覆盖 235+ 个 Tushare API）。开发中涉及 Tushare 数据获取时，应**优先查阅该文档**确认接口参数、返回字段与积分 / 权限要求，确保参数与结果解析正确。本仓库已克隆好该 skills（`skills/` 目录），它属于第三方仓库，已被 `.gitignore` 忽略，不随本项目提交。

## 使用说明

### 1. 十大股东席位分析

在指定样本池（默认中证 800 + 中证 1000）中，按 `KEY_WORD_RATIO` 配置的席位关键词筛选十大股东，并按折算比例估算持仓市值（单位：亿元）。

> **Tushare 积分要求**：十大股东相关接口（如 `top10_holders`）有积分门槛，**至少需要 2000 积分才有权限调用**。积分低于 2000 时**没有任何接口权限**，只能使用仓库自带的缓存文件 `holders/stock/tushare_top10_holders_raw.json` 分析缓存中已包含的数据。该缓存针对样本池 **中证 800 + 中证 1000 成分股**（约 1800 只），完整覆盖 **2025 年年报（`20251231`）及以后**（如 `20260331`）。

| 脚本 | 功能 | 输出 |
| --- | --- | --- |
| `holders/stock/top10_holders_value.py` | 单报告期关键词筛选，统计原始 / 折算持仓 | 控制台表格 |
| `holders/stock/top10_holders_change.py` | 双报告期（`REPORT_PERIOD1` → `REPORT_PERIOD2`）持股变动对比，标记新增 / 增持 / 减持 / 不变 / 退出 | 控制台表格 + `output/持股变动表格.png` |
| `holders/stock/top10_holders_change_merged.py` | 同 top10_holders_change，另将同一股票多个匹配席位合并统计 | 控制台表格 + `output/持股变动表格.png` |
| `holders/stock/top10_return_between_dates.py` | 同一报告期、两个交易日间的公允价值变动与收益率 | 控制台表格 + `output/股票组合收益统计_*_to_*.png`（含汇总版） |

运行前可在脚本顶部“核心配置”区修改样本池指数、报告期、交易日和关键词。公共数据获取逻辑（token、缓存、限流、指数成分股、收盘价、并发查询）已抽到 `holders/stock/tushare_client.py`，四个脚本只保留各自的业务配置与逻辑。生成的图片统一输出到 `output/` 目录（已 gitignore）；Tushare 原始数据缓存仍保存在 `holders/stock/tushare_top10_holders_raw.json`（随仓库提交，请勿删除，全量重新拉取受限流影响很慢）。

### 2. 申万行业涨幅

```powershell
C:\veighna_studio\python.exe shenwan_industry/classification.py
```

示例默认计算指定日期的申万一级 / 二级 / 三级行业涨幅榜（等权与流通市值加权两种口径），行业树从本地 `SW2021.json` 构建。

### 3. vnpy 示例

示例按学习路径编号，建议按顺序运行：

```powershell
C:\veighna_studio\python.exe vnpy_examples\01_settings.py
C:\veighna_studio\python.exe vnpy_examples\02_bardata.py
C:\veighna_studio\python.exe vnpy_examples\03_datafeed.py
C:\veighna_studio\python.exe vnpy_examples\05_indicator.py
C:\veighna_studio\python.exe vnpy_examples\06_ma_strategy.py
```

注意：

- `04_chart.py` 会启动 Qt 图形界面，需在带桌面的 Windows 环境下运行
- `06_ma_strategy.py` 的 `skip_ex` 参数：`0` = 忽略分红、使用不复权价格回测（走本地数据库，支持 vnpy 参数优化）；`1` = 每次分红前逃权（每次回测调用 Tushare，不支持参数优化）

## 重要说明

- **Tushare 限流**：脚本内置每分钟请求数限制（默认 180）和线程并发控制（默认 5，上限 20）。接口失败时会先保存缓存再退出，请根据账号权限调整 `MAX_REQUESTS_PER_MINUTE` 和 `MAX_WORKERS`
- **数据缓存**：`holders/stock/tushare_top10_holders_raw.json` 是 Tushare 原始接口数据的本地缓存（约 8 MB，随仓库提交），结构为 `{报告期: {股票代码: [原始记录]}}`，覆盖 **中证 800 + 中证 1000 成分股** 的 **2025 年年报及以后**（积分低于 2000 时没有接口权限，只能使用该缓存，见上文）。已有缓存会优先使用，避免重复请求；请勿删除该文件（全量重新拉取受 Tushare 限流影响非常慢）。生成的图片输出到 `output/` 目录，已加入 `.gitignore`
- **关键词切换**：`KEY_WORD_RATIO` 按 T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组，统一在 `holders/stock/tushare_client.py` 中配置，启用或停用关键词通过注释切换，修改一处即可；如需某个脚本单独使用不同关键词，可在该脚本内重新定义覆盖
- **信息安全**：`~/.vntrader/vt_setting.json` 中包含 Tushare token 与数据库密码，切勿提交到 git。本项目已弃用 `.env` 环境变量，所有配置统一从 vnpy 全局配置读取
- **编码**：所有代码和文本均为 UTF-8，Windows 下用编辑器或脚本读写中文时请注意编码

## 免责声明

本项目仅供量化学习与研究使用，不构成任何投资建议。数据来源于 Tushare，使用请遵守其服务条款。
