# shenwan_industry 模块说明（算法与接口约定）

本文件是申万行业模块的**算法权威描述**。涉及本模块的任何任务（新增、修改、运行、排查、文档），在向用户报告完成之前，必须先对照本文件核对一致性，具体要求见文末「强制核对流程」。

## 模块职责与文件

- 模块内容：申万 2021 三级行业分类树 + 行业涨幅榜（单日榜 / 区间累计榜，等权 / 自由流通市值加权 / 总市值加权）+ **单日榜财务指标估值列**（当前为 **PE**（净利润口径四选一：归母 / 扣非 × TTM / 动态，四口径**一次全算**、默认归母-TTM）、**PB**（归母普通股股东权益，balancesheet_vip 权威绝对额，各提供自由流通 / 总市值两种合成口径）、**净利润同比**（同四口径、无市值维度单列）、**ROE**（加权平均算法、同四口径、**按市值权重加权算术平均**：自由流通/总市值双口径随加权方式切换、等权显示"—"，"ROE算法"下拉当前仅加权一档）与 **股息率**（总额法 DPS 双口径：TTM估算值[默认]/静态，"股息率口径"下拉切换、**按市值权重加权**：双市值口径随加权方式切换、等权显示"—"，2026-08-28 新增），见第 5.1 节与 `docs/financial_indicators.md`），当前为控制台输出脚本（与 `holders/` 生成图片不同）
- 文件：
  - `industry_tree.py`：行业树与成分数据层（`ShenWanIndustryNode` / `ShenWanIndustryTree`：树构建、成分加载、`in_date`/`delist_date` 记录、股票池过滤 `filter_stock_pool`）
  - `market_data.py`：行情数据层 `MarketDataProvider`（API 调用计数、按日缓存、停牌 730 天回退、交易日历、区间逐日行情并发限流拉取；**财务数据三池批拉：`fina_indicator_vip`（扣非 `profit_dedt` + 非经常性损益 `extra_item` + 每股净资产 `bps`，归母净利润 = 前两者行内合成；TTM 归母 / TTM 扣非 / 动态归母 / 动态扣非四口径，动态 = 最新期累计 × 4/k 年化、与 TTM 同批数据零新增请求）、`balancesheet_vip`（归母普通股股东权益绝对额，供 PB）与 `express_vip`（业绩快报，归母净利润提前可用源）三池并行预热，PIT 均为 ann_date**，见第 5.1 节）
  - `industry_ranking.py`：排行榜算法库（`daily_rank_equal_weight` / `daily_rank_float_weight` / `daily_valuation_metric`（PE/PB 通用聚合，`daily_pe`/`daily_pb` 薄封装）/ `daily_profit_growth`（净利润同比，TTM/动态两式）/ `daily_roe`（ROE 加权平均，四口径整体法）/ `daily_dividend_yield`（股息率，总额法 DPS 双口径整体法）/ **`start_metric_prefetch` + `compute_fin_metric_suite`（财务指标全套公共编排：预热启动 + PE 四口径/PB/同比四口径/ROE/股息率一次算出，单日榜与区间链式榜共用）** / `run_daily_ranking` / `rank_range` / `rank_range_chain`）+ 耗时工具 `print_timing`
  - `dividend_data.py`：分红数据层与股息率计算（`DividendHistory` 每股分红事件持久缓存[data/dividend_history.json，首刷全市场约 12 分钟一次性、增量 ann_date 日历日+ex_date 交易日双通道逐日探测+新成分补拉、--full=忽略现有缓存全量重拉、拉取均 offset/limit 分页防御] + `compute_dividend_dps` 总额法 DPS 双口径；事件级级联实施>预案>无、财年归属 end_date 年份前缀、锚/完整性三态/7-31 推定，规则见第 5.1 节与 `docs/financial_indicators.md` 第 7 节）
  - `dividend_cache.py`：分红缓存构建与体检入口脚本（首刷 `--full` / 增量 / `--check` 个股抽查；日常无需运行，单日榜自动增量）
  - `daily_ranking.py` / `range_ranking.py`：单日 / 区间榜入口脚本（含耗时分析输出）
  - `data/`：需提交的数据/缓存子目录——`SW2021.json`（申万 2021 行业分类本地数据，推荐数据源，勿删）、`dividend_history.json`（每股分红事件持久缓存，供股息率，勿删）、官方指数日线可用性由服务启动后台探测（`sw_daily` 一次全市场拉取，内存缓存、无文件，见 `web/service.py` `prebuild_sw_daily_available`）
  - `__init__.py`：空
  - `web/`：本地 FastAPI Web 服务（`server.py` / `jobs.py` / `service.py` / `schemas.py` / `port_picker.py` + `static/`）与桌面启动器 `desktop.pyw`（WebView 直启，无中间过渡页）：单 worker 串行队列、前端轮询进度、任务取消、成分股子表（**单日榜与区间链式榜均含个股 PE/PB/ROE/股息率/净利润同比列**——区间口径=区间末交易日时点值（`_compute_stock_metrics` 公共编排，外层主表已注明、子表不重复），PE/PB 总市值口径、随"净利润口径"四档下拉切换（ROE 另随"ROE算法"下拉、股息率随"股息率口径"下拉）、静态版区间榜为空显示"—"，见第 5.1 节）、行业指数 K 线（`sw_daily` + 本地 ECharts；L1 全覆盖，L2/L3 可点击性规则见 `docs/known_issues.md` 第 17 条）；`port_picker.py` 端口自动顺延（首选端口被占/落系统保留段时 +1 逐个实测，server.py 与 desktop.pyw 共用，方案 B）
  - `docs/`：模块文档目录——`interface_notes.md`（Tushare 接口交互明细，**强制核对流程必读**）、`known_issues.md`（已知边界与易错点，42 条）、`roadmap.md`（未来规划）、`sync_progress.md`（官方算法同步进度，独立子文档，见第 8 节）、`financial_indicators.md`（**单日榜财务指标算法唯一权威文档**：PE / PB / ROE / 股息率，未来指标一律写入，见第 5.1 节）、`Shenwan_Index_Series_Algorithm_Text.md`（**申万官方指数算法纯文字版，只读禁止修改，见第 8 节**）

