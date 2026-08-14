# shenwan_industry 模块说明（算法与接口约定）

本文件是申万行业模块的**算法权威描述**。涉及本模块的任何任务（新增、修改、运行、排查、文档），在向用户报告完成之前，必须先对照本文件核对一致性，具体要求见文末「强制核对流程」。

## 模块职责与文件

- 模块内容：申万 2021 三级行业分类树 + 行业涨幅榜（单日榜 / 区间累计榜，等权 / 流通市值加权），当前为控制台输出脚本（与 `holders/` 生成图片不同）
- 文件：
  - `industry_tree.py`：行业树与成分数据层，含 `ShenWanIndustryNode`（行业树节点）与 `ShenWanIndustryTree`（树构建、成分股加载、`in_date`/`delist_date` 记录、股票池过滤 `filter_stock_pool`（锚点/末日参数化，单日榜与区间榜共用））
  - `market_data.py`：行情数据层 `MarketDataProvider`（构造时包装 pro 并累计 API 调用计数，`snapshot_api_calls()` 取快照；按日内存缓存涨跌幅/收盘价/流通市值；停牌流通市值 730 天回退；交易日历；区间逐日行情并发+限流拉取）
  - `industry_ranking.py`：排行榜算法库，单日榜（`daily_rank_equal_weight` / `daily_rank_float_weight`）+ 单日榜编排（`run_daily_ranking`，CLI 与 Web 共用）+ 区间累计榜（`rank_range`），另含耗时输出工具 `print_timing`（API 调用计数由 `MarketDataProvider` 提供）
  - `daily_ranking.py`：单日行业涨幅榜入口脚本（组装 tree + provider，走 `run_daily_ranking` 并输出耗时分析）
  - `range_ranking.py`：区间累计涨幅榜入口脚本（区间在文件内 `RANGE_START`/`RANGE_END` 配置，输出耗时分析）
  - `SW2021.json`：申万 2021 行业分类本地数据（推荐数据源，随仓库提交，勿删）
  - `__init__.py`：空
  - `web/`：本地 FastAPI Web 服务（`server.py` / `jobs.py` / `service.py` / `schemas.py` + `static/`）。后台单 worker 串行执行任务，前端轮询进度，支持单日榜 / 区间榜、主表升降序和成分股子表；`desktop.pyw` 为 Qt WebEngine 桌面窗口启动器，负责后台启动服务、加载前端并在窗口关闭时清理后端进程（启动过渡：窗口从创建起直接就是 QWebEngineView，无任何原生加载页/骨架屏/QStackedWidget 切换；创建时立即预载 `about:blank`，让渲染器冷启动协商（表面/缩放）在画面呈现前完成——已实测确认"缩小再放大"闪烁的窗口几何恒不变，根因是渲染器首帧协商被暴露，预载后后端就绪再加载正式页面时首帧干净；白屏期 = 引擎冷启动 + 后端启动并行；旧 `static/loading.html` 与原生骨架屏方案均已废弃）。一级行业官方指数 K 线通过 `sw_daily` 获取，前端使用 `static/vendor/echarts.min.js` 绘制

## 运行环境与数据源

- 必须使用 veighna studio 自带 Python 运行；Tushare token 从 vnpy 全局配置 `SETTINGS["datafeed.password"]` 读取（见 `__main__` 写法），禁止硬编码
- 运行需要联网访问 Tushare Pro；接口权限依赖账号积分，具体以官方文档为准
- 日期统一为 `YYYYMMDD` 字符串；方法内部使用 `datetime` 对象，通过 `strftime("%Y%m%d")` 转换

## 核心算法约定（必读）

### 1. 行业树构建

- 默认 `build_industries()`：读本地 `SW2021.json`，逐行调用 `parse_industry_row()` 构建
- 备用 `build_industries_by_tushare()`：`pro.index_classify(src='SW2021')` 全量拉取后逐行解析；行业数据长期不变，本地优先
- `parse_industry_row()`：`level` 为字符串 `"L1"/"L2"/"L3"`；L1 节点挂到 root，其余节点通过 `parent_code` 在 `industry_code_to_node` 中找父节点挂接；同时登记 `index_code` / `industry_code` / `industry_name` 三种映射
- 全称 `industry_name_long`：L1=自身名；L2=父名+“-”+自身名；L3=父全称+“-”+自身名
- 父节点缺失时会直接 `KeyError`（当前未兜底），改动时注意

### 2. 成分股加载与过滤

