# AGENTS.md

本文件是给 AI 编码代理在本仓库工作时看的项目说明与约定。

## 项目简介

quant-learning 是一个 A 股复盘与投研辅助项目（量化为辅），目前包含三块内容：

- `holders/`：基于 Tushare 的十大股东席位关键词分析（国家队、社保、险资等），含多报告期对比与持仓公允价值变动统计
- `shenwan_industry/`：申万 2021 三级行业分类树，以及行业涨幅榜（单日 / 区间累计，等权 / 自由流通市值加权 / 总市值加权）
- `vnpy_examples/`：vnpy 学习示例（配置、K 线入库、数据服务下载、图表、指标、日线回测策略）

## 运行环境

- Python 3.10+（代码使用 `X | None`、`dict[str, ...]` 等新语法）
- 项目分两个独立环境，**不要混用**（创建步骤与命令见 README；Windows 路径 `.venv\Scripts\python.exe` / `.venv-vnpy\Scripts\python.exe`）：
  - **`.venv`（申万模块专用，不依赖 vnpy）**：任意 Python 3.10+ 创建，装 `requirements.txt` 依赖（fastapi/uvicorn/pydantic/tushare/pandas，GUI 另含 PySide6）
  - **`.venv-vnpy`（holders / vnpy_examples 专用）**：必须用 veighna studio 自带 Python 创建（`--system-site-packages` 继承 vnpy / tushare / pandas / TA-Lib / PySide6）
- **禁止在代码/文档中硬编码 veighna 安装路径**（各机器安装路径不同；veighna Python 路径由用户在命令行自行指定）
- vnpy 部分（`.venv-vnpy`）的 Tushare token 与数据库连接统一从 `~/.vntrader/vt_setting.json` 动态读取（`datafeed.password` 为 token，配置示例见 README）；申万部分 token 从 `shenwan_industry/config_store.py` 读取（见「注意事项」申万部分）。项目已**弃用 `.env` 环境变量**，**禁止在代码中硬编码 token**
- 图形界面（GUI）开发优先使用 vnpy 自带环境：客户端环境已内置 PySide6（vnpy 4.1.0 对应 PySide6 6.8）与 qdarkstyle，直接 `from PySide6.QtWidgets import ...` 开发窗口，不要额外安装 PyQt / PySide 等 GUI 依赖；需要与 vnpy 风格一致时优先复用 `vnpy.trader.ui` 与 `vnpy.chart` 的现成组件

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `README.md` | 项目使用说明与**全部运行命令**（申万模块用 `.venv`、其余用 `.venv-vnpy`；ETF 导入格式另见 `holders/etf/README.md`） |
| `config.py` | 仅提供全局 `logger`（配置统一从 vnpy `SETTINGS` 动态获取，无环境变量） |
| `holders/stock/` | 十大股东分析：`tushare_client.py`（股票公共模块：token/缓存/限流/指数成分股/收盘价/关键词筛选/并发查询）+ 四个脚本（`top10_holders_value` / `top10_holders_change`（`+_merged` 版）/ `top10_return_between_dates`）+ 缓存 `tushare_top10_holders_raw.json`（约 8 MB，勿删）；功能与命令见 README |
| `holders/etf/` | ETF 十大持有人：`import_etf_data.py`（Excel 导入）/ `etf_client.py`（公共模块）/ 四个脚本（对标股票 stock）/ 缓存 `etf_top10_holders_raw.json` + `etf_basic.json` / 示例 Excel（不入库）；说明见 `holders/etf/README.md` |
| `output/` | 图片运行产物目录 |
| `docs/tushare_api_reference.md` | Tushare 接口文档快照（随仓库提交、clone 即用，唯一权威；来源与更新方式见「Tushare 数据获取」注意事项） |
| `shenwan_industry/` | 申万行业模块：行业树 + 单日/区间涨幅榜（等权 / 自由流通市值加权 / 总市值加权）+ FastAPI Web 服务与桌面启动器；各文件功能、算法权威描述与强制核对流程见模块内 `AGENTS.md` |
| `shenwan_industry/docs/` | 模块文档：`interface_notes.md`（Tushare 接口交互明细）、`known_issues.md`（已知边界与易错点）、`roadmap.md`（未来规划）、`sync_progress.md`（官方算法同步进度）、`Shenwan_Index_Series_Algorithm_Text.md`（申万官方指数算法纯文字版，**只读禁止修改**） |
| `vnpy_examples/` | vnpy 学习示例目录（配置、数据、图表、指标、回测等），按编号顺序学习 |