## 运行环境与数据源

本模块**已彻底脱离 vnpy**（不 import 任何 vnpy 包），可用任一带 tushare/pandas 的 Python 运行；token 从**仓库根公共模块** `config_store.py` 读取（与 `holders/` 共享同一份本地配置：项目根目录 `.quant-learning/settings.json`、已 gitignore 不随仓库提交；Web 页面右上角「数据配置」填写保存，CLI 未配置时报错提示先在 Web 配置；禁止硬编码）；需联网访问 Tushare Pro，接口权限依赖账号积分；日期统一 `YYYYMMDD`（内部用 `datetime`，`strftime("%Y%m%d")` 转换）。

## 核心算法约定（必读）

### 1. 行业树构建

- 默认 `build_industries()`：读本地 `data/SW2021.json` 逐行 `parse_industry_row()` 构建；备用 `build_industries_by_tushare()`（`pro.index_classify(src='SW2021')`），行业数据长期不变，本地优先
- `parse_industry_row()`：`level` 为字符串 `"L1"/"L2"/"L3"`；L1 挂到 root，其余按 `parent_code` 在 `industry_code_to_node` 找父节点挂接；登记 `index_code` / `industry_code` / `industry_name` 三种映射
- 全称 `industry_name_long`：L1=自身名；L2=父名+"-"+自身名；L3=父全称+"-"+自身名；父节点缺失直接 `KeyError`（当前未兜底）

### 2. 成分股加载与过滤

- `build_constituent_stocks_by_tushare(filter_unlisted=True)`：`pro.stock_basic` 一次拉取**上市(L)+退市(D)+暂停(P)** 三态股票（D 的 `delist_date` 记入 `ts_code_to_delist_date`）；**每次构建实时拉两次** `index_member_all`——默认（is_new=Y，当前成分）与 `is_new='N'`（历史退出，out_date 非空），合并为每股历史归属区间 `ts_code_membership`（`{ts_code: [(l3_code, in_date, out_date|None), ...]}`，按 in_date 升序；**不落盘、不缓存**）；当前成分(Y) 同时维护当前快照（`constituent_stock_to_l3_node` / 节点成分集合 / `ts_code_to_in_date`），历史退出(N) 只入 `ts_code_membership` / `all_member_codes`；不在 `stock_basic` 的股票跳过，Y 的 `l3_code` 找不到节点 → `ValueError`，N 的 l3 找不到节点则跳过并告警
- `filter_stock_pool(stock_pool, anchor_date, end_date, cancel_check=None, restructure_excluded=None)`：锚点/末日参数化（单日榜传 `(date,date)`，区间榜传 `(起始日,末日)`），返回剔除类别明细 `{类别: [ts_code]}`（`no_industry` / `not_member` / `left_mid_range` / `delisted` / `not_listed` / `restructure_window`）供区间榜汇总告警
  - 剔除 `no_industry_stocks`（本实例累计解析失败、无行业归属的股票）
  - 剔除 anchor 日**无覆盖归属区间**的成分（`not_member`：含未来纳入 `in_date>anchor` 与历史已退出 `out_date<=anchor`，由 `ts_code_membership` 判定，**消除"未来纳入"前视偏差并剔除历史退出残留**）
  - 区间模式额外剔除 anchor 日覆盖区间在 end 前已结束的股票（`left_mid_range`：区间末前已调出，满足"区间末仍在"口径）
  - 剔除 `delist_date < end` 的股票（**兜底**：官方成分 `out_date`（退市整理期首日，有整理期时）已由上方 `not_member` 先行处理；`delisted` 修复退市股被整体剔除的幸存者偏差，退市日当天及之前正常参与，见 `docs/sync_progress.md` 4.4.3）
  - 剔除未上市 / 上市未满 6 个交易日的股票（`not_listed`：官方 4.4.3 新股上市**第 6 个交易日**才纳入，注册制新股前 5 日无涨跌幅、波动剧烈；以 `list_date` 起计交易日，近 24 历日上市的新股用 `trade_cal` 精确数、实例内窗口缓存）
- 剔除当日处于 4.4.14 重整转增窗口的股票（`restructure_window`：**储备功能、默认关闭**，`SW_RESTRUCTURE_ENABLED=1` 启用；4.4.14 实际以官方成分断点为准——官方把"除权日退出/重入日计入"编码为 `index_member_all` 的 out_date/in_date，由上方 `not_member`/`left_mid_range` 自动处理，见 `docs/sync_progress.md` 4.4.14）
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
- 单股涨跌幅两种口径（`div_kind`）的定义见第 3 节；等权 `"price"` 式的关键差异：**除息股实际市值比** `今日 total_mv/昨日 total_mv − 1`（纯派现等价 raw close 比、捆绑送转+派现时送转部分价值中性，用总市值比避免解禁/股本变动对 free_share 的干扰；取不到两日市值时回退 `close/pre_close`），`"reinvest"` 即原 `close/pre_close`。**增量平均按当日混合口径累计，不改累计公式**
- 排序：剔除 `count==0` 的行业，按涨幅降序；返回 `(index_code, pct, count)` 列表（三级各一个）

### 5. 单日市值加权涨幅 `daily_rank_float_weight(tree, market_data, date, mv_kind="free")`

