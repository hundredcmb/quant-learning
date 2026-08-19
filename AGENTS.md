# AGENTS.md

本文件是给 AI 编码代理（如 Codex）在本仓库工作时看的项目说明与约定。

## 项目简介

quant-learning 是一个 A 股量化学习项目（"量化小白从零开始学习量化"），目前包含三块内容：

- `holders/`：基于 Tushare 的十大股东席位关键词分析（国家队、社保、险资等），含多报告期对比与持仓公允价值变动统计
- `shenwan_industry/`：申万 2021 三级行业分类树，以及行业涨幅榜（单日 / 区间累计，等权 / 流通市值加权）
- `vnpy_examples/`：vnpy 学习示例（配置、K 线入库、数据服务下载、图表、指标、日线回测策略）

## 运行环境

- Python 3.10+（代码使用 `X | None`、`dict[str, ...]` 等新语法）
- 项目分两个独立环境，**不要混用**：
  - **`.venv`（申万模块专用，不依赖 vnpy）**：任意 Python 3.10+ 创建，装 `requirements.txt` 依赖（fastapi/uvicorn/pydantic/tushare/pandas，GUI 另含 PySide6）。初始化命令：`python3 -m venv .venv` → `.venv/bin/python -m pip install -r requirements.txt`（Windows 用 `.venv\Scripts\python.exe`）
  - **`.venv-vnpy`（holders / vnpy_examples 专用）**：必须用 vnpy 客户端（veighna studio）自带 Python 创建：`"<veighna python>" -m venv --system-site-packages .venv-vnpy`（`--system-site-packages` 继承 vnpy / tushare / pandas / TA-Lib / PySide6 全部依赖），再 `.venv-vnpy/bin/python -m pip install -r requirements.txt` 安装项目自身依赖；Windows 下对应 `.venv-vnpy\Scripts\python.exe`
