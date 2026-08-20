# shenwan_industry 模块说明（算法与接口约定）

本文件是申万行业模块的**算法权威描述**。涉及本模块的任何任务（新增、修改、运行、排查、文档），在向用户报告完成之前，必须先对照本文件核对一致性，具体要求见文末「强制核对流程」。

## 模块职责与文件

- 模块内容：申万 2021 三级行业分类树 + 行业涨幅榜（单日榜 / 区间累计榜，等权 / 自由流通市值加权 / 总市值加权），当前为控制台输出脚本（与 `holders/` 生成图片不同）
- 文件：
  - `industry_tree.py`：行业树与成分数据层（`ShenWanIndustryNode` / `ShenWanIndustryTree`：树构建、成分加载、`in_date`/`delist_date` 记录、股票池过滤 `filter_stock_pool`）
  - `market_data.py`：行情数据层 `MarketDataProvider`（API 调用计数、按日缓存、停牌 730 天回退、交易日历、区间逐日行情并发限流拉取）
  - `industry_ranking.py`：排行榜算法库（`daily_rank_equal_weight` / `daily_rank_float_weight` / `run_daily_ranking` / `rank_range`）+ 耗时工具 `print_timing`
  - `config_store.py`：本地配置存储（Tushare token，存项目根目录 `.quant-learning/settings.json`、已 gitignore 不提交），CLI 与 Web 统一从这里读 token
  - `daily_ranking.py` / `range_ranking.py`：单日 / 区间榜入口脚本（含耗时分析输出）
  - `data/`：需提交的数据/缓存子目录——`SW2021.json`（申万 2021 行业分类本地数据，推荐数据源，勿删）、`sw_index_daily_available.json`（官方指数日线可用性缓存，探测生成、随仓库提交，每周六 00:00 过期、约合每周刷新）
  - `__init__.py`：空
  - `web/`：本地 FastAPI Web 服务（`server.py` / `jobs.py` / `service.py` / `schemas.py` / `port_picker.py` + `static/`）与桌面启动器 `desktop.pyw`（WebView 直启，无中间过渡页）；`port_picker.py` 负责端口自动顺延（首选端口被占/落系统保留段时 +1 逐个实测，server.py 与 desktop.pyw 共用，方案 B）；单 worker 串行队列、前端轮询进度、成分股子表、行业指数 K 线（`sw_daily` + 本地 ECharts；L1 全覆盖，L2/L3 按官方指数可用性可点击）
  - `docs/`：模块文档目录（`interface_notes.md`、`known_issues.md`、`roadmap.md`、`sync_progress.md`、申万官方指数算法文本 `Shenwan_Index_Series_Algorithm_Text.md`）
- 子文档（按需查阅，均在 `docs/` 下）：
  - `docs/interface_notes.md`：Tushare 接口交互明细与限流实测（**强制核对流程必读**）
  - `docs/known_issues.md`：已知边界与易错点（21 条）
  - `docs/roadmap.md`：未来规划（自建行业指数）与 Web 优化
  - `docs/sync_progress.md`：官方指数算法同步进度记录（独立子文档，见第 8 节）
  - `docs/Shenwan_Index_Series_Algorithm_Text.md`：**申万官方指数算法纯文字版（只读、禁止修改，见第 8 节）**

## 运行环境与数据源

本模块**已彻底脱离 vnpy**（不 import 任何 vnpy 包），可用任一带 tushare/pandas 的 Python 运行；token 从本地配置 `config_store.py` 读取（Web 页面右上角「数据配置」填写保存，配置文件在项目根目录 `.quant-learning/settings.json`、已 gitignore 不随仓库提交；禁止硬编码）；需联网访问 Tushare Pro，接口权限依赖账号积分；日期统一 `YYYYMMDD`（内部用 `datetime`，`strftime("%Y%m%d")` 转换）。

## 核心算法约定（必读）

### 1. 行业树构建