- `mv_kind`：`"free"`=自由流通市值加权、`"total"`=总市值加权；同一套公式、市值来源参数化（**不要复制两套代码**）
- `div_kind` 对 `mv_kind=="free"` 与 `"total"` 均有效（定义见第 3 节）。**除息日官方价格式**：M_pre 不再用 `pre_close×q_t`，而用**昨日实际市值** `M_pre = 昨日 mv = close_{t-1}×股本_{t-1}`（自由流通用 free_mv、总市值用 total_mv，=官方 `LV_{t-1}^{Adj}`；`market_data.get_ex_div_cash(date)` 识别当日除息股、`get_ts_code_to_free_mv(T-1)`/`get_ts_code_to_total_mv(T-1)` 取昨日市值，同一请求同时缓存两者、零额外费用），`ΔM = M − M_pre`。已按官方公式数值校验（纯除息与捆绑送转+派现、两日股本不同均严格等于 `LV_t/LV_{t-1}^{Adj}`）；其余事件（送转/配股/解禁/普通）仍 `pre_close×q_t`（与官方一致）。`reinvest` 式即原逻辑
- 单股单级公式（M=当日收盘权重市值，p=当日涨跌幅%）：当日新增市值 `ΔM=M*p/(p+100)`；开盘前市值 `M_pre=M/(1+p/100)`；行业涨幅 = `ΣΔM/ΣM_pre*100`（三级分别累计后取比值）
- 停牌处理（本模块最特殊逻辑）：当天 `daily_basic` 无自由流通市值时回退查询 `daily_basic(ts_code, fields='trade_date,close,total_mv,free_share,float_share', start_date, end_date)`，一次请求同时回退自由流通/总市值（`resolve_free_mv` 返回 free 并顺带缓存 total，`resolve_total_mv` 优先读缓存）并回填缓存；**自由流通市值三字段必须取自同一行（同一交易日）**计算，避免混搭不同日期股本；**自由流通市值是决定性字段**（以 free 为准，避免 total 命中却漏掉 free）。回退策略（`MV_RESOLVE_MODE`，默认 `new`，可用 `SW_MV_RESOLVE_MODE=legacy` 切回对比）：
  - **`new`（默认）**：每股先近 730 天窗口、按 **limit 阶梯（1 → 100 行，响应降序取最近，极小 payload）** 命中自由流通市值；未命中则**全窗回到上市日（19900101 起）——尽量不放弃任何股票**，只有整个上市期都没有 `daily_basic` 数据才跳仅等权榜并汇总告警；仅字段缺失/非正行会向前找正常行（`free_share>float_share` 比例越界行现视为正常、直接采信，见上）
  - **`legacy`（保留以对比耗时）**：旧行为——固定前 730 天窗口全量扫描，超 2 年停牌取不到 → 仅参与等权榜并告警
  - **并发解析**：缺失市值股票的逐股回退查询由 `resolve_missing_mv` 用线程池（`MV_RESOLVE_WORKERS`，默认 8）并发补齐并写缓存，把 N 次串行网络往返压到 ~N/workers 倍（实测 2026-07 区间首查 mv 阶段 18.6s → ~2s）；曾评估"批量回填"方案，实测冗余/更差，已移除
- 停牌股涨幅按 0% 计 → `ΔM=0`，但 `M_pre=M` 仍计入分母（稀释行业涨幅）；数据异常跳过规则与等权一致，回退扫描跳过 NaN 行取最近有效值
- 其余规则（股票池、节点解析、排序、返回结构）与等权一致

### 5.1 单日榜财务指标估值列（PE / PB / 净利润同比 / ROE / 股息率，项目自建口径）

**官方算法文本无估值章节，本口径为项目自建**，完整方法见 `docs/financial_indicators.md`（本文件为规则摘要；**未来新增财务指标（如 PS）一律写入该文档对应分节**）：