- **禁止在代码/文档中硬编码 veighna 安装路径**（各机器安装路径不同；veighna Python 路径由用户在命令行自行指定）
- vnpy 环境（`.venv-vnpy`）相关：`holders/` 与 vnpy 示例需要联网访问 Tushare Pro API，token 在 vnpy `~/.vntrader/vt_setting.json` 的 `datafeed.password` 中配置；`datafeed.name` / `database.name` 决定数据源与数据库类型，数据库连接（`database.user` 等）同样从 vnpy 配置动态获取
- 图形界面（GUI）开发优先使用 vnpy 自带环境：客户端环境已内置 PySide6（vnpy 4.1.0 对应 PySide6 6.8）与 qdarkstyle，直接 `from PySide6.QtWidgets import ...` 开发窗口，不要额外安装 PyQt / PySide 等 GUI 依赖；需要与 vnpy 风格一致时优先复用 `vnpy.trader.ui` 与 `vnpy.chart` 的现成组件
- 项目已**弃用 `.env` 环境变量**：vnpy 部分配置统一从 vnpy 全局配置 `~/.vntrader/vt_setting.json` 动态读取（**禁止在代码中硬编码 token**）；申万部分 token 从 `shenwan_industry/config_store.py` 读取（见「注意事项」申万部分）

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `config.py` | 仅提供全局 `logger`（配置统一从 vnpy `SETTINGS` 动态获取，无环境变量） |
| `holders/stock/tushare_client.py` | 股票公共模块：Tushare token、原始数据缓存、限流、指数成分股、收盘价、单报告期关键词筛选、并发查询 |
| `holders/stock/top10_holders_value.py` | 基础版：按关键词筛选十大股东席位，计算报告期持仓市值（原始/折算，单位亿元），仅控制台输出 |
| `holders/stock/top10_holders_change.py` | 双报告期对比：`REPORT_PERIOD1 -> REPORT_PERIOD2` 的持股变动统计，生成表格图片 |
| `holders/stock/top10_holders_change_merged.py` | 同 top10_holders_change，另含 `merge_holders_by_stock`（同一股票多个匹配席位合并统计） |
| `holders/stock/top10_return_between_dates.py` | 单报告期、两个交易日（`REPORT_TRADE_DATE` vs `NEW_TRADE_DATE`）的公允价值变动与收益率，生成完整表格 + 汇总两张 PNG |
| `holders/stock/tushare_top10_holders_raw.json` | Tushare 原始数据缓存（约 8 MB，请勿删除；覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后） |
| `holders/etf/import_etf_data.py` | ETF 数据导入脚本：从 Excel 导入 ETF 基础信息与十大持有人到 `holders/etf/etf_top10_holders_raw.json` 和 `holders/etf/etf_basic.json`，支持冲突策略 `--on-conflict overwrite/keep` |
| `holders/etf/etf_client.py` | ETF 公共模块：只读持有人/基础信息缓存；日线直查（`fund_daily` 按交易日一次拉全市场）、`ts_code` 回填、未知后缀枚举 `.SH`/`.SZ` |
| `holders/etf/etf_top10_holders_value.py` | 单报告期关键词筛选 + 份额/市值统计（对标股票 value） |
| `holders/etf/etf_top10_holders_change.py` | 双报告期份额变动对比，生成表格图片（对标股票 change） |
| `holders/etf/etf_top10_holders_change_merged.py` | 同 change，另含按代码合并多席位统计（对标股票 change_merged） |
| `holders/etf/etf_top10_return_between_dates.py` | 两个交易日公允价值变动 + 收益率，生成表格/汇总图（对标股票 return_between_dates） |
| `holders/etf/etf_top10_holders_raw.json` | ETF 持有人缓存（结构与股票缓存一致） |
| `holders/etf/etf_basic.json` | ETF 基础信息缓存（代码、名称、成立日） |
| `holders/etf/README.md` | ETF 模块说明：Excel 导入格式、缓存结构、更新流程 |
| `holders/etf/etf_data_example.xlsx` | 本地 Excel 数据源（不入库） |
| `output/` | 图片运行产物目录 |
| `docs/tushare_api_reference.md` | Tushare 接口文档快照（随仓库提交、clone 即用，唯一权威；来源与更新方式见「Tushare 数据获取」注意事项） |
| `shenwan_industry/industry_tree.py` | 申万行业树与成分数据层：行业树构建、成分加载（`in_date`/`delist_date` 历史过滤）、股票池过滤（`filter_stock_pool` 锚点/末日参数化，单日榜与区间榜共用） |
| `shenwan_industry/market_data.py` | 申万行情数据层 `MarketDataProvider`：涨跌幅/收盘价/流通市值按日内存缓存、停牌 730 天回退、交易日历、区间逐日行情并发限流拉取、API 调用计数 |
| `shenwan_industry/industry_ranking.py` | 排行榜算法库：单日榜 + 单日榜编排（`run_daily_ranking`，CLI/Web 共用）+ 区间累计涨幅榜（等权 / 流通市值加权），含耗时输出工具 `print_timing` |
| `shenwan_industry/daily_ranking.py` | 单日行业涨幅榜入口脚本（含耗时分析输出） |
| `shenwan_industry/range_ranking.py` | 区间累计涨幅榜入口脚本（区间在文件内配置，含耗时分析输出） |
| `shenwan_industry/SW2021.json` | 申万 2021 行业分类本地数据（推荐的数据源） |
| `shenwan_industry/sw_index_daily_available.json` | 官方指数日线可用性缓存（探测生成、随仓库提交，30 天自动刷新；L1 全覆盖，L2/L3 据此决定 K 线是否可点击） |
| `shenwan_industry/config_store.py` | 申万模块本地配置存储：Tushare token 存于项目根目录 `.quant-learning/settings.json`（已 gitignore、不随仓库提交，权限 600）；CLI 与 Web 统一从这读取，不依赖 vnpy |
| `shenwan_industry/web/server.py` | 申万行业本地 FastAPI 入口：单日/区间排行提交、任务进度查询、成分股子表、静态页面托管 |
| `shenwan_industry/web/jobs.py` | Web 后台单 worker 任务队列与 Job 状态/进度管理 |
| `shenwan_industry/web/service.py` | Web 接口与现有行业排行算法的适配层 |
| `shenwan_industry/web/static/` | Web 前端页面：查询表单、进度条、主表和成分股子表 |
| `shenwan_industry/web/static/vendor/echarts.min.js` | 前端 ECharts 本地资源，用于行业指数 K 线图 |
| `shenwan_industry/web/desktop.pyw` | 桌面窗口启动器：后台自动启动 FastAPI，并用 Qt WebEngine 打开前端页面 |
| `vnpy_examples/` | vnpy 学习示例目录（配置、数据、图表、指标、回测等），按编号顺序学习 |