- 默认 `build_industries()`：读本地 `data/SW2021.json` 逐行 `parse_industry_row()` 构建；备用 `build_industries_by_tushare()`（`pro.index_classify(src='SW2021')`），行业数据长期不变，本地优先
- `parse_industry_row()`：`level` 为字符串 `"L1"/"L2"/"L3"`；L1 挂到 root，其余按 `parent_code` 在 `industry_code_to_node` 找父节点挂接；登记 `index_code` / `industry_code` / `industry_name` 三种映射
- 全称 `industry_name_long`：L1=自身名；L2=父名+"-"+自身名；L3=父全称+"-"+自身名；父节点缺失直接 `KeyError`（当前未兜底）

### 2. 成分股加载与过滤

- `build_constituent_stocks_by_tushare(filter_unlisted=True)`：`pro.stock_basic` 一次拉取**上市(L)+退市(D)+暂停(P)** 三态股票（D 的 `delist_date` 记入 `ts_code_to_delist_date`）；`pro.index_member_all(offset, limit=1999)` 循环拉成分，按 `l3_code` 把 `ts_code` 同时挂入 L3 及其 L2/L1 祖先节点并登记 `constituent_stock_to_l3_node`，`in_date` 记入 `ts_code_to_in_date`；不在 `stock_basic` 的股票跳过，`l3_code` 找不到节点 → `ValueError`
- `filter_stock_pool(stock_pool, anchor_date, end_date, cancel_check=None)`：锚点/末日参数化（单日榜传 `(date,date)`，区间榜传 `(起始日,末日)`），返回剔除类别明细 `{类别: [ts_code]}`（`no_industry` / `in_date_later` / `delisted` / `not_listed`）供区间榜汇总告警
  - 剔除 `no_industry_stocks`（本实例累计解析失败、无行业归属的股票）
  - 剔除 `in_date > anchor` 的成分（**消除"未来纳入"前视偏差**）
  - 剔除 `delist_date < end` 的股票（**修复退市股被整体剔除的幸存者偏差**；退市日当天及之前正常参与）
  - 剔除 `list_date >= anchor` 的股票（当天及之后才上市不参与）
- 排名时股票池 = **当日行情股票 ∪ 已加载成分股**，再做上述过滤

### 3. 行情获取与涨跌幅/自由流通市值口径（`market_data.MarketDataProvider` 方法）

- `get_ts_code_to_pct_chg(date)`：`pro.daily(trade_date, offset, limit=5999)` 循环拉全市场；**涨跌幅自行重算** `pct=(close−pre_close)/pre_close*100`（不用接口 `pct_chg` 字段）；按日期存内存缓存
- **涨跌幅口径（已实测验证）**：`daily.pre_close` 在除权除息日返回交易所**除权参考价**（实测：招行 20250711 除息 `46.24=48.24−2.0`；药明康德 20190702 除权 `64.66=(91.10−0.58)/1.4`），故 `close/pre_close` 与 `adj_factor` 比值法**等价**（实测 −1.2976% vs −1.2980%），无需额外拉取 `adj_factor`
- 该口径为**价格指数口径**：除权除息无虚假跳变，但现金分红**不计入收益**（除息价差被除权参考价抵消）；做"红利再投资全收益指数"需在除息日另行加回股息率；改动涨跌幅逻辑须保持该口径，如改用 `adj_factor`/复权行情须确认同一价格口径并同步更新本文件
- `get_ts_code_to_free_mv(date)`：`pro.daily_basic(ts_code='', trade_date, fields='ts_code,close,total_mv,free_share,float_share', offset, limit=5999)` 循环拉全市场（官方上限 6000）；**同一次请求同时缓存自由流通市值与总市值**（`get_ts_code_to_total_mv` 读同一缓存，零额外请求）；**自由流通市值 = `free_share × close`**（自由流通股本×收盘价，三字段取同一行、同一交易日，等价于旧式 `circ_mv × free_share / float_share`：恒等推导 `circ_mv = float_share × close`，实测 `daily_basic.close` 与 `daily.close` 逐股完全一致；`total_mv` 单位万元、`free_share`/`float_share` 单位万股、`close` 单位元，加权只用比值不影响结果）；股本异常防御：三字段任一缺失/非有限值、`close`/`free_share`/`float_share` ≤ 0 或比例 >1（自由流通股本超过流通股本，数据异常）时该股不记入，等同无市值处理；按日期存内存缓存。**口径来源说明**：`free_share` 为 Tushare 自有自由流通口径（黑盒返回最终股本，无法获取扣减明细），与官方《编制说明》附录二六类扣减定义**无法逐条核对**，本模块以 Tushare 口径为准（见 `docs/known_issues.md` 第 18 条）

