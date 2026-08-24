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
  - `data/`：需提交的数据/缓存子目录——`SW2021.json`（申万 2021 行业分类本地数据，推荐数据源，勿删）、官方指数日线可用性由服务启动后台探测（`sw_daily` 一次全市场拉取，内存缓存、无文件，见 `web/service.py` `prebuild_sw_daily_available`）
  - `__init__.py`：空
  - `web/`：本地 FastAPI Web 服务（`server.py` / `jobs.py` / `service.py` / `schemas.py` / `port_picker.py` + `static/`）与桌面启动器 `desktop.pyw`（WebView 直启，无中间过渡页）：单 worker 串行队列、前端轮询进度、任务取消、成分股子表、行业指数 K 线（`sw_daily` + 本地 ECharts；L1 全覆盖，L2/L3 可点击性规则见 `docs/known_issues.md` 第 17 条）；`port_picker.py` 端口自动顺延（首选端口被占/落系统保留段时 +1 逐个实测，server.py 与 desktop.pyw 共用，方案 B）
  - `docs/`：模块文档目录——`interface_notes.md`（Tushare 接口交互明细，**强制核对流程必读**）、`known_issues.md`（已知边界与易错点，32 条）、`roadmap.md`（未来规划）、`sync_progress.md`（官方算法同步进度，独立子文档，见第 8 节）、`Shenwan_Index_Series_Algorithm_Text.md`（**申万官方指数算法纯文字版，只读禁止修改，见第 8 节**）

## 运行环境与数据源

本模块**已彻底脱离 vnpy**（不 import 任何 vnpy 包），可用任一带 tushare/pandas 的 Python 运行；token 从本地配置 `config_store.py` 读取（Web 页面右上角「数据配置」填写保存，配置文件在项目根目录 `.quant-learning/settings.json`、已 gitignore 不随仓库提交；禁止硬编码）；需联网访问 Tushare Pro，接口权限依赖账号积分；日期统一 `YYYYMMDD`（内部用 `datetime`，`strftime("%Y%m%d")` 转换）。

## 核心算法约定（必读）

### 1. 行业树构建

- 默认 `build_industries()`：读本地 `data/SW2021.json` 逐行 `parse_industry_row()` 构建；备用 `build_industries_by_tushare()`（`pro.index_classify(src='SW2021')`），行业数据长期不变，本地优先
- `parse_industry_row()`：`level` 为字符串 `"L1"/"L2"/"L3"`；L1 挂到 root，其余按 `parent_code` 在 `industry_code_to_node` 找父节点挂接；登记 `index_code` / `industry_code` / `industry_name` 三种映射
- 全称 `industry_name_long`：L1=自身名；L2=父名+"-"+自身名；L3=父全称+"-"+自身名；父节点缺失直接 `KeyError`（当前未兜底）

### 2. 成分股加载与过滤

- `build_constituent_stocks_by_tushare(filter_unlisted=True)`：`pro.stock_basic` 一次拉取**上市(L)+退市(D)+暂停(P)** 三态股票（D 的 `delist_date` 记入 `ts_code_to_delist_date`）；**每次构建实时拉两次** `index_member_all`——默认（is_new=Y，当前成分）与 `is_new='N'`（历史退出，out_date 非空），合并为每股历史归属区间 `ts_code_membership`（`{ts_code: [(l3_code, in_date, out_date|None), ...]}`，按 in_date 升序；**不落盘、不缓存**）；当前成分(Y) 同时维护当前快照（`constituent_stock_to_l3_node` / 节点成分集合 / `ts_code_to_in_date`），历史退出(N) 只入 `ts_code_membership` / `all_member_codes`；不在 `stock_basic` 的股票跳过，Y 的 `l3_code` 找不到节点 → `ValueError`，N 的 l3 找不到节点则跳过并告警
- `filter_stock_pool(stock_pool, anchor_date, end_date, cancel_check=None)`：锚点/末日参数化（单日榜传 `(date,date)`，区间榜传 `(起始日,末日)`），返回剔除类别明细 `{类别: [ts_code]}`（`no_industry` / `not_member` / `left_mid_range` / `delisted` / `not_listed`）供区间榜汇总告警
  - 剔除 `no_industry_stocks`（本实例累计解析失败、无行业归属的股票）
  - 剔除 anchor 日**无覆盖归属区间**的成分（`not_member`：含未来纳入 `in_date>anchor` 与历史已退出 `out_date<=anchor`，由 `ts_code_membership` 判定，**消除"未来纳入"前视偏差并剔除历史退出残留**）
  - 区间模式额外剔除 anchor 日覆盖区间在 end 前已结束的股票（`left_mid_range`：区间末前已调出，满足"区间末仍在"口径）
  - 剔除 `delist_date < end` 的股票（**修复退市股被整体剔除的幸存者偏差**；退市日当天及之前正常参与）
  - 剔除未上市 / 上市未满 6 个交易日的股票（`not_listed`：官方 4.4.3 新股上市**第 6 个交易日**才纳入，注册制新股前 5 日无涨跌幅、波动剧烈；以 `list_date` 起计交易日，近 24 历日上市的新股用 `trade_cal` 精确数、实例内窗口缓存）
