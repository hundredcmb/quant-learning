# 申万模块：Tushare 接口交互明细与限流

本文件为 `shenwan_industry/AGENTS.md` 的子文档（**强制核对流程必读**），记录模块与 Tushare 的接口交互约定。

## 接口交互明细

| 接口 | 用途 | 调用参数 | 分页/批次 | 备注 |
| --- | --- | --- | --- | --- |
| `index_classify` | 行业树（备用） | `src='SW2021'` | 一次全量 | 默认用本地 `data/SW2021.json`，仅备用 |
| `stock_basic` | 股票池状态过滤 | `list_status='L'/'D'/'P'`，D 带 `delist_date` | 每次调用不分页 | 上市+退市+暂停全部进 `stock_basic`；D 的 `delist_date` 进 `ts_code_to_delist_date` |
| `index_member_all` | 行业成分股 | `offset, limit=1999` | 循环直到不足一批 | 按 `l3_code` 挂到三级节点 |
| `trade_cal` | 区间交易日列表 | `exchange='SSE', start_date, end_date, is_open='1', fields='cal_date'` | 一次 | 区间榜取交易日用 |
| `daily` | 全市场单日行情 | `trade_date, offset, limit=5999` | 循环直到不足一批 | 涨跌幅自行从 `close/pre_close` 重算 |
| `daily_basic`（全市场） | 单日自由流通市值/总市值 | `ts_code='', trade_date, fields='ts_code,close,total_mv,free_share,float_share', offset, limit=5999` | 循环直到不足一批 | 官方单次上限 6000，5999 留余量；自由流通市值 = `free_share × close`（三者同行取值，等价于 `circ_mv × free_share / float_share`） |
| `daily_basic`（单只） | 停牌回退查自由流通市值/总市值 | `ts_code, fields='trade_date,close,total_mv,free_share,float_share', start_date, end_date` | 不分页 | 响应按 `trade_date` 降序，取 ≤ date 最新一条；自由流通市值三字段须取自同一行 |
| `sw_daily` | 申万行业指数日 K 线（Web；L1 全覆盖，L2/L3 按可用性） | `ts_code, start_date, end_date`；另支持仅 `trade_date` 一次拉全市场 | 一次（单次上限 4000 行） | 可用性判定：`trade_date` 全量拉取与 `data/SW2021.json` 求交集，**服务启动时后台探测一次、内存缓存（无文件）**，前端 `/api/index/available` 在探测完成前等待就绪 |

## 调用约定

- token 从本机配置读取（`shenwan_industry/config_store.py`，存储与填写方式见 `shenwan_industry/AGENTS.md`「运行环境与数据源」），代码中不出现真实 token；模块不依赖 vnpy
- API 调用计数：`MarketDataProvider` 构造时包装 `pro` 并累计，`snapshot_api_calls()` 取快照；Web 任务前后快照求差即该任务实际调用次数（缓存命中不计；建树阶段调用不计入任务）
- 限流（已实测）：本账号 5000 积分，单接口限流 **500 次/分钟**（按 60 秒滚动窗口计数，官方报错信息原文确认；本地文档未列具体数字）。区间榜一次约 78 次调用（daily 66 + daily_basic 6 + 其他 6），远低于上限，可安全并发；但同一分钟连续跑多次区间会累积，批量任务需按窗口留余量。项目节流器（按接口独立 ≤450 次/分钟、进程内生效）见 `shenwan_industry/AGENTS.md` 第 7 节
- 接口名、参数、字段与权限要求以 `docs/tushare_api_reference.md` 为准，不要凭记忆硬写