### 4. 单日等权涨幅 `daily_rank_equal_weight(tree, market_data, date)`

- 股票池过滤见第 2 节；逐只取 L1/L2/L3 节点，解析失败跳过（记入 `no_industry_stocks`）
- 单股涨跌幅：有行情用实际值；**停牌（行情缺失）按 0% 计入，且计入成分股数量**
- 数据异常（涨跌幅非有限值）：记为 None 并告警，**直接跳过，不计入平均与数量**（与停牌按 0% 不同）
- 行业涨幅 = 成分股涨跌幅简单算术平均（增量平均 `new_avg=(old_avg*old_count+pct)/(old_count+1)`），L3/L2/L1 三级同时累计
- 排序：剔除 `count==0` 的行业，按涨幅降序；返回 `(index_code, pct, count)` 列表（三级各一个）

### 5. 单日市值加权涨幅 `daily_rank_float_weight(tree, market_data, date, mv_kind="free")`

- `mv_kind`：`"free"`=自由流通市值加权、`"total"`=总市值加权；同一套公式、市值来源参数化（**不要复制两套代码**）
- 单股单级公式（M=当日收盘权重市值，p=当日涨跌幅%）：当日新增市值 `ΔM=M*p/(p+100)`；开盘前市值 `M_pre=M/(1+p/100)`；行业涨幅 = `ΣΔM/ΣM_pre*100`（三级分别累计后取比值）
- 停牌处理（本模块最特殊逻辑）：当天 `daily_basic` 无自由流通市值时回退查询 `daily_basic(ts_code, fields='trade_date,close,total_mv,free_share,float_share', start_date=前 730 天, end_date=date)`，**一次请求同时回退自由流通市值/总市值**（`resolve_free_mv` 返回自由流通市值并顺带缓存总市值，`resolve_total_mv` 优先读缓存避免重复请求），取降序第一条 `trade_date<=date` 的有效值作"停牌前最近自由流通市值"并回填缓存；**自由流通市值三字段必须取自同一行（同一交易日）**计算，避免混搭不同日期股本；**最多支持连续停牌约 2 年（730 天）**，再往前查不到 → `ValueError`
- 停牌股涨幅按 0% 计 → `ΔM=0`，但 `M_pre=M` 仍计入分母（稀释行业涨幅）；数据异常跳过规则与等权一致，回退扫描跳过 NaN 行取最近有效值
- 其余规则（股票池、节点解析、排序、返回结构）与等权一致

### 6. 输出与示例

- 入口脚本从 `config_store.get_token()` 取 token → 构建树/加载成分 → 计算 → 按 L3/L2/L1 打印三列涨幅（总市值加权/自由流通市值加权/等权）、全称、成分股数量与名称；对每只行业指数用 `-100` 作"等权缺失"哨兵校验，命中即报错（单日示例日期硬编码 `2025-04-07`；区间在 `range_ranking.py` 内 `RANGE_START`/`RANGE_END` 配置）
- 运行结束输出**耗时分析**（组小计、各阶段耗时/占比、总耗时与 API 调用次数）：耗时统计 `print_timing`，API 次数由 `MarketDataProvider.snapshot_api_calls()` 提供（构造时即包装计数，含建树阶段）；大阶段按"接口拉取 vs 本地计算/回退"拆分
- 单日榜入口（CLI 与 Web `service._run_daily`）统一走 `run_daily_ranking`（拉行情/市值 → 等权 → 自由流通市值加权 → 总市值加权，避免两套编排漂移），**返回 4 元组 `(等权, 自由流通市值加权, 总市值加权, timings)`**；timings key：`daily_fetch`/`mv_fetch`/`equal_compute`/`float_compute`/`float_fallback`/`total_compute`/`total_fallback`（总市值回退走缓存，`total_fallback` 通常为 0）；进度回调 `(0~100, 说明, 阶段名)`（阶段名供 Web 前端展示）
- 模块暂无图片产物，仅控制台输出