## 常用命令

```bash
# 两个环境，不要混用：
#   .venv      申万模块专用（任意 Python 3.10+ 创建，不依赖 vnpy）：python3 -m venv .venv → .venv/bin/python -m pip install -r requirements.txt
#   .venv-vnpy  holders / vnpy 示例专用（必须用 veighna Python 创建，--system-site-packages 继承 vnpy）：
#              "<veighna python>" -m venv --system-site-packages .venv-vnpy
# 以下命令以 Linux/macOS 路径为例（Windows 为 .venv\Scripts\python.exe / .venv-vnpy\Scripts\python.exe）

# ---------- 申万行业（.venv，不依赖 vnpy；token 在 Web 页面「数据配置」填写） ----------
.venv/bin/python -m shenwan_industry.web.server --host 127.0.0.1 --port 8080   # Web 服务
.venv/bin/python shenwan_industry/daily_ranking.py                              # 单日涨幅榜示例
.venv/bin/python shenwan_industry/range_ranking.py                              # 区间涨幅榜示例（区间在文件内配置）
.venv/bin/python shenwan_industry/web/desktop.pyw                               # 桌面窗口（GUI 需 PySide6）

# ---------- 十大股东分析（.venv-vnpy；token 在 vnpy 的 datafeed.password 中配置） ----------
.venv-vnpy/bin/python holders/stock/top10_holders_value.py
.venv-vnpy/bin/python holders/stock/top10_holders_change.py
.venv-vnpy/bin/python holders/stock/top10_holders_change_merged.py
.venv-vnpy/bin/python holders/stock/top10_return_between_dates.py

# ---------- ETF 十大持有人（.venv-vnpy；数据源为手动导入缓存，日线从 fund_daily 拉取） ----------
.venv-vnpy/bin/python holders/etf/import_etf_data.py
.venv-vnpy/bin/python holders/etf/etf_top10_holders_value.py
.venv-vnpy/bin/python holders/etf/etf_top10_holders_change.py
.venv-vnpy/bin/python holders/etf/etf_top10_holders_change_merged.py
.venv-vnpy/bin/python holders/etf/etf_top10_return_between_dates.py

# ---------- vnpy 示例（.venv-vnpy；需先配置好数据库与 tushare） ----------
.venv-vnpy/bin/python vnpy_examples/01_settings.py
.venv-vnpy/bin/python vnpy_examples/02_bardata.py
.venv-vnpy/bin/python vnpy_examples/03_datafeed.py
.venv-vnpy/bin/python vnpy_examples/05_indicator.py
.venv-vnpy/bin/python vnpy_examples/06_ma_strategy.py
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
- 生成的图片输出到仓库根目录 `output/`；缓存文件保持在 `holders/stock/tushare_top10_holders_raw.json`（约 8 MB，已提交进仓库，请勿删除、避免无谓膨胀），缓存结构为 `{报告期: {股票代码: [Tushare 原始记录列表]}}`，仅存接口原始数据、不含业务处理；修改业务逻辑时优先复用缓存，不要改变该结构
- 十大股东接口（如 `top10_holders`）至少需要 2000 Tushare 积分才有权限调用；低于 2000 积分时没有任何接口权限，只能使用仓库缓存分析其中已包含的数据（覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后）
- 接口失败时脚本会 `save_raw_cache()` 后 `os._exit(-1)` 退出；Tushare 有限流，默认 `MAX_REQUESTS_PER_MINUTE=180`（建议比官方限制低 20）、`MAX_WORKERS=5`（上限 20）

### 股票与 ETF 十大持有人（严格区分）

- **数据来源不同**：A 股股票的十大持有人可通过 Tushare 接口获取（也可以查询缓存）；**A 股 ETF 的十大持有人无法从 Tushare 获取**，只能手动录入缓存——持有人与基础信息（代码、名称、成立日）一律只从缓存读取，不调用 Tushare 的持有人/基础信息接口；**ETF 日线行情从 Tushare `fund_daily` 直接获取**（**至少需要 5000 积分**，低于 5000 积分无法拉取），按 `trade_date` 一次请求拉全市场，**不建价格缓存、不做限流**
- **代码格式相同但概念不同**：股票和 ETF 都是六位代码，但属于不同标的类型，开发和处理数据时不要混淆，务必严格区分股票与 ETF
- **更新周期不同**：A 股 ETF 的十大持有人一年只更新两次（半年报 + 年报）；股票一年四个财报期都会公布
- **ETF 数据缓存文件**：持有人缓存 `holders/etf/etf_top10_holders_raw.json`（结构与股票缓存完全一致：`{报告期: {代码: [{ts_code, holder_name, hold_amount(份), hold_ratio(%), rank}]}}`，rank 是 Excel 模板额外的排名字段，`ts_code` 为**无后缀代码**）；基础信息缓存 `holders/etf/etf_basic.json`（key 为**无后缀代码**，value 为 `{name, found_date, import_code(导入格式代码，导入时更新), ts_code(tushare 代码，拉取日线时回填)}`）；均由 `holders/etf/import_etf_data.py` 从 Excel 导入维护，`hold_amount` 已统一为“份”（Excel 中为“亿份”）。日线查询、ts_code 回填与未知后缀枚举（`.SH`/`.SZ`）由 `etf_client.py` 提供（`get_daily_prices` / `resolve_ts_code`）

### Tushare 数据获取

- Tushare 接口文档快照已随仓库提交：`docs/tushare_api_reference.md`（来源 waditu-tushare/skills 官方仓库，覆盖 235+ 个接口）。开发中需要获取 Tushare 数据时，**优先查阅**该文件确认接口名、必填/可选参数、返回字段与积分/频率限制，确保参数和结果解析正确，不要仅凭记忆硬写字段名；上游文档更新时从 https://github.com/waditu-tushare/skills.git 获取最新 `数据接口.md` 覆盖该文件即可（覆盖后保留文件头来源说明）
- `fund_daily`（ETF 日线行情）的 `ts_code` 与 `trade_date` 均为可选参数：**支持像股票 `daily` 一样按 `trade_date` 获取全市场 ETF 日线**（单次最多 5000 行，场内 ETF 数量足够一次拉取），也支持按 `ts_code` 或 `start_date/end_date` 区间获取单只历史

### 申万行业（shenwan_industry/）

- 行业树优先用本地 `SW2021.json` 构建（`build_industries()`），tushare 版本 `build_industries_by_tushare()` 仅作备用
- **申万模块已彻底脱离 vnpy**（不 import 任何 vnpy 包）：Tushare token 从本地配置 `shenwan_industry/config_store.py` 读取（Web 页面右上角「数据配置」填写保存，配置文件在项目根目录 `.quant-learning/settings.json`、已 gitignore 不随仓库提交；禁止硬编码）；CLI 与 Web 均不读 vnpy `SETTINGS`。依赖（fastapi/uvicorn/pydantic/tushare/pandas）见根 `requirements.txt`，Windows 下仍可复用 vnpy 环境运行
- Web 页面保存新 token 后，后台自动重置已构建的行业树上下文，下次查询用新 token 重建（`service.save_token` / `PreparedContext.ensure`）
- 流通市值加权算法对停牌股票做了特殊处理（回退查询停牌前最近流通市值），改动时不要破坏该逻辑
- **自建申万行业指数是项目未来核心工作**（官方指数不稳定且种类少）；历史成分缓存与指数构建规划见 `shenwan_industry/roadmap.md`
- 本模块的算法权威描述与强制核对流程见 `shenwan_industry/AGENTS.md`；涉及申万行业的任务在完成通知用户前，必须先对照该文件核对算法一致性
- 本地 Web 服务入口为 `shenwan_industry/web/server.py`，浏览器访问 `http://127.0.0.1:9010/`；首版采用单 worker 串行任务队列，长任务通过前端轮询进度条展示，并支持取消运行中/排队中的任务。多 worker 并发暂未实现，已写入 `shenwan_industry/roadmap.md`
- 若启动报 `WinError 10013`（端口绑定被拒）：多为 Windows 动态保留端口段覆盖了默认端口 9010，用 `netsh interface ipv4 show excludedportrange protocol=tcp` 检查，`net stop winnat && net start winnat`（管理员）释放后重试，或用 `--port` 换端口
- **智能体浏览器测试用独立端口**：ZCode 等 AI 代理通过浏览器插件/工具对 Web 页面做自动化测试时，**不要占用默认端口 9010**（该端口可能正被用户桌面窗口或手动启动的服务占用）；应使用 `--port` 显式指定其他端口启动测试用服务（如 9120），测试完成后自行关闭该进程，避免端口冲突与遗留进程
- 桌面窗口客户端入口为 `shenwan_industry/web/desktop.pyw`，Windows 使用 `pythonw.exe` 双击启动（Linux/macOS 用 `.venv/bin/python` 直接运行）会后台拉起 FastAPI 并打开 Qt WebEngine 窗口；关闭窗口会自动结束由该启动器拉起的后端
- 行业排行榜中，仅行业名称列可点击查看官方指数 K 线（代码列不响应点击；一级全覆盖；二级/三级仅官方指数有日线数据的行业可点击，可用性缓存于 `shenwan_industry/sw_index_daily_available.json`）；数据来自 Tushare `sw_daily`，前端使用本地 ECharts 绘制，副图支持成交额/成交量切换