- 排名时股票池 = **当日行情股票 ∪ `all_member_codes`**（有(过)申万归属的全部股票）；归属解析 `get_stock_industry_nodes(ts_code, date)` **按历史区间取当日所属 L1/L2/L3**（有记录但当日不在任一行业则安静跳过，视为正常历史退出；无任何归属记录才记入无行业集合）

### 3. 行情获取与涨跌幅/自由流通市值口径（`market_data.MarketDataProvider` 方法）

- `get_ts_code_to_pct_chg(date)`：`pro.daily(trade_date, offset, limit=5999)` 循环拉全市场；**涨跌幅自行重算** `pct=(close−pre_close)/pre_close*100`（不用接口 `pct_chg` 字段）；按日期存内存缓存
- **涨跌幅口径（已实测验证）**：`daily.pre_close` 在除权除息日返回交易所**除权参考价**（实测：招行 20250711 除息 `46.24=48.24−2.0`；药明康德 20190702 除权 `64.66=(91.10−0.58)/1.4`），故 `close/pre_close` 与 `adj_factor` 比值法**等价**（实测 −1.2976% vs −1.2980%），无需额外拉取 `adj_factor`
- 该口径为**除权参考价（复权式）口径**：`close/pre_close` 对送转/除息/配股都无虚假跳变（参考价编码对应调整），**现金分红被中性化**（除息日按 0% 计、分红不计入收益），等价于复权/全收益式涨跌幅（分红视作再投资）。**注意与发布型价格指数的区别**：真正的价格指数（申万官方/上证）在除息日把派现当作下跌计入（`LV_t/LV_{t-1}^{Adj}` 用实际市值），与本口径在除息日差一个股息率。因此**单日榜等权与市值加权（自由流通/总市值）各提供两种口径**：`div_kind="price"`（官方价格式，默认，除息计入下跌，见第 4/5 节）与 `div_kind="reinvest"`（分红再投资/全收益式，原行为，可选）；区间榜仍全收益式。改动涨跌幅逻辑须保持该口径，如改用 `adj_factor`/复权行情须确认同一价格口径并同步更新本文件
- `get_ts_code_to_free_mv(date)`：`pro.daily_basic(ts_code='', trade_date, fields='ts_code,close,total_mv,free_share,float_share', offset, limit=5999)` 循环拉全市场（官方上限 6000）；**同一次请求同时缓存自由流通市值与总市值**（`get_ts_code_to_total_mv` 读同一缓存，零额外请求）；**自由流通市值 = `free_share × close`**（自由流通股本×收盘价，三字段取同一行、同一交易日，等价于旧式 `circ_mv × free_share / float_share`：恒等推导 `circ_mv = float_share × close`，实测 `daily_basic.close` 与 `daily.close` 逐股完全一致；`total_mv` 单位万元、`free_share`/`float_share` 单位万股、`close` 单位元，加权只用比值不影响结果）；股本字段防御：三字段任一缺失/非有限值、`close`/`free_share`/`float_share` ≤ 0 时该股不记入，等同无市值处理；**`free_share > float_share` 属 Tushare 自有口径（黑盒），视为正常、直接采信 `free_share × close`**（实测 2026-07 多只股票长期 free>float、如 001216 比例 1.43——无法获知背后扣减明细，不排除不回退，2026-08-23 策略调整）；按日期存内存缓存。**口径来源说明**：`free_share` 为 Tushare 自有自由流通口径（黑盒返回最终股本，无法获取扣减明细），与官方《编制说明》附录二六类扣减定义**无法逐条核对**，本模块以 Tushare 口径为准（见 `docs/known_issues.md` 第 18 条）

