# 申万模块：未来规划与 Web 优化

本文件为 `shenwan_industry/AGENTS.md` 的子文档，记录未来工作方向。

## 未来规划：自建行业指数（核心工作）

自建申万行业指数（K 线）是项目未来核心工作（官方指数不稳定且种类少），前置条件是**历史成分完整**。现状（已修部分与剩余缺口详见 `known_issues.md` 第 13 条）与计划：

- **实现方案（已实测验证可行性）**：
  - 逐股调用 `index_member(ts_code=...)` 建历史成分缓存：约 5400 上市 + 340 退市 ≈ 6000 次调用，按 ~180 次/分钟约 30 分钟，需分批/限流
  - 过滤规则：只保留 `index_code` 能映射到 `data/SW2021.json` 行业树节点的记录；该接口混有申万主题/风格指数与中证记录（如 399810.CSI、801862 等），必须过滤
  - 切换日期以 L1/L2 记录为准；L3 的 `in_date` 存在回填不一致（实测顺丰 L3"快递"in_date=20100323 早于 L1 切换日 20170303）
  - 借壳换代码的股票（三六零 601313→601360）历史挂在现代码下，老代码查不到记录，按现代码查询
  - 2021-12-13 前后有版本切换记录（实测中免、中公），做历史归属前需确定口径（按 SW2021 回填 or 当时官方版本）
  - 缓存结构建议：`shenwan_industry/historical_membership.json`，`{ts_code: [{index_code, in_date, out_date}]}`；过去日期永久有效，每半年增量刷新新增调整（每批约 60~110 只）
  - 接入方式：`filter_stock_pool`（锚点=分析日期）按股票在分析日期落在哪个行业区间决定归属，与现有 `in_date`/`delist_date` 过滤整合
- **指数构建（后续另行规划）**：逐日链式累乘 `指数点位 = 前日点位 × (1 + 当日加权涨幅)`；涨跌幅用 `daily.pre_close`（交易所除权参考价口径，见 AGENTS.md 第 3 节）；成分历史、加权口径（流通 vs 自由流通）、新股纳入规则需与官方指数对账

## Web 服务未来优化

- 当前 `web/jobs.py` 采用单 worker 串行队列，主要为了避免 Tushare 接口限流和 `ShenWanIndustryTree` / `MarketDataProvider` 可变状态（行业树、按日行情缓存、API 计数）并发冲突。后续若改为多 worker，需要先把行业树/行情缓存改成线程安全访问，并增加按 Tushare 每分钟调用上限的全局限流器。
- 任务取消已实现：`POST /api/jobs/{job_id}/cancel`，JobManager 设置取消标记，`rank_range` 的逐日拉取、`daily_rank_float_weight` 的停牌市值回退等长循环会协作式检查并返回 `cancelled` 状态。
