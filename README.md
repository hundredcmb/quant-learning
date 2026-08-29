# quant-learning

A 股复盘与投研辅助项目（量化为辅），主要用于辅助每日复盘与投研分析。

本项目是一套可直接运行的 Python 脚本，分为**互不依赖的两部分**：

- **申万行业分析**（`shenwan_industry/`）：申万 2021 三级行业分类树 + 单日/区间行业涨幅榜（等权 / 流通市值加权）+ 样本空间三档（全A/中证800/中证1800，一次全算、Web 即时切换、默认全A）+ 行业财务指标（单日榜为当日时点、区间榜（官方逐日链式）为**区间末交易日时点值**：**PE**、**净利润同比** 与 **ROE**（净利润口径四选一：归母/扣非 × TTM/动态，Web 下拉切换、默认归母-TTM；同比扭亏/转亏/加大亏损/减少亏损类别化（加大亏损 < 减少亏损 < 转亏排序）；ROE 为加权平均算法、随"ROE算法"下拉选择）、**PB** 归母普通股股东权益、**股息率**（总额法 DPS 双口径：TTM估算值/静态），PE/PB 各自由流通 / 总市值两种合成口径，计算方法见 `shenwan_industry/docs/financial_indicators.md`），Web 可视化界面。**已彻底脱离 vnpy**，任意 Python 3.10+ 环境即可运行
- **十大股东席位关键词分析**（`holders/`）：A 股股票十大股东 + ETF 十大持有人（国家队、社保、险资等席位筛选，含持仓市值、变动对比与区间收益）。同样**不依赖 vnpy**，与申万行业分析共用 `.venv` 环境，token 也共用同一份配置
- **vnpy 入门示例**（`vnpy_examples/`）：K 线入库、图表、指标、回测。**必须依赖 vnpy 客户端（veighna studio）环境**

---

# 第一部分：不依赖 vnpy 的部分（shenwan_industry / holders）

## 1. 环境准备

两部分共用一个虚拟环境：申万模块需要 `fastapi / uvicorn / pydantic / tushare / pandas`（GUI 桌面窗口另需 `PySide6`），holders 另需 `Pillow` 出图；全部依赖都已在 requirements.txt 中，安装一次即可。在项目根目录逐行执行：

```bash
# 创建虚拟环境（Python 3.10+）
python3 -m venv .venv

# 安装依赖（requirements.txt 已包含全部所需包）
.venv/bin/python -m pip install -r requirements.txt
```

> Windows（PowerShell）：
>
> ```powershell
> python -m venv .venv
> .venv\Scripts\python.exe -m pip install -r requirements.txt
> ```
>
> 国内网络若访问 pypi.org 超时，可加镜像源：`... -i https://pypi.tuna.tsinghua.edu.cn/simple`

## 2. 配置 Tushare token

1. 启动 Web 服务（见下节）
2. 打开页面，点击右上角「数据配置」，填写你的 Tushare token 并保存
3. 可点「测试」验证 token 有效性

token 保存在项目根目录 `.quant-learning/settings.json`（已 gitignore，不随仓库提交，权限 600），申万 Web/CLI 与 holders 各脚本**共用这份配置**，只需配置一次。两种配置方式等效：

- **Web 页面填写**：申万 Web 服务右上角「数据配置」（上文步骤 1-3）
- **命令行参数**：holders 各脚本支持 `--token <你的token>`，第一次运行任一脚本时传入即可自动保存供未来使用；定时任务等场景每次显式传参也可以

既没有已保存配置也不传 `--token` 时，holders 脚本会直接报错退出。