- **产出**：单日榜每只指数（L1/L2/L3）每个指标提供两种市值口径——PE 为 `pe_{basis}_float`/`pe_{basis}_total`（basis ∈ 归母/扣非 × TTM/动态四口径，`industry_ranking.PROFIT_BASES` 的 basis id：`attr_ttm`/`attr_dynamic`/`deduct_ttm`/`deduct_dynamic`，四口径**一次全部算出**、共享财务缓存零新增请求、默认 `attr_ttm` 归母-TTM）、PB 为 `pb_float`/`pb_total`（接口都返回，CLI 打印的 PE 两列为默认口径归母-TTM）；**Web 主表每个指标一列、随加权方式切换市值口径**（总市值/自由流通 → 对应口径，等权 → 显示"—"），**PE 与净利润同比两列另随"净利润口径"下拉切换四档**（默认归母-TTM，PB 与该下拉无关）；成分股子表同 `pe_{basis}`/`profit_growth_{basis}` 四字段切换；无 `div_kind` 维度（与除息日涨跌幅口径无关）；**区间链式榜同样提供全套指标（口径=区间末交易日时点值，涨幅区间累计+指标末收盘快照，与单日榜共用公共编排 `compute_fin_metric_suite`/`start_metric_prefetch`，Web 区间表头注明时点口径；静态版区间榜不计算保持对照轻量）**；**动态口径**：动态净利润 = 最新报告期累计利润 × 4/k（k=该期覆盖季度数，Q1→1/中报→2/三季报→3/年报→4，与 Tushare `daily_basic` 动态市盈率同法，即把 TTM 不足四期兜底式用于全体股票；最新期为年报时动态=年报值=TTM 退化式，年报披露后至下期季报披露前动态 PE ≡ TTM PE），归母动态含快报双源合并、扣非动态纯财报；**动态同比** = 最新期累计 / 去年同季累计 − 1（同相位对比，数学上等价"动态值/去年同期年化值"，与 Tushare `netprofit_yoy` 同口径；**不用**"动态(D)/动态(D-1年)"——两时点最新期披露节奏可能错位引入失真），去年同季优先取主窗口 D 视角审定值（其披露日可能晚于 D-1年，从基期窗口取会被 PIT 过滤）、停披超一年的股票回落基期预热窗口兜底，零新增请求
- **数据**：`fina_indicator_vip` 按报告期全市场批拉，**一次请求同取** `profit_dedt`（归属母公司扣非净利润，**年初至今累计值**，元）、`extra_item`（非经常性损益，自带正负号，元）与 `bps`（每股净资产，**报告期末时点值**，元），字段 `ann_date`=公告日；**归母净利润不在该接口（实测 `n_income_attr_p` 被静默忽略），由恒等式行内合成 `归母 = profit_dedt + extra_item`（仅同行两字段齐备才算出、不跨行拼接；全市场实测 20250630 可对齐 6243 只、99.8% 相对误差 <0.1%、有扣非时 extra 缺失率 0%）；PE 四口径 = 归母/扣非 × TTM/动态（`get_ts_code_to_ttm_attr_profit` / `get_ts_code_to_ttm_deducted_profit` / `get_ts_code_to_dynamic_profit`，全部同批数据零新增请求）**；limit 参数生效且上限远高于 daily（实测 9999/20000 整批无截断），offset/limit=9999 分页循环（全量单期 8808 行 → 每期 1 页）；**8 期并发拉取**（8 线程、同一节流器错开开始时刻、往返并行，实测 ~1.4 秒，见 interface_notes）；**单日榜编排最开始即以后台线程预热财务批拉**（`prefetch_fina_indicators`：fina/balancesheet/express 三池并行，与行情/市值拉取及六条涨幅序列计算全程并行、接口限流独立、仅写各自财务缓存），PE/PB/增长阶段 join 命中缓存——拉取耗时从墙体时间中隐藏；**同股票同报告期有重复行（含 NaN 行），去重为字段级各自取最后非空**（实测 bps 两行均有效但值不同 56.7800/56.7751，不能整行丢弃）；fields 指定不存在的字段名**静默忽略**，须 `getattr` 防御；每接口独立节流（7.5/s）
- **净利润同比（TTM 与动态两式、口径与 PE 分子同源）**：TTM 式 `增长 = TTM(D) / TTM(D-1年) − 1`，基期 = 同日历日去年（`growth_base_date`，2/29 回退 2/28）走**同一套**归母 TTM 机制（含 express 双源合并/PIT/4/k 兜底，报告期窗口自动落到 [D-36月, D-12月]，预热由 `prefetch_fina_indicators(growth_base_date=...)` 在主日期之后串行补拉——共享报告期命中 period 缓存、实测仅多 4 期 +8 次请求，旧报告期行数触顶 9999 自动翻页）；动态式 `增长 = 最新期累计 / 去年同季累计 − 1`（同相位对比；去年同季优先取主窗口 D 视角审定值、停披超一年回落基期窗口兜底，零新增请求）；**行业合成 = Σ 当期 / Σ 基期 − 1**（与 PE 的 Σ市值/Σ股东值 同构，亏损股不剔除、负值参与合计）；**参与 = 两期值均有的成分股（both-or-neither**，缺基期的新股不进分子分母、计入 stats `stocks_no_base`）；**四级显示与排序**（`classify_profit_growth`，个股与行业 Σ 同一规则）：数值 ∈[−100%,∞)（**分档显示：≥100% 用 +x.xx 倍、否则带符号两位小数 %**，仅数值着色红涨绿跌、类别文本不着色；排序仍按真实数值不受显示分档影响）/ "扭亏"（基期≤0 当期>0，排序位最高）/ "转亏"（基期>0 当期≤0）/ "持续亏损"（两期均≤0，最低），无数据键缺失显示"—"恒置底；Web 排序经 `growthSortValue` 把类别映射为 ±1e12 哨兵数值；基期为"当前快照回看"（含此后更正，与 PE 历史回看同口径）；接口字段 `profit_growth_{basis}`、列名（CLI 与 Web）"净利润同比"
- **ROE（加权平均算法，`get_ts_code_to_roes` + `daily_roe`，2026-08-28 新增）**：算法=证监会《编报规则第 9 号》加权平均 ROE，**数据锚为 `fina_indicator_vip` 同批带回的披露值 `roe_waa`**（随现有批拉零新增请求、实测覆盖 98.2%）；官方加权分母反推 `E_waa = 归母累计×100 ÷ roe_waa`（自建 9 号逐笔事件项在本项目数据源下不可拼全——增发无接口、OCI 无逐笔数据，披露值天然含全部事件项）；四口径与 PE/净利润同比的 `PROFIT_BASES` 联动——`attr_dynamic = roe_waa×4/k`、`deduct_dynamic = (扣非累计×4/k)÷E_waa(P)`、`attr_ttm = 纯财报归母 TTM ÷ E_TTM`（不经 express）、`deduct_ttm = 扣非 TTM ÷ E_TTM`；**TTM 分母分段推导 `E_TTM = E_waa(A) + (E_waa(P)−E_waa(S))÷2`**（TTM 区间=去年下半年+今年年初至今的分段展开，最新期为年报时 S=A 退化为上下半年平均；A/S 缺失或推导非正兜底 E_waa(P)，与 TTM 分子 4/k 兜底配套）；**全链不接业绩快报**（express 无 roe_waa，分子用快报会与披露分母期次错配；年报季时效落后 PE/同比一档，同档"归母-TTM"下 PE 含快报而 ROE 不含）；**roe_waa 缺失（实测约 1.8%）或反推异常 → 四口径全部降级"—"不做简化式兜底**（实测简单平均分母与官方加权分母偏差中位 2.9%、1/3 公司 >5%）；行业合成=**按当日市值权重的加权算术平均 Σ(市值×ROE)÷Σ市值**（自由流通/总市值双口径随加权方式切换、等权与 PE/PB 一致显示"—"；个股负 ROE 正常参与加权；显示上正数不带+号不着色、负值显示"亏损"——与 PE 的"亏损"同文案但来源不同：PE 为行业 None、ROE 为负值类别化，排序仍按真实负值）；主表接口字段 `roe_waa_{basis}_float`/`_total`（子表个股纯比值无市值段 `roe_waa_{basis}`；键带算法段供将来扩展）；完整公式、数据可得性实测记录与边界见 `docs/financial_indicators.md` 第 6 节
- **股息率（总额法 DPS 双口径，`dividend_data` + `daily_dividend_yield`，2026-08-28 新增）**：**不用 Tushare 现成股息率字段**（`dv_ratio`/`dv_ttm` 逐日实测为"滚动窗口混合财年"口径，一次转多次场景连续数月高估 43~61%），自建 = **财年锚定 + 总额法**——事件按 `end_date` 年份前缀归财年（1231 年度/0630 中期/0930 三季度/非报告期=特别分红），每股口径 = Σ(每股派现×事件基准股本 base_share)÷当前总股本（财年内送转正确处理）；**事件级级联实施>预案>无、只认实施与预案两态**（div_proc 实际 5 态；"股东大会通过"行 ann_date 被回填且携带后到修订金额=PIT 脏[中移动实测]，绝不使用；停止实施行作废其之前预案；预案修订实测 3.2%/中位 0.44% 实施自愈）；**静态口径** = 最近完整分红年度（锚 = 年度事件有实施或有预案的最近财年，预案先行锚切换提前到 3~4 月）级联总额；**TTM估算值（默认）= 进行中财年估算、宣告优先外推补位、随每期利润报告刷新**（与 PE 同源含快报；估算 = payout(锚)×归母TTM÷当前总股本且 payout 封顶 95%[锚年利润塌方/分红刚性/特别分红异常锚防荒谬外推，五粮液 FY2025 payout 223.5% 实测]；TTM≤0 按 0 利润估算 0.00% 参与合成不判"—"；宣告瞬间目标滚动下一财年、12 个月无空档；宣告替换外推而非取大；部分实绩超估算才用实绩；停发锚估算恒 0）；**完整性三态**：有行即齐备（0 金额预案行=显式不分配）/ 无行过 7/31(Y+1) 推定零（代价约 0.3% 支付者 ≤3 周自愈）/ 其余未知；**0.00%（齐备零分红）与 "—"（未知）严格区分**（停发每年影响 8~19% 分红公司，非边角料）；行业值=**按当日市值权重的加权平均**（total=Σ(DPS×总股本)/Σ总市值 即原整体法、float=Σ(DPS×自由流通股本)/Σ自由流通市值 系统性更高，双口径随加权方式切换、等权与 PE/PB 一致显示"—"）；数据 = 分红持久缓存（`data/dividend_history.json`，首刷 ~12 分钟一次性、增量 ann_date[日历日，公告可有周六]+ex_date[交易日]双通道探测+新成分补拉、--full=全量重拉）+ 归母TTM（PE 同源零新增接口类型）；主表接口字段 `div_{est|static}_{float|total}`（子表个股 DPS/close 无市值段 `div_{basis}`）；完整规则栈、官方字段破译与实测边界见 `docs/financial_indicators.md` 第 7 节与 `known_issues` 第 42 条
- **PB 数据（balancesheet_vip 池）**：按报告期全市场批拉 `total_hldr_eqy_exc_min_int,oth_eqt_tools`，**PB 分母 = 归母权益 − 其他权益工具（合计、已含优先股，子项 `oth_eqt_tools_p_shr` 不可重复扣减）= 归属于母公司普通股股东的权益（绝对额元、报告期末时点值）**——与 fina 的 `bps` 分子**严格同口径**（实测 20250407 对账：招行 12260−1804=10456 亿 == bps×当日股本分毫不差；华能/东航/大唐/深能源等永续债大户隐含股本与当日总股本吻合 0.001%），与 Tushare `daily_basic.pb` 及数据商惯例一致；**不经"每股×股本"折算**，报告期后送转/增发/回购的股本变动、CDR 股本口径错配（实测九号公司 689009.SH 旧口径 PB 低估 10 倍）、次新 bps 分母漂移不再引入近似（旧 bps×当日股本 口径的偏差清单见 `known_issues` 第 37 条，`get_ts_code_to_bps` 保留对照）；report_type 实测不传时默认只回 '1'（合并报表）、代码逐行过滤防御；limit=9999 单页回全量（实测 20250630 整批 6927 行）；重复行/字段级去重/ann_date 零缺失/偶见畸形代码无害；**与 fina 池并行预热**（见上行 `prefetch_fina_indicators`，bs/express 线程隔离、失败仅本池降级）；北交所与部分次新缺行（20241231 差 259 只，均不在申万股票池内、池内覆盖零损失）
- **业绩快报（express_vip 池，仅归母口径）**：按报告期全市场批拉 `n_income`（**即归母净利润**，交易所快报模板口径；实测 20241231 对账：招行快报 1483.91 亿与利润表归母分毫不差、与含少数 1495.59 亿不符，全市场 1160 只偏差符号无系统性偏正），元、累计值、**未审计初步数**（与审定值中位差 ~0.7%、43% 差 >1%、~10% 差 >10%，年报披露后自动切回审定值）；**PIT 双源合并（`_merge_attr_with_express`）**：合并可用日 = min(财报日, 快报首版日)（"最新期"按此选取），值优先级 = **审定优先**（fina 已可用且归母非空用 fina，否则快报 ann_date ≤ D 的最新修正版本），fina 可用但归母为 None 时也回退快报（宁用快报不判无财报）；TTM 三期各自独立解析；快报修正为**版本级**选择（同期多行不同 ann_date，实测 20241231 有 6 只，如 601231 先发空值后补全；n_income NaN 版本丢弃）；覆盖部分（20241231 期 1403 只约 21%、20240630 仅 100 只，集中在年报季）；实测 2025-04-07 快报参与 1060 只（年报未披露而快报已出的股票，最新期从 Q3 提前到年报）；**扣非口径与 PB 无快报源、不受影响**；快报池失败仅告警、归母退回纯财报口径；stats 加 `stocks_express`（任一期取到快报值的股票数，CLI 统计行打印）
- **报告期窗口**：计算日 D 前 24 个月内所有季末（最多 8 期），按 period 内存缓存、按计算日合并缓存（`_fina_per_stock`/`_bs_per_stock`，窗口共用 `_fina_period_window`），PE 用 fina 池、PB 用 bs 池；TTM/动态/bps/equity 结果各自按计算日（动态含口径维）缓存
- **PIT（消除前视偏差）**：每股最新期 = `ann_date ≤ D` 的最大报告期（`ann_date` 缺失按法定披露截止日推定：Q1→4/30、中报→8/31、三季报→10/31、年报→次年 4/30）；实测 2025-04-07 当天无一家公布 Q1'25，全部股票 TTM 落在 2024 年报或更早
- **每股 TTM（仅 PE 的 TTM 口径）**：标准式 `TTM = 归母(最新期) + 归母(去年年报) − 归母(去年同季)`（累计值口径关键，禁止多期累计值直接求和；扣非口径同一规则，换字段不换算法；最新期为年报时"去年同季"=去年年报、自动退化为年报值）；**不足四期兜底**（去年年报/去年同季缺失，如新股）：`TTM = 归母(最新期) × 4/k`，k=最新报告期覆盖季度数（Q1→1、中报→2、三季报→3、年报→4）；亏损股负值按 4/k 外推保留参与；最新期归母合成失败时按无财报处理（stocks_missing），不回看更早期；**动态口径无此区分**——一律 `动态 = 最新期累计 × 4/k`（`get_ts_code_to_dynamic_profit`，全市场统一年化外推、无标准式，新股天然适用；Q1 披露后季节性行业偏差大属固有特性）；**PB 无滚动、无年化兜底**（净资产为时点值，新股仅一期也直接用最新期）
- **合成（`daily_valuation_metric(tree, market_data, date, kind, ...)`，PE/PB 通用一个聚合（PE 由 `profit_kind`×`dynamic` 参数化四口径，`industry_ranking.PROFIT_BASES` 为唯一口径表）；`daily_pe`/`daily_pb` 为薄封装；复用当日涨幅榜同一份市值缓存，含停牌回退值，权重与指数一致；PB 不再用股本）**：
  - `free = Σ free_mv / Σ (股东值×ratio)`，`total = Σ total_mv / Σ 股东值`；`ratio = free_mv/total_mv`（同日同行情口径下 ≡ free_share/total_share）；逐股贡献只算一次，L3→L2→L1 三级累加
  - 每股"股东值"(万元)：PE = 所选口径净利润(TTM 或动态，元)/1e4；PB = 归母普通股股东权益(元)/1e4（balancesheet_vip 权威绝对额、与 bps 分子同口径；单股口径 PB = 总市值/普通股东权益）
  - 自由流通口径等价于"以自由流通市值为权重的个股总市值口径指标加权调和平均"