- `build_constituent_stocks_by_tushare(filter_unlisted=True)`：
  - 若 `filter_unlisted` 且 `stock_basic` 尚未加载：`pro.stock_basic` 一次拉取**上市(L) + 退市(D) + 暂停上市(P)** 三种状态股票；D 的 `delist_date` 记录到 `ts_code_to_delist_date`，供历史日期过滤
  - `pro.index_member_all(offset=..., limit=1999)` 循环拉取成分，直到返回行数小于批大小；每行按 `l3_code` 找到 L3 节点，把 `ts_code` 同时加入 L3 及其 L2 父、L1 祖父节点的 `constituent_stocks`，并登记 `constituent_stock_to_l3_node`
  - 同时把每只成分的纳入日期 `in_date`（接口默认返回字段，YYYYMMDD）记录到 `ts_code_to_in_date`，供历史日期过滤使用
  - `filter_unlisted=True` 时，不在 `stock_basic`（L/D/P）中的股票直接跳过
  - `l3_code` 找不到节点 → `ValueError`
- `filter_stock_pool(stock_pool, anchor_date, end_date, cancel_check=None)`：锚点/末日参数化，单日榜传 `(date, date)`，区间榜传 `(区间起始日, 区间末日)`；返回被剔除股票的类别明细 `{类别: [ts_code]}`（`no_industry` / `in_date_later` / `delisted` / `not_listed`），区间榜据此汇总告警
  - 剔除 `no_industry_stocks`（本实例上累计解析失败、无行业归属的股票）
  - 剔除 `in_date > anchor` 的成分（锚点之后才纳入行业的股票不参与，**消除“未来纳入”的前视偏差**）
  - 剔除 `delist_date < end` 的股票（末日之前已退市不参与，退市日当天及之前正常参与，**修复退市股被整体剔除的幸存者偏差**）
  - 剔除 `list_date >= anchor` 的股票（锚点当天及之后才上市的股票不参与）
- 排名时的股票池 = **当日行情股票 ∪ 已加载成分股**，再做上述过滤

### 3. 行情获取与涨跌幅/流通市值口径（`market_data.MarketDataProvider` 方法）

- `get_ts_code_to_pct_chg(date)`：
  - `pro.daily(trade_date=YYYYMMDD, offset=..., limit=5999)` 循环拉全市场当日行情
  - **涨跌幅自行重算**：`pct = (close - pre_close) / pre_close * 100`，不使用接口返回的 `pct_chg` 字段
  - 结果按日期存内存缓存（`ts_code_to_pct_chg_cache`）
- **涨跌幅口径（已实测验证）**：Tushare `daily` 的 `pre_close` 在除权除息日返回的是交易所发布的**除权参考价**（实测：招行 20250711 除息日 `pre_close=46.24=48.24−2.0`；药明康德 20190702 除权日 `pre_close=64.66=(91.10−0.58)/1.4`），因此 `pct=(close−pre_close)/pre_close` 与 `adj_factor` 比值法**等价**（实测日涨幅 −1.2976% vs −1.2980%），不需要额外拉取 `adj_factor`
  - 该口径是**价格指数口径**：除权除息不产生虚假跳变，但现金分红本身**不计入收益**（除息日价格跌掉的分红被除权参考价抵消，不产生正收益）；若要做“红利再投资全收益指数”，需在除息日另行加回股息率
  - 修改涨跌幅逻辑时，保持“基于 `daily` 的 `close/pre_close`（除权参考价口径）”即可；如改用 `adj_factor` 或复权行情，必须确认仍是同一价格口径并同步更新本文件
- `get_ts_code_to_circ_mv(date)`：
  - `pro.daily_basic(ts_code='', trade_date=YYYYMMDD, fields='ts_code,circ_mv', offset=..., limit=5999)` 循环拉全市场（官方单次上限 6000）
  - `circ_mv` 单位为万元，但加权公式只用比值，单位不影响结果
  - 结果按日期存内存缓存（`ts_code_to_circ_mv_cache`）

### 4. 单日等权涨幅 `industry_ranking.daily_rank_equal_weight(tree, market_data, date)`

