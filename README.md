# quant-learning

量化小白从零开始学习量化，主要用于 A 股市场。

本项目是一套围绕 A 股量化学习整理的、可直接运行的 Python 脚本，覆盖：

- **十大股东席位分析**（`holders/`）：基于 Tushare 数据，在中证 800 + 中证 1000 样本池内按席位关键词（国家队、社保、险资等）筛选十大股东持仓，支持多报告期对比、公允价值变动与收益率统计，并输出控制台报表和图片
- **申万行业分析**（`shenwan_industry/`）：申万 2021 三级行业分类树，计算单日行业涨幅榜（等权 / 流通市值加权）
- **vnpy 入门示例**（`vnpy_examples/`）：从配置、K 线入库、数据服务下载，到图表绘制、技术指标计算和日线双均线回测策略

## 运行前提（必须）

本项目依赖 vnpy 完整环境（vnpy、vnpy_tushare、vnpy_ctastrategy、tushare、pandas、numpy、Pillow、TA-Lib 等），**必须安装 vnpy 客户端（veighna studio）**。代码不直接依赖 veighna 的具体安装路径：Windows 运行 `.\setup.ps1 -PythonPath <veighna python 路径>`、Linux/macOS 运行 `./setup.sh -p <veighna python 路径>` 创建继承该环境的虚拟环境 `.venv`，之后所有脚本统一用 `.venv` 中的 Python 运行（Windows 为 `.venv\Scripts\python.exe`，Linux/macOS 为 `.venv/bin/python`）。不要用系统 Python 单独 pip 安装 vnpy 相关依赖。

- vnpy 客户端下载：https://www.vnpy.com/
- 客户端安装后自带 Python 3.13 和完整 vnpy 环境；**veighna 的安装路径因人而异**（例如 Windows 下 `C:\veighna_studio`、`D:\Tools\veighna_studio`）。本项目不依赖具体安装路径：Windows 用 `setup.ps1 -PythonPath`、Linux/macOS 用 `setup.sh -p` 手动指定本机 veighna Python 后创建本地虚拟环境 `.venv`，之后所有命令统一用 `.venv` 中的 Python 运行（详见「快速开始」）

## 目录结构

```text
quant-learning/
├── config.py                  # 仅提供全局 logger（配置统一从 vnpy 全局配置读取）
├── requirements.txt           # 项目自身依赖（vnpy 相关库由客户端提供）
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
│       ├── etf_client.py               # 公共模块：缓存读取 + 日线直查（fund_daily）
│       ├── etf_top10_holders_value.py  # 单报告期筛选 + 份额/市值统计
│       ├── etf_top10_holders_change.py # 双报告期份额变动对比
│       ├── etf_top10_holders_change_merged.py  # 变动对比（同代码多席位合并）
│       ├── etf_top10_return_between_dates.py  # 两交易日公允价值变动 + 收益率
│       ├── etf_top10_holders_raw.json  # 持有人缓存（结构与股票缓存一致）
│       └── etf_basic.json              # ETF 基础信息缓存（代码、名称、成立日）
├── output/                    # 运行产物（生成的图片，已 gitignore）
├── shenwan_industry/
│   ├── industry_tree.py       # 申万行业树与成分数据层
│   ├── industry_ranking.py    # 排行榜算法（单日 + 区间）
│   ├── daily_ranking.py       # 单日涨幅榜入口（含耗时分析）
│   ├── range_ranking.py       # 区间涨幅榜入口（含耗时分析）
│   ├── config_store.py        # 本地配置存储（Tushare token，存项目根目录 .quant-learning/、不提交 git）
│   └── SW2021.json            # 申万 2021 行业分类本地数据
├── docs/
│   └── tushare_api_reference.md     # Tushare 接口文档快照（随仓库提交，clone 即用）
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

下载并安装 veighna studio（自带 Python 3.13 与完整 vnpy 环境），记下你本机 veighna 自带 Python 的路径（例如 `C:\veighna_studio\python.exe` 或 `D:\Tools\veighna_studio\python.exe`）。

### 2. 初始化项目虚拟环境 `.venv`

在项目根目录执行（把路径换成你本机 veighna 自带 Python）：

Windows（PowerShell）：

```powershell
cd <你的项目根目录>
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PythonPath "D:\Tools\veighna_studio\python.exe"
```

Linux / macOS：

```bash
cd <你的项目根目录>
./setup.sh -p "/opt/veighna_studio/bin/python"
```

> `-PythonPath`（Windows）/ `-p`（Linux/macOS）为必填参数（脚本不会自动探测），必须指向 veighna studio 自带的 Python。

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

- `datafeed.*`：数据服务配置。`name` 固定为 `tushare`，`password` 填你的 Tushare token（在 [tushare.pro](https://tushare.pro) 注册后获取，并需开通 `top10_holders`、`index_weight`、`stock_basic`、`daily`、`index_classify`、`index_member_all`、`daily_basic` 等接口权限）；`holders/` 从这里读取 token（需 vnpy 环境）
- `database.*`：数据库连接配置，供 vnpy 示例读写 K 线使用（`vnpy_examples/02、03、05、06` 通过 `get_database()`）

> **申万模块独立配置**：`shenwan_industry/` 已彻底脱离 vnpy，token 在 Web 页面右上角「数据配置」中填写保存（存到项目根目录 `.quant-learning/settings.json`，已 gitignore 不提交，无需 vnpy）。
- 修改配置前请先关闭 vnpy 客户端，避免配置被覆盖

### 4. 运行脚本

所有脚本统一用 `.venv` 中的 Python 运行（不再依赖 veighna 的具体安装路径），例如：

Windows（PowerShell）：

```powershell
.venv\Scripts\python.exe holders\stock\top10_holders_value.py
```

Linux / macOS：

```bash
.venv/bin/python holders/stock/top10_holders_value.py
```

桌面窗口客户端：Windows 用 `pythonw` 启动，Linux/macOS 直接用 `python`：

```powershell
.venv\Scripts\pythonw.exe shenwan_industry\web\desktop.pyw
```

```bash
.venv/bin/python shenwan_industry/web/desktop.pyw
```

## 使用 AI Agent 开发

Tushare 接口文档已随仓库提交：`docs/tushare_api_reference.md`（Tushare 官方 skills 的 `数据接口.md` 快照，覆盖 235+ 个 Tushare API）。开发中涉及 Tushare 数据获取时，应**优先查阅该文档**确认接口参数、返回字段与积分 / 权限要求，确保参数与结果解析正确；上游（https://github.com/waditu-tushare/skills.git）文档有更新时，获取最新版覆盖该文件即可（覆盖后保留文件头的来源说明）。

## 使用说明

> 以下命令行示例以 Windows 为例；Linux/macOS 将 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`、`\` 换成 `/` 即可。

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
.venv\Scripts\python.exe shenwan_industry\daily_ranking.py
.venv\Scripts\python.exe shenwan_industry\range_ranking.py   # 区间涨幅榜（区间在文件内配置）
```