### 4. 单日等权涨幅 `daily_rank_equal_weight(tree, market_data, date)`

- 股票池过滤见第 2 节；逐只取 L1/L2/L3 节点，解析失败跳过（记入 `no_industry_stocks`）
- 单股涨跌幅：有行情用实际值；**停牌（行情缺失）按 0% 计入，且计入成分股数量**
- 数据异常（涨跌幅非有限值）：记为 None 并告警，**直接跳过，不计入平均与数量**（与停牌按 0% 不同）
- 行业涨幅 = 成分股涨跌幅简单算术平均（增量平均 `new_avg=(old_avg*old_count+pct)/(old_count+1)`），L3/L2/L1 三级同时累计
- 单股涨跌幅有两种口径（`div_kind`，见第 5 节）：`"reinvest"`=分红再投资/全收益式（原行为，`close/pre_close`，除息中性）；`"price"`=官方价格式（默认，**除息股实际市值比** `今日 total_mv/昨日 total_mv − 1`，除息计入下跌；纯派现等价 raw close 比、捆绑送转+派现时送转部分价值中性，用总市值比避免解禁/股本变动对 free_share 的干扰；取不到两日市值时回退 `close/pre_close`）。**增量平均按当日混合口径累计，不改累计公式**
- 排序：剔除 `count==0` 的行业，按涨幅降序；返回 `(index_code, pct, count)` 列表（三级各一个）

### 5. 单日市值加权涨幅 `daily_rank_float_weight(tree, market_data, date, mv_kind="free")`

- `mv_kind`：`"free"`=自由流通市值加权、`"total"`=总市值加权；同一套公式、市值来源参数化（**不要复制两套代码**）
- `div_kind`（对 `mv_kind=="free"` 与 `"total"` 均有效）：`"price"`=官方价格式（默认，除息计入下跌）、`"reinvest"`=分红再投资/全收益式（除息中性，原行为）。**除息日官方价格式**：M_pre 不再用 `pre_close×q_t`，而用**昨日实际市值** `M_pre = 昨日 mv = close_{t-1}×股本_{t-1}`（自由流通用 free_mv、总市值用 total_mv，=官方 `LV_{t-1}^{Adj}`；`market_data.get_ex_div_cash(date)` 识别当日除息股、`get_ts_code_to_free_mv(T-1)`/`get_ts_code_to_total_mv(T-1)` 取昨日市值，同一请求同时缓存两者、零额外费用），`ΔM = M − M_pre`。已按官方公式数值校验（纯除息与捆绑送转+派现、两日股本不同均严格等于 `LV_t/LV_{t-1}^{Adj}`）；其余事件（送转/配股/解禁/普通）仍 `pre_close×q_t`（与官方一致）。`reinvest` 式即原逻辑
- 单股单级公式（M=当日收盘权重市值，p=当日涨跌幅%）：当日新增市值 `ΔM=M*p/(p+100)`；开盘前市值 `M_pre=M/(1+p/100)`；行业涨幅 = `ΣΔM/ΣM_pre*100`（三级分别累计后取比值）
- 停牌处理（本模块最特殊逻辑）：当天 `daily_basic` 无自由流通市值时回退查询 `daily_basic(ts_code, fields='trade_date,close,total_mv,free_share,float_share', start_date, end_date)`，一次请求同时回退自由流通/总市值（`resolve_free_mv` 返回 free 并顺带缓存 total，`resolve_total_mv` 优先读缓存）并回填缓存；**自由流通市值三字段必须取自同一行（同一交易日）**计算，避免混搭不同日期股本；**自由流通市值是决定性字段**（以 free 为准，避免 total 命中却漏掉 free）。回退策略（`MV_RESOLVE_MODE`，默认 `new`，可用 `SW_MV_RESOLVE_MODE=legacy` 切回对比）：
  - **`new`（默认）**：每股先近 730 天窗口、按 **limit 阶梯（1 → 100 行，响应降序取最近，极小 payload）** 命中自由流通市值；未命中则**全窗回到上市日（19900101 起）——尽量不放弃任何股票**，只有整个上市期都没有 `daily_basic` 数据才跳仅等权榜并汇总告警；仅字段缺失/非正行会向前找正常行（`free_share>float_share` 比例越界行现视为正常、直接采信，见上）
  - **`legacy`（保留以对比耗时）**：旧行为——固定前 730 天窗口全量扫描，超 2 年停牌取不到 → 仅参与等权榜并告警
  - **并发解析**：缺失市值股票的逐股回退查询由 `resolve_missing_mv` 用线程池（`MV_RESOLVE_WORKERS`，默认 8）并发补齐并写缓存，把 N 次串行网络往返压到 ~N/workers 倍（实测 2026-07 区间首查 mv 阶段 18.6s → ~2s）；曾评估"批量回填"方案，实测冗余/更差，已移除