- 股票池过滤见第 2 节；逐只取 L1/L2/L3 节点，解析失败跳过（并记入 `no_industry_stocks`）
- 单股涨跌幅：当日有行情用实际值；**停牌（行情缺失）按 0% 计入，且计入该行业成分股数量**
- 数据异常（涨跌幅非有限值，如 `pre_close` 缺失/为 0）：`get_ts_code_to_pct_chg` 记为 None 并告警，排名时**直接跳过，不计入平均与数量**（与停牌按 0% 不同）
- 行业涨幅 = 成分股涨跌幅的简单算术平均；代码用增量平均实现：`new_avg = (old_avg * old_count + pct) / (old_count + 1)`，L3/L2/L1 三级同时累计
- 排序：剔除 `count == 0` 的行业，按涨幅降序；返回 `(index_code, pct, count)` 列表（三级各一个）

### 5. 单日流通市值加权涨幅 `industry_ranking.daily_rank_float_weight(tree, market_data, date)`

- 单股单级公式（M = 当日收盘流通市值，p = 当日涨跌幅%）：
  - 当日新增流通市值：`ΔM = M * p / (p + 100)`
  - 当日开盘前流通市值：`M_pre = M / (1 + p / 100)`
  - 行业涨幅 = `ΣΔM / ΣM_pre * 100`（代码按三级分别累计后取比值）
- 停牌处理（本模块最特殊的逻辑，与等权不同）：
  - 当天 `daily_basic` 中无该股 `circ_mv` 时，回退查询 `pro.daily_basic(ts_code=..., fields='trade_date,circ_mv', start_date=date 前 730 天, end_date=date)`
  - 响应按 `trade_date` 降序，取第一条 `trade_date <= date` 的记录作为“停牌前最近流通市值”，并回填到当日缓存
  - **最多支持连续停牌约 2 年（730 天）**，再往前查不到则 `ValueError`
  - 停牌股涨幅按 0% 计 → `ΔM = 0`，但 `M_pre = M` 仍计入行业分母（对行业涨幅有稀释作用）
  - 数据异常跳过规则与等权一致；回退扫描时跳过 `circ_mv` 为 NaN 的日期，取最近的有效值
- 其余规则（股票池、节点解析、排序、返回结构）与等权一致

### 6. 输出与示例

- `__main__`：从 vnpy `SETTINGS["datafeed.password"]` 取 token → `ts.pro_api(token)` → 构建树、加载成分股 → 对示例日期（当前硬编码 `2025-04-07`）分别计算等权与加权 → 按 L3/L2/L1 打印两列涨幅、行业全称、成分股数量与名称列表
- 示例中对每只行业指数用 `-100` 作为“等权缺失”哨兵校验，命中即报错
- 两个入口脚本（`daily_ranking.py` / `range_ranking.py`）运行结束都会在控制台输出**耗时分析**（按主次分组：组小计、各阶段耗时/占比、总耗时与 API 调用次数），耗时统计工具为 `industry_ranking.print_timing`，API 调用次数由 `MarketDataProvider.snapshot_api_calls()` 提供（构造时即包装计数，含建树阶段调用）；秒级以下的零碎阶段合并展示，行情拉取/市值拉取等大阶段按“接口拉取 vs 本地计算/回退”拆分
- 单日榜入口（CLI 与 Web `service._run_daily`）统一走 `industry_ranking.run_daily_ranking`（拉行情/市值 → 等权 → 加权 的公共编排），避免两套编排漂移；其 timings key 为 `daily_fetch` / `circ_fetch` / `equal_compute` / `float_compute` / `float_fallback`，进度回调为 `(0~100, 说明, 阶段名)`（阶段名供 Web 前端展示）
- 模块暂无图片产物，仅控制台输出

### 7. 区间累计涨幅榜 `industry_ranking.rank_range(tree, market_data, start_date, end_date, timings=None, progress_callback=None, detail=None)`