- **防御**：亏损/负净值股不剔除、合成法天然扣减；仅剔除无指标数据 / 无市值 / 自由流通占比越界（`ratio>1`，Tushare 黑盒口径防御）并按原因告警；行业股东值合计 ≤ 0 → PE=None（显示"亏损"）、PB=None（显示"资不抵债"），无数据键缺失（显示"—"）；**Web 排序时 null 按最大值参与**（降序置顶/升序置底，无数据恒置底）
- **降级**：财务接口失败（权限/积分/网络）→ 该指标列全部"—"，涨幅榜不受影响（警告不中断）
- **统计**（CLI/日志）：PE=报告期数、标准式/年化/无财报股票数；PB=报告期数、有净资产/无净资产股票数；覆盖数可与此对照

### 6. 输出与示例

- 入口脚本从 `config_store.get_token()` 取 token → 构建树/加载成分 → 计算 → 按 L3/L2/L1 打印六列涨幅（总市值加权·官方价格/总市值·分红再投资/自由流通市值加权·官方价格/自由流通·分红再投资/等权·官方价格/等权·分红再投资）+ **每个财务指标两列（PE 自由流通/总市值、PB 自由流通/总市值，默认口径归母-TTM）与 ROE、股息率（TTM估算值）、净利润同比各一列（数值%/类别文本）**、全称、成分股数量与名称；对每只行业指数用 `-100` 作"等权缺失"哨兵校验，命中即报错（单日示例日期硬编码 `2025-04-07`；区间在 `range_ranking.py` 内 `RANGE_START`/`RANGE_END` 配置）
- 运行结束输出**耗时分析**（组小计、各阶段耗时/占比、总耗时与 API 调用次数）：耗时统计 `print_timing`，API 次数由 `MarketDataProvider.snapshot_api_calls()` 提供（构造时即包装计数，含建树阶段）；大阶段按"接口拉取 vs 本地计算/回退"拆分；另打印每指标统计行（PE/净利润同比/ROE 仅默认口径归母-TTM：报告期数、标准式/年化/无财报/快报参与股票数；PB：报告期数、有净资产/无净资产股票数；净利润同比：参与/扭亏/转亏/持续亏损/无基期/池内无数据股票数；ROE：报告期数、有披露值/无数据/TTM 分母三期齐全/TTM 分母兜底/池内无数据股票数；股息率（TTM估算值）：缓存覆盖/静态有值[零分红/7-31 推定]/估算有值[零分红/实绩接管/payout 封顶/0 利润估算]/无锚/无锚年利润/池内无数据/无市值股本）
- 单日榜入口（CLI 与 Web `service._run_daily`）统一走 `run_daily_ranking`（拉行情/市值 → 等权（官方价格式）→ 等权（分红再投资式）→ 自由流通（官方价格式）→ 自由流通（分红再投资式）→ 总市值（官方价格式）→ 总市值（分红再投资式）→ PE 四口径（归母-TTM → 归母-动态 → 扣非-TTM → 扣非-动态）→ PB → 净利润同比四口径（同序）→ ROE 四口径（加权平均算法一次算出）→ 股息率双口径（TTM估算值+静态一次算出），避免两套编排漂移；财务预热线程与**分红缓存刷新线程**均在编排**最开始**启动、与行情/市值/涨幅计算全程并行），**返回 8 元组 `(等权·官方价格式, 等权·分红再投资式, 自由流通·官方价格式, 自由流通·分红再投资式, 总市值·官方价格式, 总市值·分红再投资式, timings, valuation)`**；valuation = `{"pe_"+basis / "pb": {"free"/"total": {"1"|"2"|"3": {index_code: 值|None}}, "stats": {...}}}`（basis ∈ `PROFIT_BASES` 四口径**一次全部算出**、共享同一批财务数据与市值缓存，动态与扣非口径仅本地重算零新增请求，供 Web"净利润口径"下拉切换；None=亏损/资不抵债、键缺失=无数据/降级）；**`"growth_"+basis` 为 {"value": {"1"|"2"|"3": {index_code: 数值%|"扭亏"/"转亏"/"持续亏损"}}, "stats": {...}}**（净利润同比四口径一次算出、无市值维度等权模式也显示、随"净利润口径"下拉切换、键缺失=无数据/降级）；**`"roe_waa_"+basis` 为 {"float"/"total": {"1"|"2"|"3": {index_code: ROE%}}, "stats": {...}}**（ROE 加权平均算法四口径由 `daily_roe` 一次算出、**市值权重加权算术平均双口径**随加权方式切换[等权显示"—"]、分子另随"净利润口径"与"ROE算法"两下拉切换、键缺失=无数据/降级/无参与股票）；**`"div_yield"` 为 {"float"/"total": {"est"|"static": {"1"|"2"|"3": {index_code: 股息率%}}}, "stats": {...}}**（股息率双口径由 `daily_dividend_yield` 一次算出、**市值权重加权平均双口径**随加权方式切换[等权显示"—"]、另随"股息率口径"下拉切换、键缺失=无数据/降级/无参与股票），Web 前端单日榜与**区间链式榜**均显示财务指标列（区间口径=区间末交易日时点值，表头"共 N 个行业"后注明；静态版区间榜不带字段显示"—"）；指标全套的编排抽为公共函数 `compute_fin_metric_suite`（预热启动 `start_metric_prefetch`），单日榜与区间链式榜共用同一实现避免漂移；timings key：`daily_fetch`/`mv_fetch`/`equal_compute`/`equal_tr_compute`/`float_compute`/`float_fallback`/`float_tr_compute`/`total_compute`/`total_fallback`/`total_tr_compute`/`total_tr_fallback`/**`fina_fetch`**(fina+balancesheet+express 三池并行+增长基期串行补拉的后台线程总墙时, 与其他阶段重叠、加总口径含重复计入)/**`pe_compute`**/**`pe_dynamic_compute`**/**`pe_deduct_compute`**/**`pe_deduct_dynamic_compute`**/**`pb_compute`**/**`growth_compute`**/**`growth_dynamic_compute`**/**`growth_deduct_compute`**/**`growth_deduct_dynamic_compute`**/**`roe_compute`**(ROE 四口径一次)/**`div_yield_compute`**(股息率双口径一次)；进度回调 `(0~100, 说明, 阶段名)`（阶段名供 Web 前端展示）
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
- **区间末财务指标（`valuation_out` 参数，Web 链式区间榜使用）**：传入非 None dict 时追加计算**区间末交易日时点值**的财务指标全套（与单日榜共用 `start_metric_prefetch` 预热 + `compute_fin_metric_suite` 编排，键结构一致调用方可硬下标）；财务/分红预热线程在编排最开始启动、与三池预取及逐日计算全程并行，末日市值直接复用逐日循环的 daily_basic 缓存（含停牌 memo 写回）；涨幅列成分口径为逐日过滤联合、指标列为末日成立口径（`filter_stock_pool(T_end, T_end)`），两者数量可差几个中途纳入/退出的成分，属"时点值"语义本身；None 则完全不算（静态版/对照场景）
- 返回 `(等权·价格, 等权·全收益, 自由流通·价格, 自由流通·全收益, 总市值·价格, 总市值·全收益)`（各为 L1/L2/L3 榜单）；`_build_levels` 两分支同构，前端不用区分
- **数据/性能约定**：`daily_basic`/`dividend`/行情三池**并行**预取（`fetch_daily_batch`/`fetch_mv_batch`/`fetch_ex_div_batch` 同时提交，各接口独立 450 次/分钟节流，并行总时长 ≈ 单段时长；`fetch_daily_batch` 已回填 pct/close 缓存，逐日命中零请求）；**日历跨度切片缓存**（`market_data.get_trading_days` 与 `tree._trading_days_window` 预取宽区间后子窗口切片命中，除息日 12 天窗口与新股 6 交易日门槛的逐日查询全部归零）；**停牌股跨日复用 memo**——当日不在全市场市值数据中的参与股沿用最近一次已知市值（停牌期间必然不变、零重复点查），memo 仅对**当日参与股票**（逐日过滤后）生效，避免为退市已久/尚未上市的历史成分发起无谓点查；复牌/新上市日由全市场数据自动刷新
- 实测（2024-09-24~2024-12-31，66 交易日）：链式总计约 **24 秒**（预取三池并行 9.3s + 逐日 6 序列聚合纯 CPU 12.2s + 首日点查 2.4s）；默认月区间（23 天，2026-07）约 **11 秒**；`trade_cal` 仅 3 次（预取+切片）、`daily_basic` 66+点查（66 全市场 + 11 停牌点查）、`dividend` 66 次（预取）；与申万官方指数 L1 区间涨幅对照 31/31 行业平均差 0.44pp、最大 1.09pp（差异来源=Tushare 自由流通口径 vs 官方附录二扣减明细，见 known_issues 第 18 条）
- `timings`：`trade_cal`/`prefetch`(三池并行总时长)/`daily_fetch`/`mv_prefetch`/`ex_prefetch`/`accumulate`/`mv_resolve`/`compute`/`trading_days`；`detail` 语义与静态版一致（`ts_code_to_*` 为首日盘前市值）

### 8. 申万官方指数算法（只读权威文档）与同步进度

- `docs/Shenwan_Index_Series_Algorithm_Text.md`：**申万官方行业指数计算方法的纯文字版**（申银万国股价系列指数算法，官方发布文本，随仓库提交）
- 该文件**只能阅读、禁止任何修改**（包括格式、文字、公式与错别字勘误）；确需变更必须先征求用户同意，由用户提供新版本覆盖
- **未来目标**：把项目内**所有市值类加权算法**（自由流通市值加权、总市值加权，单日榜与区间榜）逐步与官方算法**完全同步**
- **同步进度**：单独记录在独立子文档 `docs/sync_progress.md`（本文件只保留规则、不内嵌进度明细）；每完成一个章节的对照与同步，必须到该文件更新进度记录；**进度记录一律按官方章节顺序排列**（4.1 → 4.2 → 4.3 → 4.4.1…4.4.14 → 附录二：进度总览表格、已同步章节明细、未覆盖项三处都遵守），同一章节拆出的多条记录（如 4.4.3 新股上市 / 公司退市）相邻排列，新增记录插入对应章节位置、不追加在末尾，不得因后写而打乱既有顺序；非官方章节项（区间累计口径）排在全部章节之后

## 强制核对流程（任务完成通知前必做）

1. 报告"完成"前，重新通读本文件「核心算法约定」、`docs/interface_notes.md`「接口交互明细」与 `docs/Shenwan_Index_Series_Algorithm_Text.md`（官方算法，只读）
2. 逐条对照交付与描述一致，核对点至少包括：涨跌幅是否仍由 `close/pre_close` 重算（尤其复权/除权除息）；等权平均公式与加权公式（`ΔM=M*p/(p+100)`、`M_pre=M/(1+p/100)`）；**市值类加权口径与官方算法文本的对照（第 8 节）**；停牌按 0% 计入与 730 天回退；股票池过滤规则；区间榜参与口径 / 连乘基准 / 起始日权重锚定（第 7 节）；**财务指标（第 5.1 节）**：`profit_dedt`/归母合成值为累计值、**归母 = profit_dedt + extra_item 仅同行合成**、标准 TTM 公式与 4/k 年化兜底、**动态口径 = 最新期累计 × 4/k（全市场统一年化、无标准式；最新期为年报时与 TTM 退化式相等）**、**动态同比 = 最新期累计/去年同季累计（同相位，不用"动态(D)/动态(D-1年)"两时点对比；去年同季主窗口优先、基期窗口兜底停披股）**、`bps` 为时点值（PB 无年化）、**PB 分母 = balancesheet_vip `归母权益−其他权益工具（已含优先股）` 普通股股东权益绝对额**（`get_ts_code_to_equity`，旧 `bps×当日股本` 口径保留对照）、PIT `ann_date ≤ D`（各池同规则）、**归母 TTM 的业绩快报双源合并（`_merge_attr_with_express`：合并可用日取 min、值审定优先、快报取 ≤D 最新修正版本、快报失败退回纯财报）**、**净利润同比（TTM/动态两式各四口径、Σ合成、both-or-neither 参与、四级类别与排序、TTM 式基期 [D-36月, D-12月] 窗口与旧期翻页）**、`ratio = free_mv/total_mv` 同日同源、越界与亏损/资不抵债展示、字段级去重（重复行 NaN 与双值）；**ROE（第 5.1 节）：披露值 roe_waa 锚定、E_waa=归母×100÷roe_waa 反推、四口径公式（attr_dynamic=roe_waa×4/k）、E_TTM 分段推导与兜底、全链不接快报、缺失降级、整体法 Σ分子/Σ分母（显示上负值类别化为"亏损"、排序按真实负值）**；**股息率（第 5.1 节）：财年归属按 end_date 年份前缀、总额法（每股×base_share÷当前总股本）、事件级级联实施>预案>无且不碰"股东大会通过"行、静态锚=年度事件有行最近财年、完整性三态（有行/7-31 推定零/未知，0.00% 与 "—" 严格区分）、TTM估算值=进行中财年宣告优先外推补位随每期报告刷新且宣告替换外推不取大、行业整体法 Σ(DPS×总股本)/Σ(总市值)**；Tushare 接口、参数、分页、token 获取（`docs/interface_notes.md`）
3. 发现不一致（无论本次引入还是历史遗留）**必须在最终回复中明确列出**，不得静默通过；涉及算法变更同步更新本文件并说明变更点
4. 本文件与代码冲突时以代码为准，但必须把冲突点报告给用户