- 停牌股涨幅按 0% 计 → `ΔM=0`，但 `M_pre=M` 仍计入分母（稀释行业涨幅）；数据异常跳过规则与等权一致，回退扫描跳过 NaN 行取最近有效值
- 其余规则（股票池、节点解析、排序、返回结构）与等权一致

### 6. 输出与示例

- 入口脚本从 `config_store.get_token()` 取 token → 构建树/加载成分 → 计算 → 按 L3/L2/L1 打印六列涨幅（总市值加权·官方价格/总市值·分红再投资/自由流通市值加权·官方价格/自由流通·分红再投资/等权·官方价格/等权·分红再投资）、全称、成分股数量与名称；对每只行业指数用 `-100` 作"等权缺失"哨兵校验，命中即报错（单日示例日期硬编码 `2025-04-07`；区间在 `range_ranking.py` 内 `RANGE_START`/`RANGE_END` 配置）
- 运行结束输出**耗时分析**（组小计、各阶段耗时/占比、总耗时与 API 调用次数）：耗时统计 `print_timing`，API 次数由 `MarketDataProvider.snapshot_api_calls()` 提供（构造时即包装计数，含建树阶段）；大阶段按"接口拉取 vs 本地计算/回退"拆分
- 单日榜入口（CLI 与 Web `service._run_daily`）统一走 `run_daily_ranking`（拉行情/市值 → 等权（官方价格式）→ 等权（分红再投资式）→ 自由流通（官方价格式）→ 自由流通（分红再投资式）→ 总市值（官方价格式）→ 总市值（分红再投资式），避免两套编排漂移），**返回 7 元组 `(等权·官方价格式, 等权·分红再投资式, 自由流通·官方价格式, 自由流通·分红再投资式, 总市值·官方价格式, 总市值·分红再投资式, timings)`**；timings key：`daily_fetch`/`mv_fetch`/`equal_compute`/`equal_tr_compute`/`float_compute`/`float_fallback`/`float_tr_compute`/`total_compute`/`total_fallback`/`total_tr_compute`/`total_tr_fallback`；进度回调 `(0~100, 说明, 阶段名)`（阶段名供 Web 前端展示）
- 模块暂无图片产物，仅控制台输出

### 7. 区间累计涨幅榜 `rank_range(tree, market_data, start_date, end_date, timings=None, progress_callback=None, detail=None)`