- 返回 `(等权(l1,l2,l3), 流通市值加权(l1,l2,l3))`，榜单项仍为 `(index_code, 涨跌幅%, 成分股数量)`
- 参与股票：区间**起始日**已在成分（`in_date <= 区间起点`）**且**区间末仍在（`delist_date >= 区间终点`）**且起始日已上市**（`list_date < 区间起点`，因为 Tushare 新股的 `in_date` 可能是上市前的“预先纳入日”，早于实际上市）；中段才纳入 / 起始日尚未上市 / 区间末前已退市均剔除；同类剔除告警**按类型汇总为一行**（显示数量与少量样例），避免大量日志刷屏；筛选复用 `filter_stock_pool(stock_pool, 起点, 终点)`，其返回的类别明细即告警数据源
- 个股区间收益 = 区间内所有有行情日的每日官方涨跌幅（`close/pre_close`，除权参考价口径）连乘，**包含起始日当天涨跌**，隐含基准 = 区间内首个有行情日的 `pre_close`（即区间前一交易日收盘 / 停牌前收盘）；整段区间无任何行情的股票直接剔除；停牌日自动按 0% 累计（**无需逐股回退查收益**）
- 权重：两个榜都锚定**区间起始日**——等权 = 起始成分简单平均，加权 = 起始日流通市值权重（起始日停牌按 730 天回退；仍取不到则仅参与等权榜，告警汇总为一行）
- 网络策略：`trade_cal` 1 次 + 区间内每个交易日 `daily(trade_date)` 1 次 + 起始日 `daily_basic` 1 次 + 少量停牌回退；**不是简单重复执行 N 次单日接口**；逐日 `daily` 用线程池并发拉取（8 worker），并按固定速率（`MAX_DAILY_FETCH_RATE=7.5 次/秒`，约 450 次/分钟，留 10% 余量）平摊请求开始时刻，避免瞬时爆发与官方 60 秒滚动窗口的微小不对齐触发 429；单日失败自动重试 3 次，仍失败则抛错（不静默改变结果）
- 入口示例：`python shenwan_industry/range_ranking.py`（区间起止在文件内 `RANGE_START`/`RANGE_END` 配置）
  - `timings`：可选 dict，`rank_range` 会把各阶段耗时写入（`trade_cal`/`participate`/`daily_fetch`/`accumulate`/`circ_fetch`/`circ_fallback`/`compute`/`trading_days`），供入口脚本输出耗时分析；`daily_rank_float_weight` 的 `timings` 记录 `circ_fallback`（停牌市值回退耗时）
  - `progress_callback`：可选 `(percent: float, message: str) -> None`，仅在拉取交易日历、逐日行情、累计收益、市值权重、聚合计算等阶段回调，不参与任何数值计算
  - `detail`：可选 dict，`rank_range` 会写入 `stock_ret`（参与股票的区间收益）、`last_close`（区间末日收盘价）和 `ts_code_to_circ_mv`（起始日流通市值，含停牌回退），供 Web 成分股子表使用；传入 `None` 时行为与旧版完全一致

## Tushare 接口交互明细

| 接口 | 用途 | 调用参数 | 分页/批次 | 备注 |
| --- | --- | --- | --- | --- |
| `index_classify` | 行业树（备用） | `src='SW2021'` | 一次全量 | 默认用本地 `SW2021.json`，仅备用 |
| `stock_basic` | 股票池状态过滤 | `list_status='L'/'D'/'P'`，D 带 `delist_date` | 每次调用不分页 | 上市+退市+暂停全部进 `stock_basic`；D 的 `delist_date` 进 `ts_code_to_delist_date` |
| `index_member_all` | 行业成分股 | `offset, limit=1999` | 循环直到不足一批 | 按 `l3_code` 挂到三级节点 |
| `trade_cal` | 区间交易日列表 | `exchange='SSE', start_date, end_date, is_open='1', fields='cal_date'` | 一次 | 区间榜取交易日用 |
| `daily` | 全市场单日行情 | `trade_date, offset, limit=5999` | 循环直到不足一批 | 涨跌幅自行从 `close/pre_close` 重算 |
| `daily_basic`（全市场） | 单日流通市值 | `ts_code='', trade_date, fields='ts_code,circ_mv', offset, limit=5999` | 循环直到不足一批 | 官方单次上限 6000，5999 留余量 |
| `daily_basic`（单只） | 停牌回退查流通市值 | `ts_code, fields='trade_date,circ_mv', start_date, end_date` | 不分页 | 响应按 `trade_date` 降序，取 ≤ date 最新一条 |

- token 一律从 vnpy `SETTINGS["datafeed.password"]` 获取，代码中不出现真实 token
- API 调用计数：`MarketDataProvider` 构造时包装 `pro` 并累计，`snapshot_api_calls()` 取快照；Web 任务前后快照求差即该任务实际调用次数（缓存命中不计；建树阶段调用不计入任务）
- 限流（已实测）：本账号 5000 积分，单接口限流 **500 次/分钟**（按 60 秒滚动窗口计数，官方报错信息原文确认；本地文档未列具体数字）。区间榜一次约 78 次调用（daily 66 + daily_basic 6 + 其他 6），远低于上限，可安全并发；但同一分钟连续跑多次区间会累积，批量任务需按窗口留余量（建议单接口 ≤ 450 次/分钟）
- 接口名、参数、字段与权限要求以 `skills/tushare/references/数据接口.md`（本仓库克隆的官方文档）为准，不要凭记忆硬写