### vnpy 示例（vnpy_examples/）

- 按编号顺序学习即可，具体功能与参数说明见各脚本内注释

### 环境与 Git
- **提交规则（重要）**：AI 编码代理（如 Codex）默认**不得自行执行 `git add` / `git commit` / `git push`**；只有用户明确要求提交时才可执行。代码/文档改动完成后保持未提交状态，等待用户指示。
- **Git 与 GitHub 操作优先使用 gh CLI（ZCode 内置 GitHub 插件技能）**：本机已安装并认证 GitHub CLI（`gh`，账号 `hundredcmb`，凭据存于系统 keyring）；涉及 GitHub 的操作（提交、PR、Issue、Gist、Release、仓库浏览等）统一走 `github:*` 插件技能（`/setup`、`/commit`、`/pr`、`/issue`、`/repo`、`/gist` 等斜杠命令，或直接自然语言触发），底层由 `gh` 完成；不要手写裸 `curl` 调 GitHub API、手工维护 token，也不要绕过 gh 直接操作 GitHub 网络资源

- 开发机为 Windows 或 Linux/macOS 均可，示例命令以 Linux/macOS 路径为例（Windows 将 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`、`.venv-vnpy/bin/python` 换成 `.venv-vnpy\Scripts\python.exe`、`/` 换成 `\`）；PIL 图片默认用 `msyh.ttc` 字体（代码已做跨平台兜底）；Windows PowerShell 读写中文文件时注意 UTF-8 编码
- `.gitignore` 已忽略 `.idea/` 与 `output/`（运行产物图片）；缓存 JSON 位于 `holders/` 且随仓库提交，不要忽略；新增生成文件时先确认是否应提交，`~/.vntrader/vt_setting.json` 中的真实 token / 数据库信息严禁提交
- GitHub 推送凭据：本机为双助手（GCM `manager` + `store`），git 按序尝试；**排查推送认证问题优先检查 `~/.git-credentials`**（明文条目 `https://用户名:token@github.com`，`store` 兜底，GCM 登录弹窗被取消不影响推送）；该文件与 `~/.vntrader/vt_setting.json`（Tushare token）是两套互不相干的凭据。gh CLI 的认证独立存储于系统 keyring（`gh auth status` 查看、`gh auth login`/`gh auth switch` 管理），与上述 git 推送凭据互不干扰
- 提交信息使用中文 Conventional Commits 风格（如 `feat:`、`fix:`），单行主题、简洁描述，优先交给 `/commit` 技能按此规范生成；开发分支建议使用 `codex/` 前缀
