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
| `daily_basic`（全市场） | 单日自由流通市值/总市值/总股本 | `ts_code='', trade_date, fields='ts_code,close,total_mv,free_share,float_share,total_share', offset, limit=5999` | 循环直到不足一批 | 官方单次上限 6000，5999 留余量；自由流通市值 = `free_share × close`（三者同行取值，等价于 `circ_mv × free_share / float_share`）；`total_share`（万股）随请求缓存（PB 已改用 balancesheet_vip 权威净资产、不再用股本折算，字段保留） |
| `daily_basic`（单只） | 停牌回退查自由流通市值/总市值/总股本 | `ts_code, fields='trade_date,close,total_mv,free_share,float_share,total_share', start_date, end_date` | 不分页 | 响应按 `trade_date` 降序，取 ≤ date 最新一条；自由流通市值三字段须取自同一行；`total_share` 与总市值同行 |
| `fina_indicator_vip` | 单日榜财务指标：按报告期全市场拉扣非净利润、非经常性损益与每股净资产（归母净利润由前两者行内合成） | `period, fields='ts_code,ann_date,end_date,profit_dedt,extra_item,bps', offset, limit=9999`；也支持单只/多只 `ts_code` 查询 | offset/limit 分页循环直到不足一批 | **VIP 接口（需对应积分权限）**；`limit` 上限远高于 daily 类接口（实测 limit=9999/20000 均整批返回无截断）；全量单期 6870~8808 行（20260331 6870、20250630 8086、20250331 8808）→ 每期 **1 页**；**2022~2023 旧报告期行数触顶 9999 需翻 2 页**（实测 20220630/20220930/20221231/20230331 均触顶，净利润同比 TTM 式基期窗口 [D-36月, D-12月] 拉到旧期时分页循环自动处理）；当期窗口 8 期 + 增长基期新增 4 期（共享 4 期命中 period 缓存），单日全流程实测 fina 16 次请求；**8 期并发拉取**（8 线程，开始时刻由节流器统一 7.5/s 错开、往返并行，实测 ~1.4 秒）；`profit_dedt`=归属母公司扣非净利润、单位元、**年初至今累计值**（非单季）；`extra_item`=非经常性损益（自带正负号）；**归母净利润 = `profit_dedt + extra_item` 行内合成**（本接口无归母绝对额字段，实测请求 `n_income_attr_p` 被静默忽略；恒等式全市场实测 20250630 可对齐 6243 只、99.8% 相对误差 <0.1%、有扣非时 extra 缺失率 0%；仅同行两字段齐备才合成，不跨行拼接）；`bps`=每股净资产、单位元、**报告期末时点值**（非累计）；`ann_date`=报表公告日（实测无缺失）；**同股票同报告期有重复行（含 NaN 行，20250630 约 1600 只重复；实测 601318 两行 bps 均有效但值不同）**，实现为**字段级**各自取最后非空；fields 对**不存在的字段名静默忽略**（不报错），必须 `getattr` 防御；限流走独立节流器；与 balancesheet_vip/express_vip 池**并行拉取**、增长基期在主日期后串行补拉（见下行） |
| `balancesheet_vip` | 单日榜 PB：按报告期全市场拉归母权益与其他权益工具（合成归母普通股股东权益） | `period, fields='ts_code,ann_date,end_date,report_type,total_hldr_eqy_exc_min_int,oth_eqt_tools', offset, limit=9999`；也支持单只 `ts_code` 查询 | offset/limit 分页循环直到不足一批 | **VIP 接口（5000 积分，实测本 token 可用）**；`total_hldr_eqy_exc_min_int`=股东权益合计(不含少数股东权益)=归母权益、单位元、**报告期末时点值**；`oth_eqt_tools`=其他权益工具**合计、已含优先股**（子项 `oth_eqt_tools_p_shr` 为"其中:优先股"，不可重复扣减）；**PB 分母 = 归母权益 − 其他权益工具 = 归母普通股股东权益**（与 fina 的 `bps` 分子严格同口径，实测 20250407 对账：招行 12260−1804=10456 亿 == bps×当日股本分毫不差、华能等永续债大户隐含股本吻合 0.001%）；`report_type` 不传时**服务端默认只返回 '1'（合并报表）**（实测 20250630 全 6927 行均为 '1'），代码仍逐行过滤非 '1' 防御；全量单期 6927~7000 行（20250630 6927、20241231 7000）→ 每期 **1 页**、8 期共 8 次请求；**与 fina_indicator_vip 池并行拉取**（各自 8 期并发 + 池间并行、节流独立，实测两池总墙时 ~1.4s ≈ 单池）；同股票同报告期有重复行（20250630 有 611 只双行、update_flag 0/1 值相同），字段级各自取最后非空；`ann_date` 与两金额字段实测零缺失；`oth_eqt_tools` 缺列时告警（fields 静默忽略防御）；北交所（430xxx）与部分次新缺行（20241231 较 fina 少 259 只，均不在申万股票池内）；偶见畸形代码 `833243!1.BJ`（无害） |
| `express_vip` | 单日榜 PE 归母口径：按报告期全市场拉业绩快报（归母净利润提前可用源） | `period, fields='ts_code,ann_date,end_date,n_income', offset, limit=9999`；也支持单只 `ts_code` 查询 | offset/limit 分页循环直到不足一批 | **VIP 接口（5000 积分，实测本 token 可用）**；`n_income`=**归母净利润**（交易所快报模板口径；实测 20241231 对账：招行快报 1483.91 亿与利润表归母分毫不差、与含少数 1495.59 亿不符，1160 只偏差符号无系统性偏正），单位元、**年初至今累计值**、**未审计初步数**（is_audit 0 占 1392/1402；与审定值中位差 ~0.7%、43% 差 >1%、~10% 差 >10%）；`ann_date`=快报公告日（实测无缺失）；**覆盖部分**（20241231 期 1409 行/1403 只 ≈ 21% 公司、20240630 仅 100 只，集中在年报季）；**同期多行为真修正**（ann_date 不同，实测 20241231 有 6 只，如 601231 先发 NaN 行后补全）——实现为**版本级**选择（保留多版本按 ann_date ≤ D 取最新，n_income NaN 版本丢弃），与 fina 的同日双行字段级去重不同档；与 fina/bs 池并行拉取（三池各自 8 期并发 + 池间并行、节流独立，实测三池总墙时 ~1.5s）；PIT 双源合并规则（审定优先/快报兑现）见 AGENTS.md 第 5.1 节 |

## 调用约定

- token 从本机配置读取（**仓库根公共模块** `config_store.py`，与 `holders/` 共享同一份本地配置；存储与填写方式见 `shenwan_industry/AGENTS.md`「运行环境与数据源」），代码中不出现真实 token；模块不依赖 vnpy
- API 调用计数：`MarketDataProvider` 构造时包装 `pro` 并累计，`snapshot_api_calls()` 取快照；Web 任务前后快照求差即该任务实际调用次数（缓存命中不计；建树阶段调用不计入任务）
- 限流（已实测）：本账号 5000 积分，单接口限流 **500 次/分钟**（按 60 秒滚动窗口计数，官方报错信息原文确认；本地文档未列具体数字）。区间榜一次约 78 次调用（daily 66 + daily_basic 6 + 其他 6），远低于上限，可安全并发；但同一分钟连续跑多次区间会累积，批量任务需按窗口留余量。项目节流器（按接口独立 ≤450 次/分钟、进程内生效）见 `shenwan_industry/AGENTS.md` 第 7 节
- 接口名、参数、字段与权限要求以 `docs/tushare_api_reference.md` 为准，不要凭记忆硬写