## 已知边界与易错点

1. 涨跌幅口径（见第 3 节）：`daily.pre_close` 已含除权参考价修正（价格口径），现金分红不计入收益；改动涨跌幅逻辑时必须保持该口径，或明示切换为全收益口径
2. 停牌股等权按 0% 计入平均、加权按 0% 涨幅 + 停牌前流通市值计入分母；停牌占比高的日期，等权涨幅会被系统性拉低
3. 停牌流通市值回退上限 730 天；连续停牌超 2 年直接报错
4. 上市首日（`list_date == date`）不参与，`list_date >= date` 即剔除
5. 退市股/暂停股在成分股加载时已纳入（`stock_basic` L/D/P），按 `delist_date` 截断；`stock_basic` 每次调用不分页，若单次行数上限低于对应状态股票总数会漏尾部股票（当前 L=5543 未触发，扩容后有风险）
6. `no_industry_stocks` 在 tree 实例上累积，后续日期过滤会一直沿用
7. `MarketDataProvider` 内存缓存按日期键分开；同一实例跨日期复用不会串数据（停牌回填值也写在对应日期字典里）
8. 非交易日 `daily` 返回空 → 排名方法抛 `ValueError`
9. NaN/缺失值防御（已加固）：`pre_close`/`close` 缺失、为 0 或非有限值时，`get_ts_code_to_pct_chg` 记为 None 并告警，排名时该股票不计入平均/加权（不再污染 NaN）；`circ_mv` 为 NaN 时跳过不写入字典，缺失时走停牌回退，回退扫描同样跳过 NaN 行；`stock_basic` 的 `list_date` 为 NaN/None 时跳过（不再 `strptime` 崩溃）
10. 复权字段单位（已实测验证）：tushare `dividend` 的 `stk_div` 为每股送转股数（10送10 → 1.0）、`cash_div` 为每股派现元（10派10 → 1.0）；`adj_factor` 与 `daily.pre_close` 同属**除权参考价（价格）口径**，比值收益 = close/除权参考价 − 1，现金分红不计入收益；曾被独立实现的"送转除、派现减"静态前复权算法（原 `qfq_adjust.py`，已删除）与上述口径一致
11. 停牌复牌（已实测验证）：`daily` 在停牌日**没有记录**；普通复牌日 `pre_close` = 停牌前最后收盘价（实测 300862/300955），停牌期间发生除权除息时复牌日 `pre_close` = 交易所发布的除权参考价，因此复牌日涨跌幅直接用 `close/pre_close` 即为交易所口径，无需额外处理
12. **除权参考价可能不是“免费送转”公式**（000793.SZ 实测案例）：*ST华闻 2026-06-22 复牌日为重整计划“有偿转增”（10转12，转增平均价格 2.41 元/股），交易所除权参考价 = (前收 2.63 + 2.41×1.2)/(1+1.2) = **2.51**，而非普通免费送转的 2.63/2.2≈1.20；`dividend` 接口只含转增比例、不含转增价格，**无法自行推导除权参考价**；第三方“不复权”行情（如东财显示 0.38%）未采用交易所参考价，与官方口径（Tushare `pct_chg` 5.18%）不同。**结论：当日涨跌幅/昨收一律以 `daily.pre_close`（交易所口径）为准，`dividend` 只用于了解方案本身**
13. 历史成分口径（已修部分）：排名已按 `in_date` 过滤（分析日之后才纳入的成分不参与），且退市股已纳入并按 `delist_date` 截断（退市日后不参与），消除“未来纳入”与“退市股被整体剔除”两类偏差；**剩余缺口**：被剔除/换行业但仍上市的股票，Tushare `index_member_all` 只有当前归属（`out_date` 恒为空、`is_new` 全为 Y），需逐股历史补齐，尚未实现（完整方案见文末「未来规划」节）
14. 区间榜口径（见第 7 节）：中段纳入/起始日尚未上市/区间末前退市/整段停牌均不参与；无法取到起始日市值的股票仅参与等权榜（告警不中断）；**区间收益包含起始日当天涨跌**（隐含基准 = 首个有行情日的 `pre_close`），单日区间（起止同日）等于当日涨跌幅
15. 性能约定：DataFrame 逐行遍历统一使用 `itertuples(index=False)`（实测 5500 行 × 66 天解析：`iterrows` 约 5.7s vs `itertuples` 约 0.1s），不要改回 `iterrows`；行内取值用 `row.列名`（可选列用 `getattr(row, '列名', None)`），转 dict 用 `row._asdict()`