## 代码约定

- 注释、文档字符串、日志和输出均为中文；脚本通过 `if __name__ == "__main__":` 直接运行
- 脚本型代码把可调参数集中放在文件顶部“核心配置”区，注释掉的分支表示备选方案
- 日期统一为 `YYYYMMDD` 字符串；股票代码统一为 Tushare 格式（如 `600036.SH`）
- 持仓市值计算：`市值(亿) = hold_amount * close / 1e8`，折算市值 = 市值 × 席位折算比例
- 类型注解尽量完整；遇到 PyPI 包缺失，先确认是否由 vnpy 客户端环境提供，只有项目自身依赖才加入 requirements.txt

## 注意事项

### 十大股东分析（holders/）

- 公共数据获取逻辑（token、缓存、限流、指数成分股、收盘价、并发查询）已抽到 `holders/stock/tushare_client.py`，四个脚本只保留各自的业务配置与逻辑，修改公共逻辑只改这一处；`KEY_WORD_RATIO`（席位关键词 → 折算比例，T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组）也统一在此配置（改一处即可），如需某脚本单独调整可在该脚本 import 后重新定义覆盖
- 输出位置、缓存结构、积分门槛与限流参数等运行细节见 README：图片输出仓库根目录 `output/`；缓存 `holders/stock/tushare_top10_holders_raw.json`（约 8 MB、已提交勿删、结构 `{报告期: {股票代码: [Tushare 原始记录列表]}}`，仅存接口原始数据，修改业务逻辑时优先复用缓存、不要改变该结构）；`top10_holders` 至少 2000 积分（低于时只能用缓存）；接口失败先 `save_raw_cache()` 再 `os._exit(-1)` 退出；限流默认 `MAX_REQUESTS_PER_MINUTE=180`（建议比官方限制低 20）、`MAX_WORKERS=5`（上限 20）

### 股票与 ETF 十大持有人（严格区分）

- **数据来源不同**：A 股股票的十大持有人可通过 Tushare 接口获取（也可以查询缓存）；**A 股 ETF 的十大持有人无法从 Tushare 获取**，只能手动录入缓存——持有人与基础信息（代码、名称、成立日）一律只从缓存读取，不调用 Tushare 的持有人/基础信息接口；**ETF 日线行情从 Tushare `fund_daily` 直接获取**（**至少需要 5000 积分**），按 `trade_date` 一次请求拉全市场、**不建价格缓存、不做限流**（细节见 README）
- **代码格式相同但概念不同**：股票和 ETF 都是六位代码，但属于不同标的类型，开发和处理数据时不要混淆，务必严格区分股票与 ETF
- **更新周期不同**：A 股 ETF 的十大持有人一年只更新两次（半年报 + 年报）；股票一年四个财报期都会公布
- **ETF 数据缓存文件**：持有人缓存 `holders/etf/etf_top10_holders_raw.json`（结构与股票缓存一致，`ts_code` 为**无后缀代码**、含 `rank` 排名字段）+ 基础信息缓存 `holders/etf/etf_basic.json`（key 为**无后缀代码**），均由 `holders/etf/import_etf_data.py` 从 Excel 导入维护、`hold_amount` 统一为“份”（Excel 中为“亿份”）；字段明细见 `holders/etf/README.md`「缓存结构」，日线查询、ts_code 回填与后缀枚举（`.SH`/`.SZ`）由 `etf_client.py` 提供（`get_daily_prices` / `resolve_ts_code`）

### Tushare 数据获取

- Tushare 接口文档快照已随仓库提交：`docs/tushare_api_reference.md`（覆盖 235+ 个接口）。开发中需要获取 Tushare 数据时，**优先查阅**该文件确认接口名、必填/可选参数、返回字段与积分/频率限制，确保参数和结果解析正确，不要仅凭记忆硬写字段名；文档来源与上游更新方式见 README
- `fund_daily`（ETF 日线行情）的 `ts_code` 与 `trade_date` 均为可选参数：**支持像股票 `daily` 一样按 `trade_date` 获取全市场 ETF 日线**（单次最多 5000 行，场内 ETF 数量足够一次拉取），也支持按 `ts_code` 或 `start_date/end_date` 区间获取单只历史