> Tushare token 在 [tushare.pro](https://tushare.pro) 注册后获取，需开通 `stock_basic`、`daily`、`daily_basic`、`index_classify`、`index_member_all`、`sw_daily` 等接口权限；单日榜 PE/PB 另需 **VIP 接口 `fina_indicator_vip`**（需对应积分；积分不足时指标列显示"—"，涨幅榜不受影响，见 `shenwan_industry/docs/financial_indicators.md`）。

## 3. 启动 Web 服务

```bash
.venv/bin/python -m shenwan_industry.web.server --host 127.0.0.1 --port 9010
```

浏览器访问 <http://127.0.0.1:9010/>，支持：

- 单日排行 / 区间排行（等权 / 流通市值加权，L1/L2/L3 三级；两榜均含行业 PE/PB/ROE/股息率/净利润同比列——单日榜为当日时点、区间榜（官方逐日链式，Web 默认）为**区间末交易日时点值**（表头注明），PE/ROE/股息率随加权方式显示对应市值口径（等权为空）、PE/ROE 另随"净利润口径"下拉切换四档，股息率随"股息率口径"下拉切换，净利润同比无市值维度等权也显示）
- 行业成分股子表
- 一级行业官方指数 K 线（成交额/成交量切换）
- 任务进度条与取消

带桌面窗口的启动器（Windows 双击 `pythonw.exe` 运行，Linux/macOS 直接用 python）：

```powershell
.venv\Scripts\pythonw.exe shenwan_industry\web\desktop.pyw   # Windows
```

```bash
.venv/bin/python shenwan_industry/web/desktop.pyw             # Linux / macOS
```

## 4. CLI 使用

```bash
.venv/bin/python shenwan_industry/daily_ranking.py   # 单日涨幅榜（日期在文件内配置）
.venv/bin/python shenwan_industry/range_ranking.py   # 区间涨幅榜（区间在文件内配置）
.venv/bin/python -m shenwan_industry.dividend_cache             # 分红缓存增量刷新+体检（股息率列日常自动增量，无需手动跑）
.venv/bin/python -m shenwan_industry.dividend_cache --full      # 强制全量重建分红缓存（首刷约12分钟）
.venv/bin/python -m shenwan_industry.dividend_cache --check 600036.SH 600519.SH   # 抽查个股分红事件与双口径股息率
```

CLI 与 Web 使用同一份 token 配置（第 2 节），运行结束会输出耗时分析与 API 调用次数。

## 5. 功能说明

行业树优先从本地 `data/SW2021.json` 构建（备用 Tushare `index_classify`）；涨跌幅由 `close/pre_close` 自行重算；流通市值加权对停牌股做「停牌前最近流通市值」回退（最长 730 天）。单日榜财务指标：PE 用净利润四口径（归母/扣非 × TTM/动态，默认归母-TTM；TTM 滚动 12 月、不足四期按 4/k 年化，动态 = 最新期累计 × 4/k；接口无归母绝对额，由 `profit_dedt + extra_item` 行内合成；年报披露前有业绩快报（`express_vip`）则提前以快报值参与，审定值披露后自动切回；净利润同比同四口径，TTM 式比 TTM(D)/TTM(D-1年)、动态式比最新期/去年同季累计）、PB 用 `balancesheet_vip` 归母普通股股东权益绝对额（归母权益−其他权益工具，时点值无年化、不经"每股×股本"折算），与 `fina_indicator_vip` 按报告期并行批拉、按 `ann_date` 做时点过滤，行业合成 = ∑市值 / ∑分摊股东值；股息率用 `dividend` 接口每股全历史持久缓存（首刷约12分钟一次性，之后自动增量），财年锚定+总额法自算（官方 dv_ratio/dv_ttm 为滚动窗口口径不采用），双口径「TTM估算值（默认，进行中财年宣告优先/外推补位）/静态（最近完整分红年度）」，详见 `shenwan_industry/docs/financial_indicators.md` 第 7 节。

## 6. 十大股东席位分析（holders）

在指定样本池（默认中证 800 + 中证 1000）中，按 `KEY_WORD_RATIO` 配置的席位关键词筛选十大股东，并按折算比例估算持仓市值（单位：亿元）。token 与申万模块共用同一份配置（第 2 节）：未保存过时用 `--token <你的token>` 运行任一脚本，传入后自动保存供未来使用；不传参且无保存配置则直接报错。

**披露闸门与未披露公司提示**：脚本启动时会校验所配报告期是否已过法定披露截止日（一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30，截止日次日起才可用），未到期的报告期直接报错退出、不做任何查询；查询阶段遇到尚无披露数据的公司，其空结果一律不写入缓存，并在运行结束时汇总打印这些公司（名称+代码）。

> **Tushare 积分要求与启动探测**：`top10_holders` **至少 2000 积分**、ETF 日线 `fund_daily` **至少 5000 积分**。所有脚本启动时会用公共档位探测（固定历史数据真实调用门槛接口）把账号归入三档：<2000 / 2000~5000 / ≥5000——股票脚本不足 2000、ETF 脚本不足 5000 时直接报错退出；通过后按档位自动设定全接口节流速率（2000~5000 档 3 次/秒、≥5000 档 7.5 次/秒）。仓库自带的原始数据缓存 `holders/stock/tushare_top10_holders_raw.json` 针对样本池 **中证 800 + 中证 1000 成分股**（约 1800 只），完整覆盖 **2025 年年报（`20251231`）及以后**（如 `20260331`）。

### 股票（holders/stock/）

除 `top10_holders_value.py` 外，另外两个脚本**单次运行同时输出「席位明细」与「按股票合并席位」两套结果**（控制台各一张表、图片独立文件名互不覆盖），无需再分开跑两个脚本。

| 脚本 | 功能 | 输出 |
| --- | --- | --- |
| `top10_holders_value.py` | 单报告期关键词筛选，统计原始 / 折算持仓 | 控制台表格 |
| `top10_holders_change.py` | 双报告期（`REPORT_PERIOD1` → `REPORT_PERIOD2`）持股变动对比，标记新增 / 增持 / 减持 / 不变 / 退出；附带按股票合并席位视图（整体变动百分比） | 控制台双表 + `output/持股变动表格.png` 与 `持股变动表格_合并版.png` |
| `top10_return_between_dates.py` | 同一报告期、两个交易日间的公允价值变动与收益率；附带按代码合并席位视图 | 控制台双表 + `output/股票组合收益统计_*_to_*.png`（明细/合并 × 完整/汇总 共 4 张） |

```bash
.venv/bin/python holders/stock/top10_holders_value.py
.venv/bin/python holders/stock/top10_holders_change.py
.venv/bin/python holders/stock/top10_return_between_dates.py
.venv/bin/python holders/stock/cache_stats.py          # 缓存体检：各报告期覆盖统计（不联网，--json 可导出）
```

### ETF（holders/etf/）

**A 股 ETF 的十大持有人无法从 Tushare 获取**，只能手动录入缓存：先用 Excel 整理持有人数据，再运行 `import_etf_data.py` 导入（导入格式见 `holders/etf/README.md`）；ETF 日线行情从 Tushare `fund_daily` 直接获取（**至少需要 5000 积分**）。两个分析脚本同样单次运行输出「席位明细」+「按代码合并持有人」两套视图。

```bash
.venv/bin/python holders/etf/import_etf_data.py
.venv/bin/python holders/etf/etf_top10_holders_value.py
.venv/bin/python holders/etf/etf_top10_holders_change.py
.venv/bin/python holders/etf/etf_top10_return_between_dates.py
.venv/bin/python holders/etf/cache_stats.py            # 缓存体检：持有人报告期覆盖 + 基础信息录入情况（不联网）
```

运行前可在各脚本顶部「核心配置」区修改样本池指数、报告期、交易日和关键词；公共数据获取逻辑（token、缓存、限流、指数成分股、收盘价、并发查询）分别集中在 `holders/stock/tushare_client.py` 与 `holders/etf/etf_client.py`。生成的图片统一输出到 `output/` 目录（已 gitignore）；Tushare 原始数据缓存放于 `holders/` 下并随仓库提交，请勿删除（全量重新拉取受限流影响很慢）。

---

# 第二部分：依赖 vnpy 的部分（vnpy_examples）

本部分仅剩 **vnpy 入门示例**，需要 **vnpy 客户端（veighna studio）** 提供完整环境（vnpy、tushare、pandas、TA-Lib、PySide6 等）。vnpy 客户端下载：https://www.vnpy.com/

## 1. 安装 veighna studio

下载安装后自带 Python 3.13 与完整 vnpy 环境。**veighna 的安装路径因人而异**，请先记下本机 veighna 自带 Python 的路径（如 Windows `D:\Tools\veighna_studio\python.exe`、macOS `/Applications/veighna_studio/bin/python`），下文简称 `<veighna python>`。

## 2. 创建 vnpy 虚拟环境（逐行执行）

基于 veighna Python 创建继承其全部依赖的虚拟环境 `.venv-vnpy`：

Linux / macOS：

```bash
cd <你的项目根目录>
"<veighna python>" -m venv --system-site-packages .venv-vnpy
.venv-vnpy/bin/python -m pip install -r requirements.txt   # 项目自身依赖（无额外包时也可跳过）
```

Windows（PowerShell）：

```powershell
cd <你的项目根目录>
& "<veighna python>" -m venv --system-site-packages .venv-vnpy
.venv-vnpy\Scripts\python.exe -m pip install -r requirements.txt
```

之后本部分所有命令统一用 `.venv-vnpy` 中的 Python（Windows 为 `.venv-vnpy\Scripts\python.exe`，Linux/macOS 为 `.venv-vnpy/bin/python`）。

## 3. 配置 Tushare token 与数据库（vt_setting.json）

**仅 `vnpy_examples/` 需要此配置**（申万与 holders 的 Tushare token 见第一部分第 2 节，两者相互独立、互不影响）。本部分通过 vnpy 全局配置读取数据服务与数据库连接。配置文件位于 `~/.vntrader/vt_setting.json`（Windows 下即 `C:\Users\<用户名>\.vntrader\vt_setting.json`），首次运行 vnpy 客户端时会自动生成；也可以直接在客户端界面中修改。

配置示例（**把 `<>` 中的内容替换为你自己的 token 和数据库信息，切勿把真实信息提交到 git**）：

```json
{
    "font.family": "微软雅黑",
    "font.size": 12,
    "log.active": true,
    "log.level": 50,
    "log.console": true,
    "log.file": true,
    "email.server": "smtp.qq.com",
    "email.port": 465,
    "email.username": "",
    "email.password": "",
    "email.sender": "",
    "email.receiver": "",
    "datafeed.name": "tushare",
    "datafeed.username": "token",
    "datafeed.password": "<你的Tushare token>",
    "database.timezone": "Asia/Shanghai",
    "database.name": "mysql",
    "database.database": "<数据库名>",
    "database.host": "<数据库地址>",
    "database.port": 3306,
    "database.user": "<数据库用户名>",
    "database.password": "<数据库密码>"
}
```

说明：

- `datafeed.*`：数据服务配置。`name` 固定为 `tushare`，`password` 填你的 Tushare token（供示例脚本下载数据使用）
- `database.*`：数据库连接配置，供 vnpy 示例读写 K 线使用（`vnpy_examples/02、03、05、06` 通过 `get_database()`）
- 修改配置前请先关闭 vnpy 客户端，避免配置被覆盖

## 4. vnpy 入门示例（vnpy_examples/）

示例按学习路径编号，建议按顺序运行：

```bash
.venv-vnpy/bin/python vnpy_examples/01_settings.py
.venv-vnpy/bin/python vnpy_examples/02_bardata.py
.venv-vnpy/bin/python vnpy_examples/03_datafeed.py
.venv-vnpy/bin/python vnpy_examples/05_indicator.py
.venv-vnpy/bin/python vnpy_examples/06_ma_strategy.py
```

注意：

- `04_chart.py` 会启动 Qt 图形界面，需在带桌面的图形环境（Windows / Linux / macOS）下运行
- `06_ma_strategy.py` 的 `skip_ex` 参数：`0` = 忽略分红、使用不复权价格回测（走本地数据库，支持 vnpy 参数优化）；`1` = 每次分红前逃权（每次回测调用 Tushare，不支持参数优化）

---

# 使用 AI Agent 开发

Tushare 接口文档已随仓库提交：`docs/tushare_api_reference.md`（Tushare 官方 skills 的 `数据接口.md` 快照，覆盖 235+ 个 Tushare API）。开发中涉及 Tushare 数据获取时，应**优先查阅该文档**确认接口参数、返回字段与积分 / 权限要求，确保参数与结果解析正确；上游（https://github.com/waditu-tushare/skills.git）文档有更新时，获取最新版覆盖该文件即可（覆盖后保留文件头的来源说明）。

# 重要说明

- **Tushare 限流**：`holders/` 采用「同接口独立限流 + 跨接口并行」策略（仓库根公共节流器，与申万模块一致）。速率由启动时的公共积分档探测自动设定并留 10% 余量：2000~5000 积分档 ≤3 次/秒（约 180 次/分）、≥5000 积分档 ≤7.5 次/秒（约 450 次/分）。触发官方频次限制时该接口速率自动减半自愈；节流为进程内生效，多脚本并跑会叠加占用额度
- **数据缓存**：`holders/stock/tushare_top10_holders_raw.json` 是 Tushare 原始接口数据的本地缓存（约 8 MB，随仓库提交），结构为 `{报告期: {股票代码: [原始记录]}}`，覆盖 **中证 800 + 中证 1000 成分股** 的 **2025 年年报及以后**（积分低于 2000 时没有接口权限，只能使用该缓存，见上文）。已有缓存会优先使用，避免重复请求；请勿删除该文件。生成的图片输出到 `output/` 目录，已加入 `.gitignore`
- **关键词切换**：`KEY_WORD_RATIO` 按 T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组，统一在 `holders/stock/tushare_client.py` 中配置，启用或停用关键词通过注释切换，修改一处即可；如需某个脚本单独使用不同关键词，可在该脚本内重新定义覆盖
- **ETF 日线行情**：`fund_daily` 接口需要**至少 5000 Tushare 积分**，按 `trade_date` 一次请求拉全市场（`etf_client.get_daily_prices`），不建缓存、不做限流；低于 5000 积分无法拉取。ETF 十大持有人与基础信息仍只从缓存读取（手动导入）
- **信息安全**：Tushare token 统一保存在项目根目录 `.quant-learning/settings.json`（申万与 holders 共用，已 gitignore）；`~/.vntrader/vt_setting.json` 存放 vnpy 示例部分的 token 与数据库连接信息。两者切勿提交到 git。本项目已弃用 `.env` 环境变量
- **编码**：所有代码和文本均为 UTF-8，Windows 下用编辑器或脚本读写中文时请注意编码

# 免责声明

本项目仅供复盘与投研辅助使用，不构成任何投资建议。数据来源于 Tushare，使用请遵守其服务条款。