申万模块已彻底脱离 vnpy，token 不读 vnpy 配置：先启动 Web 服务在页面右上角「数据配置」填写保存，CLI 与 Web 共用这份本地配置（存于项目根目录 `.quant-learning/settings.json`，不提交 git）。

示例默认计算指定日期（或区间）的申万一级 / 二级 / 三级行业涨幅榜（等权与流通市值加权两种口径），行业树从本地 `SW2021.json` 构建；两个入口运行结束都会在控制台输出耗时分析与 API 调用次数。

### 3. vnpy 示例

示例按学习路径编号，建议按顺序运行：

```powershell
.venv\Scripts\python.exe vnpy_examples\01_settings.py
.venv\Scripts\python.exe vnpy_examples\02_bardata.py
.venv\Scripts\python.exe vnpy_examples\03_datafeed.py
.venv\Scripts\python.exe vnpy_examples\05_indicator.py
.venv\Scripts\python.exe vnpy_examples\06_ma_strategy.py
```

注意：

- `04_chart.py` 会启动 Qt 图形界面，需在带桌面的图形环境（Windows / Linux / macOS）下运行
- `06_ma_strategy.py` 的 `skip_ex` 参数：`0` = 忽略分红、使用不复权价格回测（走本地数据库，支持 vnpy 参数优化）；`1` = 每次分红前逃权（每次回测调用 Tushare，不支持参数优化）

## 重要说明

- **Tushare 限流**：脚本内置每分钟请求数限制（默认 180）和线程并发控制（默认 5，上限 20）。接口失败时会先保存缓存再退出，请根据账号权限调整 `MAX_REQUESTS_PER_MINUTE` 和 `MAX_WORKERS`
- **数据缓存**：`holders/stock/tushare_top10_holders_raw.json` 是 Tushare 原始接口数据的本地缓存（约 8 MB，随仓库提交），结构为 `{报告期: {股票代码: [原始记录]}}`，覆盖 **中证 800 + 中证 1000 成分股** 的 **2025 年年报及以后**（积分低于 2000 时没有接口权限，只能使用该缓存，见上文）。已有缓存会优先使用，避免重复请求；请勿删除该文件（全量重新拉取受 Tushare 限流影响非常慢）。生成的图片输出到 `output/` 目录，已加入 `.gitignore`
- **关键词切换**：`KEY_WORD_RATIO` 按 T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组，统一在 `holders/stock/tushare_client.py` 中配置，启用或停用关键词通过注释切换，修改一处即可；如需某个脚本单独使用不同关键词，可在该脚本内重新定义覆盖
- **ETF 日线行情**：`fund_daily` 接口需要**至少 5000 Tushare 积分**，按 `trade_date` 一次请求拉全市场（`etf_client.get_daily_prices`），不建缓存、不做限流；低于 5000 积分无法拉取。ETF 十大持有人与基础信息仍只从缓存读取（手动导入）
- **信息安全**：`~/.vntrader/vt_setting.json` 中包含 Tushare token 与数据库密码，切勿提交到 git。本项目已弃用 `.env` 环境变量：`holders/` 与 vnpy 示例统一从 vnpy 全局配置读取；`shenwan_industry/` 独立于 vnpy，token 保存在项目根目录 `.quant-learning/settings.json`（`shenwan_industry/config_store.py`），已 gitignore 同样不提交 git
- **编码**：所有代码和文本均为 UTF-8，Windows 下用编辑器或脚本读写中文时请注意编码

## 免责声明

本项目仅供量化学习与研究使用，不构成任何投资建议。数据来源于 Tushare，使用请遵守其服务条款。