### 申万行业（shenwan_industry/）

- **已彻底脱离 vnpy**（不 import 任何 vnpy 包）：token 从 `config_store.py` 读取（Web 页面右上角「数据配置」填写保存，存储位置与配置步骤见 README；**禁止硬编码 token**），依赖见根 `requirements.txt`；Web 保存新 token 后后台自动重置已构建的行业树上下文（`service.save_token` / `PreparedContext.ensure`）
- **算法权威与强制核对**：涉及申万行业的任务在报告完成前，必须先对照模块 `AGENTS.md`（算法权威描述 + 强制核对流程）；官方指数算法纯文字版 `docs/Shenwan_Index_Series_Algorithm_Text.md` **只能读、禁止任何修改**（确需变更须用户提供新版本覆盖），各市值类加权算法与其同步的进度见 `docs/sync_progress.md`
- **自建申万行业指数是项目未来核心工作**（官方指数不稳定且种类少），规划见 `docs/roadmap.md`
- Web 服务入口 `web/server.py`（浏览器访问 `http://127.0.0.1:9010/`）：单 worker 串行队列 + 进度轮询 + 任务取消；服务启动/保存 token 后后台预建行业树（`service.prebuild_context` → `PreparedContext.build_async`，构建互斥，首次查询即就绪）
- 端口冲突自动顺延（方案 B，`web/port_picker.py`）；排查 Windows 动态保留段：`netsh interface ipv4 show excludedportrange protocol=tcp`，或管理员 `net stop winnat && net start winnat` 释放后配合 `--port` 固定端口
- **智能体浏览器测试用独立端口**：ZCode 等 AI 代理自动化测试 Web 页面时**不要占用默认端口 9010**（可能正被用户桌面窗口或手动启动的服务占用），用 `--port` 显式指定其他端口（如 9400），测试完成后自行关闭进程
- 桌面启动器 `web/desktop.pyw` 有资源释放兜底（后端 `--parent-pid` 看门狗线程 + `KeyboardInterrupt`/finally），即使启动进程被 IDE 强制停止也会连带结束后端，避免端口与进程残留

### vnpy 示例（vnpy_examples/）

- 按编号顺序学习即可（命令与说明见 README），具体功能与参数说明见各脚本内注释

### 环境与 Git
- **提交规则（重要）**：AI 编码代理默认**不得自行执行 `git add` / `git commit` / `git push`**；只有用户明确要求提交时才可执行。代码/文档改动完成后保持未提交状态，等待用户指示。
- **Git 与 GitHub 操作**：GitHub 操作统一走 `gh` CLI（确保已安装并完成一次 `gh auth login` 认证）；涉及 GitHub 的操作（提交、PR、Issue、Gist、Release、仓库浏览等）按运行环境分两种方式，**底层都是 `gh`**：
  - **ZCode 环境**：使用 ZCode 内置 GitHub 插件技能（`github:*`：`/setup`、`/commit`、`/pr`、`/issue`、`/repo`、`/gist` 等斜杠命令，或直接自然语言触发）
  - **非 ZCode 环境（其他 AI 代理）**：直接使用 `gh` CLI 完成
  - 不要手写裸 `curl` 调 GitHub API、手工维护 token，也不要绕过 `gh` 直接操作 GitHub 网络资源

- 开发机为 Windows 或 Linux/macOS 均可，示例命令以 Linux/macOS 路径为例（Windows 将 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`、`.venv-vnpy/bin/python` 换成 `.venv-vnpy\Scripts\python.exe`、`/` 换成 `\`）；PIL 图片默认用 `msyh.ttc` 字体（代码已做跨平台兜底）；Windows PowerShell 读写中文文件时注意 UTF-8 编码
- `.gitignore` 已忽略 `.idea/` 与 `output/`（运行产物图片）；缓存 JSON 位于 `holders/` 且随仓库提交，不要忽略；新增生成文件时先确认是否应提交，`~/.vntrader/vt_setting.json` 中的真实 token / 数据库信息严禁提交
- 提交信息使用中文 Conventional Commits 风格（如 `feat:`、`fix:`），单行主题、简洁描述（ZCode 环境优先交给 `/commit` 技能按此规范生成）