### 7. 区间累计涨幅榜 `rank_range(tree, market_data, start_date, end_date, timings=None, progress_callback=None, detail=None)`

- 返回 `(等权(l1,l2,l3), 自由流通市值加权(l1,l2,l3), 总市值加权(l1,l2,l3))`，榜单项 `(index_code, 涨跌幅%, 成分股数量)`
- 参与股票：**起始日**已在成分（`in_date<=起点`）**且**区间末仍在（`delist_date>=终点`）**且起始日已上市**（`list_date<起点`，Tushare 新股 `in_date` 可能早于实际上市）；中段纳入 / 起始日未上市 / 区间末前退市均剔除；剔除告警**按类型汇总为一行**（数量+少量样例，避免刷屏）；筛选复用 `filter_stock_pool(股票池, 起点, 终点)`，其类别明细即告警数据源
- 个股区间收益 = 区间内各交易日官方涨跌幅（`close/pre_close`，口径见第 3 节）连乘，**包含起始日当天涨跌**，隐含基准 = 首个有行情日 `pre_close`；整段无行情的股票剔除；停牌日自动按 0% 累计（无需逐股回退）
- 权重锚定**区间起始日**：等权 = 起始成分简单平均；加权 = 起始日自由流通市值/总市值权重（停牌按 730 天回退，见第 5 节；仍取不到仅参与等权榜并告警）
- 网络策略：`trade_cal` 1 次 + 每交易日 `daily` 1 次 + 起始日 `daily_basic` 1 次 + 少量停牌回退（**非简单重复 N 次单日接口**）；逐日 `daily` 线程池并发（8 worker）+ 固定速率（`MAX_DAILY_FETCH_RATE=7.5 次/秒`≈450 次/分钟，留 10% 余量）平摊请求，避免瞬时爆发触发 429；单日失败重试 3 次，仍失败抛错（不静默）
- `timings`：`trade_cal`/`participate`/`daily_fetch`/`accumulate`/`mv_fetch`/`mv_fallback`/`compute`/`trading_days`；`progress_callback`：`(percent, message)` 阶段回调，不参与数值计算；`detail`：写入 `stock_ret`/`last_close`/`ts_code_to_free_mv`/`ts_code_to_total_mv` 供 Web 子表，传 `None` 行为与旧版一致

### 8. 申万官方指数算法（只读权威文档）与同步进度

- `docs/Shenwan_Index_Series_Algorithm_Text.md`：**申万官方行业指数计算方法的纯文字版**（申银万国股价系列指数算法，官方发布文本，随仓库提交）
- 该文件**只能阅读、禁止任何修改**（包括格式、文字、公式与错别字勘误）；确需变更必须先征求用户同意，由用户提供新版本覆盖
- **未来目标**：把项目内**所有市值类加权算法**（自由流通市值加权、总市值加权，单日榜与区间榜）逐步与官方算法**完全同步**
- **同步进度**：单独记录在独立子文档 `docs/sync_progress.md`（本文件只保留规则、不内嵌进度明细）；每完成一个章节的对照与同步，必须到该文件更新进度记录

## 强制核对流程（任务完成通知前必做）

1. 报告"完成"前，重新通读本文件「核心算法约定」、`docs/interface_notes.md`「接口交互明细」与 `docs/Shenwan_Index_Series_Algorithm_Text.md`（官方算法，只读）
2. 逐条对照交付与描述一致，核对点至少包括：涨跌幅是否仍由 `close/pre_close` 重算（尤其复权/除权除息）；等权平均公式与加权公式（`ΔM=M*p/(p+100)`、`M_pre=M/(1+p/100)`）；**市值类加权口径与官方算法文本的对照（第 8 节）**；停牌按 0% 计入与 730 天回退；股票池过滤规则；区间榜参与口径 / 连乘基准 / 起始日权重锚定（第 7 节）；Tushare 接口、参数、分页、token 获取（`docs/interface_notes.md`）
3. 发现不一致（无论本次引入还是历史遗留）**必须在最终回复中明确列出**，不得静默通过；涉及算法变更同步更新本文件并说明变更点
4. 本文件与代码冲突时以代码为准，但必须把冲突点报告给用户