- 返回 `(等权(l1,l2,l3), 自由流通市值加权(l1,l2,l3), 总市值加权(l1,l2,l3))`，榜单项 `(index_code, 涨跌幅%, 成分股数量)`
- 参与股票：**起始日**已在成分（`in_date<=起点`）**且**区间末仍在（`delist_date>=终点`）**且起始日已上市**（`list_date<起点`，Tushare 新股 `in_date` 可能早于实际上市）；中段纳入 / 起始日未上市 / 区间末前退市均剔除；剔除告警**按类型汇总为一行**（数量+少量样例，避免刷屏）；筛选复用 `filter_stock_pool(股票池, 起点, 终点)`，其类别明细即告警数据源
- 个股区间收益 = 区间内各交易日官方涨跌幅（`close/pre_close`，口径见第 3 节）连乘，**包含起始日当天涨跌**，隐含基准 = 首个有行情日 `pre_close`；整段无行情的股票剔除；停牌日自动按 0% 累计（无需逐股回退）
- 权重锚定**区间首日盘前市值**（`M_pre = 首日 pre_close×股本`，= 首日上一交易日调整后市值，与单日榜 reinvest 式 M_pre 同一口径，见第 5 节；实现为 收盘市值×(pre_close/close) 折算，零额外请求）：等权 = 起始成分简单平均；加权 = 首日盘前自由流通/总市值权重（首日停牌按 730 天回退，回退市值即盘前市值、不折算；仍取不到仅参与等权榜并告警）
- 网络策略：`trade_cal` 1 次 + 每交易日 `daily` 1 次 + 起始日 `daily_basic` 1 次 + 少量停牌回退（**非简单重复 N 次单日接口**）；**按接口独立的请求节流器**（`MarketDataProvider._acquire_rate_slot(接口名)`，`daily`/`daily_basic`/`dividend` 各持一把锁，同接口内批拉、分页、停牌点查、8 worker 并发补齐共享 450 次/分钟，为 Tushare **每接口** 500 次/分钟上限留 10% 余量；**不同接口限额互相独立、可并行**——链式区间榜行情/市值/除息三池并发预取的基础）；单日失败重试 3 次，仍失败抛错（不静默）。**注意：限流器是进程内的，同一 token 下 Web 与 CLI 同时跑任务时各自计数、互不协调（实测超限报错多源于此），避免并发使用**
- `timings`：`trade_cal`/`participate`/`daily_fetch`/`accumulate`/`mv_fetch`/`mv_fallback`/`compute`/`trading_days`；`progress_callback`：`(percent, message)` 阶段回调，不参与数值计算；`detail`：写入 `stock_ret`/`last_close`/`ts_code_to_free_mv`/`ts_code_to_total_mv` 供 Web 子表，传 `None` 行为与旧版一致
- **静态版保留为对照模式**（Web 区间查询默认官方逐日链、无选择 UI；静态版仅 API `chain=false` 或 CLI `range_ranking.py` 的 `RANGE_CHAIN` 双输出可看）；与官方指数的精确对齐如下面的逐日链式

### 7.2 官方逐日链式区间榜 `rank_range_chain(tree, market_data, start_date, end_date, ...)`

- **每日再平衡**（= 区间形态的自建指数引擎，`LV_T/LV_{t0-1}^{Adj}` 链）：区间内每个交易日按**当日**成分（逐日过滤，新股纳入/退市即单日榜口径，不再锚定起始日行业）与当日盘前市值权重，复用单日榜同款日级函数算 **6 条序列**（等权/自由流通/总市值 × 官方价格式/全收益式），逐行业连乘 `Π(1+pct/100)` 得区间累计
- 返回 `(等权·价格, 等权·全收益, 自由流通·价格, 自由流通·全收益, 总市值·价格, 总市值·全收益)`（各为 L1/L2/L3 榜单）；`_build_levels` 两分支同构，前端不用区分
- **数据/性能约定**：`daily_basic`/`dividend`/行情三池**并行**预取（`fetch_daily_batch`/`fetch_mv_batch`/`fetch_ex_div_batch` 同时提交，各接口独立 450 次/分钟节流，并行总时长 ≈ 单段时长；`fetch_daily_batch` 已回填 pct/close 缓存，逐日命中零请求）；**日历跨度切片缓存**（`market_data.get_trading_days` 与 `tree._trading_days_window` 预取宽区间后子窗口切片命中，除息日 12 天窗口与新股 6 交易日门槛的逐日查询全部归零）；**停牌股跨日复用 memo**——当日不在全市场市值数据中的参与股沿用最近一次已知市值（停牌期间必然不变、零重复点查），memo 仅对**当日参与股票**（逐日过滤后）生效，避免为退市已久/尚未上市的历史成分发起无谓点查；复牌/新上市日由全市场数据自动刷新
- 实测（2024-09-24~2024-12-31，66 交易日）：链式总计约 **24 秒**（预取三池并行 9.3s + 逐日 6 序列聚合纯 CPU 12.2s + 首日点查 2.4s）；默认月区间（23 天，2026-07）约 **11 秒**；`trade_cal` 仅 3 次（预取+切片）、`daily_basic` 66+点查（66 全市场 + 11 停牌点查）、`dividend` 66 次（预取）；与申万官方指数 L1 区间涨幅对照 31/31 行业平均差 0.44pp、最大 1.09pp（差异来源=Tushare 自由流通口径 vs 官方附录二扣减明细，见 known_issues 第 18 条）
- `timings`：`trade_cal`/`prefetch`(三池并行总时长)/`daily_fetch`/`mv_prefetch`/`ex_prefetch`/`accumulate`/`mv_resolve`/`compute`/`trading_days`；`detail` 语义与静态版一致（`ts_code_to_*` 为首日盘前市值）

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
