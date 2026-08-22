# 申万模块：未来规划与 Web 优化

本文件为 `shenwan_industry/AGENTS.md` 的子文档，记录未来工作方向。

## 未来规划：自建行业指数（核心工作）

自建申万行业指数（K 线）是项目未来核心工作（官方指数不稳定且种类少），前置条件是**历史成分完整**。现状（已修部分与剩余缺口详见 `known_issues.md` 第 13 条）与计划：

- **实现方案（已落地一部分）**：
  - **历史成分归属已落地（2026-08-20）**：`index_member_all` 默认(Y，当前) + `is_new='N'`（历史退出）每次构建实时拉全量（约 7901 行、数次请求），内存拼成每股 in/out 区间 `ts_code_membership`；`filter_stock_pool` / `get_stock_industry_nodes(ts_code, date)` 已按日期（date-aware）过滤与归属（见 AGENTS.md 第 2 节）。**不落盘、不缓存**——曾考虑 `historical_membership.json` 周刷缓存，因历史成分直接影响涨幅、需至少日级新鲜而放弃
  - 数据质量注意点（原逐股 `index_member` 方案确认，改 Y+N 后同样适用）：L3 `in_date` 存在回填不一致（实测顺丰 L3"快递"in_date=20100323 早于 L1 切换日 20170303）；借壳换代码股票（三六零 601313→601360）历史挂在现代码下；2021-12-13 前后有版本切换记录（实测中免、中公），历史归属按 SW2021 口径回填、与当时官方版本可能不一致（见 known_issues 第 23 条）；已从申万彻底移除（无 Y/N 记录）的股票无法解析历史归属
- **指数构建（点位输出，后续工作）**：逐日链式累乘 `指数点位 = 前日点位 × (1 + 当日加权涨幅)`；涨跌幅用 `daily.pre_close`（交易所除权参考价口径，见 `shenwan_industry/AGENTS.md` 第 3 节）；加权口径（自由流通，见 `sync_progress.md`）、新股纳入规则（4.4.3）等需与官方指数对账。**区间累计形态已先行落地（2026-08-23）**：`rank_range_chain` 官方逐日链式区间榜（Web 区间查询默认即此，静态版仅 CLI/API 对照），缺的是点位基准与序列输出

## Web 服务未来优化

- 当前 `web/jobs.py` 采用单 worker 串行队列，主要为了避免 Tushare 接口限流和 `ShenWanIndustryTree` / `MarketDataProvider` 可变状态（行业树、按日行情缓存、API 计数）并发冲突。后续若改为多 worker，需要先把行业树/行情缓存改成线程安全访问，并增加按 Tushare 每分钟调用上限的全局限流器。
- 任务取消已实现：`POST /api/jobs/{job_id}/cancel`，JobManager 设置取消标记，`rank_range` 的逐日拉取、`daily_rank_float_weight` 的停牌市值回退等长循环会协作式检查并返回 `cancelled` 状态。