## 未来规划：自建行业指数（核心工作）

自建申万行业指数（K 线）是项目未来核心工作（官方指数不稳定且种类少），前置条件是**历史成分完整**。现状与计划：

- **已完成**：`in_date` 过滤（分析日之后才纳入的成分不参与）+ 退市股按 `delist_date` 截断，消除“未来纳入”与“退市股被整体剔除”两类偏差
- **剩余缺口**：被剔除/换行业但仍上市的股票，`index_member_all` 只有当前归属（`out_date` 恒为空、`is_new` 全为 Y）
- **实现方案（已实测验证可行性）**：
  - 逐股调用 `index_member(ts_code=...)` 建历史成分缓存：约 5400 上市 + 340 退市 ≈ 6000 次调用，按 ~180 次/分钟约 30 分钟，需分批/限流
  - 过滤规则：只保留 `index_code` 能映射到 `SW2021.json` 行业树节点的记录；该接口混有申万主题/风格指数与中证记录（如 399810.CSI、801862 等），必须过滤
  - 切换日期以 L1/L2 记录为准；L3 的 `in_date` 存在回填不一致（实测顺丰 L3“快递”in_date=20100323 早于 L1 切换日 20170303）
  - 借壳换代码的股票（三六零 601313→601360）历史挂在现代码下，老代码查不到记录，按现代码查询
  - 2021-12-13 前后有版本切换记录（实测中免、中公），做历史归属前需确定口径（按 SW2021 回填 or 当时官方版本）
  - 缓存结构建议：`shenwan_industry/historical_membership.json`，`{ts_code: [{index_code, in_date, out_date}]}`；过去日期永久有效，每半年增量刷新新增调整（每批约 60~110 只）
  - 接入方式：`filter_stock_pool`（锚点=分析日期）按股票在分析日期落在哪个行业区间决定归属，与现有 `in_date`/`delist_date` 过滤整合
- **指数构建（后续另行规划）**：逐日链式累乘 `指数点位 = 前日点位 × (1 + 当日加权涨幅)`；涨跌幅用 `daily.pre_close`（交易所除权参考价口径，见第 3 节）；成分历史、加权口径（流通 vs 自由流通）、新股纳入规则需与官方指数对账

## Web 服务未来优化

- 当前 `web/jobs.py` 采用单 worker 串行队列，主要为了避免 Tushare 接口限流和 `ShenWanIndustryTree` / `MarketDataProvider` 可变状态（行业树、按日行情缓存、API 计数）并发冲突。后续若改为多 worker，需要先把行业树/行情缓存改成线程安全访问，并增加按 Tushare 每分钟调用上限的全局限流器。
- 任务取消已实现：`POST /api/jobs/{job_id}/cancel`，JobManager 设置取消标记，`rank_range` 的逐日拉取、`daily_rank_float_weight` 的停牌市值回退等长循环会协作式检查并返回 `cancelled` 状态。

## 强制核对流程（任务完成通知前必做）

以下规则适用于**所有**涉及本模块的任务（新增、修改、运行、排查、写文档）：

1. 在向用户报告“完成”之前，必须重新通读本文件「核心算法约定」与「Tushare 接口交互明细」两节
2. 逐条对照：本次交付的实际代码/结果/文档与上述算法描述必须一致；核对点至少包括：
   - 涨跌幅是否仍由 `daily` 的 `close/pre_close` 重算，口径有无变化（尤其复权/除权除息）
   - 等权平均公式、加权公式（`ΔM = M*p/(p+100)`、`M_pre = M/(1+p/100)`）是否被改动
   - 停牌按 0% 计入、流通市值回退 730 天逻辑是否保持（或已同步更新描述）
   - 股票池过滤规则（未上市、无行业、退市）是否一致
   - 区间榜参与口径（起始日成分 ∩ 区间末成分）、连乘基准、起始日权重锚定是否与第 7 节一致
   - Tushare 接口、参数、分页、token 获取方式是否一致
3. 若发现不一致（无论是本次改动引入，还是历史遗留），**必须在最终回复中明确列出差异**，不得静默通过；涉及算法变更时应同步更新本文件并说明变更点
4. 本文件与实际代码冲突时，以实际代码为准，但必须把冲突点报告给用户
