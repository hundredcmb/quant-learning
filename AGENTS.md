# AGENTS.md

本文件是给 AI 编码代理（如 Codex）在本仓库工作时看的项目说明与约定。

## 项目简介

quant-learning 是一个 A 股量化学习项目（“量化小白从零开始学习量化”），目前包含四块内容：

- `holders/`：基于 Tushare 的十大股东席位关键词分析（国家队、社保、险资等），含多报告期对比与持仓公允价值变动统计
- `shenwan_industry/`：申万 2021 三级行业分类树，以及单日行业涨幅榜（等权 / 流通市值加权）
- `vnpy_examples/`：vnpy 学习示例（配置、K 线入库、数据服务下载、图表、指标、日线回测策略）
- `database/`：SQLAlchemy + MySQL 的会话封装

## 运行环境

- Python 3.10+（代码使用 `X | None`、`dict[str, ...]` 等新语法）
- 必须使用 vnpy 客户端（veighna studio）自带的 Python 运行；vnpy / vnpy_tushare / vnpy_ctastrategy / tushare / pandas / numpy / Pillow / TA-Lib 等由客户端环境提供，不要用 pip 单独安装；[requirements.txt](requirements.txt) 只包含项目自身依赖（pymysql、sqlalchemy）
- 项目已**弃用 `.env` 环境变量**，所有配置统一从 vnpy 全局配置 `~/.vntrader/vt_setting.json` 动态读取：
  - `datafeed.password` 存放 Tushare token，`datafeed.name` / `database.name` 决定数据源与数据库类型；`holders/` 和申万示例都从这里读 token
  - 数据库连接（`database.user` / `database.password` / `database.host` / `database.port` / `database.database`）同样从 vnpy 配置动态获取，`database/session.py` 据此构建 SQLAlchemy 连接串
- 脚本运行需要联网访问 Tushare Pro API，且 token 需开通对应接口权限（如 `top10_holders`）

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `config.py` | 仅提供全局 `logger`（配置统一从 vnpy `SETTINGS` 动态获取，无环境变量） |
| `database/session.py` | SQLAlchemy 引擎与 `db_session` 上下文管理器（自动提交/回滚/关闭），另有 FastAPI 风格 `get_db` |
| `holders/top10_holders_value.py` | 基础版：按关键词筛选十大股东席位，计算报告期持仓市值（原始/折算，单位亿元），仅控制台输出 |
| `holders/top10_holders_change.py` | 双报告期对比：`REPORT_PERIOD1 -> REPORT_PERIOD2` 的持股变动统计，生成表格图片 |
| `holders/top10_holders_change_merged.py` | 同 top10_holders_change，另含 `merge_holders_by_stock`（同一股票多个匹配席位合并统计） |
| `holders/top10_return_between_dates.py` | 单报告期、两个交易日（`REPORT_TRADE_DATE` vs `NEW_TRADE_DATE`）的公允价值变动与收益率，生成完整表格 + 汇总两张 PNG |
| `holders/tushare_top10_holders_raw.json` | Tushare 原始数据缓存（约 8 MB，已提交进仓库，请勿删除；覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后） |
| `output/` | 图片运行产物目录（已被 `.gitignore` 忽略） |
| `skills/` | Tushare 官方 skills（已克隆到本地，含 `SKILL.md` 与完整接口文档 `references/数据接口.md`；已被 gitignore，不随仓库提交） |
| `shenwan_industry/classification.py` | `ShenWanIndustryTree`：行业树构建、成分股加载、等权/流通市值加权涨幅排名 |
| `shenwan_industry/SW2021.json` | 申万 2021 行业分类本地数据（推荐的数据源） |
| `vnpy_examples/` | vnpy 学习示例目录（配置、数据、图表、指标、回测等），按编号顺序学习 |

## 常用命令

```bash
# 十大股东分析（token 在 vnpy 的 datafeed.password 中配置）
python holders/top10_holders_value.py
python holders/top10_holders_change.py
python holders/top10_holders_change_merged.py
python holders/top10_return_between_dates.py

# 申万行业涨幅示例
python shenwan_industry/classification.py

# vnpy 示例（需先配置好数据库与 tushare）
python vnpy_examples/01_settings.py
python vnpy_examples/02_bardata.py
python vnpy_examples/03_datafeed.py
python vnpy_examples/05_indicator.py
python vnpy_examples/06_ma_strategy.py
```

## 代码约定

- 注释、文档字符串、日志和输出均为中文；脚本通过 `if __name__ == "__main__":` 直接运行
- 脚本型代码把可调参数集中放在文件顶部“核心配置”区，注释掉的分支表示备选方案
- 日期统一为 `YYYYMMDD` 字符串；股票代码统一为 Tushare 格式（如 `600036.SH`）
- 持仓市值计算：`市值(亿) = hold_amount * close / 1e8`，折算市值 = 市值 × 席位折算比例
- 类型注解尽量完整；遇到 PyPI 包缺失，先确认是否由 vnpy 客户端环境提供，只有项目自身依赖才加入 requirements.txt

## 注意事项

### 十大股东分析（holders/）

- 四个脚本是同一套代码演化出的变体，存在大量复制粘贴，改动公共逻辑（限流、缓存、指数成分股获取）时要检查所有脚本
- `KEY_WORD_RATIO` 是“席位关键词 -> 折算比例”，按 T0 国家队 / T0 社保 / T1 平安 / T1 国寿 / T2 新华 / T2 太保 / T2 人保分组；启用或停用关键词通过注释切换，注意保持各脚本一致
- 生成的图片输出到仓库根目录 `output/`（已 gitignore）；缓存文件保持在 `holders/tushare_top10_holders_raw.json`（已提交进仓库，请勿删除），缓存结构为 `{报告期: {股票代码: [Tushare 原始记录列表]}}`，仅存接口原始数据、不含业务处理；修改业务逻辑时优先复用缓存，不要改变该结构
- 十大股东接口（如 `top10_holders`）至少需要 2000 Tushare 积分才有权限调用；低于 2000 积分时没有任何接口权限，只能使用仓库缓存分析其中已包含的数据（覆盖中证 800 + 中证 1000 成分股、2025 年年报及以后）
- 接口失败时脚本会 `save_raw_cache()` 后 `os._exit(-1)` 退出；Tushare 有限流，默认 `MAX_REQUESTS_PER_MINUTE=180`（建议比官方限制低 20）、`MAX_WORKERS=5`（上限 20）
- 缓存 JSON 约 8 MB 且已提交进仓库，避免无谓地让缓存文件进一步膨胀

### Tushare 数据获取

- 本仓库已克隆 Tushare 官方 skills 到 `skills/`（已被 `.gitignore` 忽略，不随仓库提交）。开发中需要获取 Tushare 数据时，**优先查阅** `skills/tushare/references/数据接口.md`（或 `skills/tushare-data/` 版本）确认接口名、必填/可选参数、返回字段与积分/频率限制，确保参数和结果解析正确，不要仅凭记忆硬写字段名
- Tushare token **统一通过 vnpy 接口动态获取**：`SETTINGS["datafeed.password"]`（写法见 holders 脚本），不要在代码中硬编码 token，也不要从其他环境变量读取

### 申万行业（shenwan_industry/）

- 行业树优先用本地 `SW2021.json` 构建（`build_industries()`），tushare 版本 `build_industries_by_tushare()` 仅作备用
- 流通市值加权算法对停牌股票做了特殊处理（回退查询停牌前最近流通市值），改动时不要破坏该逻辑

### vnpy 示例（vnpy_examples/）

- 按编号顺序学习即可，具体功能与参数说明见各脚本内注释

### 环境与 Git

- 本机是 Windows，PIL 图片默认用 `msyh.ttc` 字体（代码已做跨平台兜底）；PowerShell 读写中文文件时注意 UTF-8 编码
- `.gitignore` 已忽略 `.idea/` 与 `output/`（运行产物图片）；缓存 JSON 位于 `holders/` 且随仓库提交，不要忽略；新增生成文件时先确认是否应提交，`~/.vntrader/vt_setting.json` 中的真实 token / 数据库信息严禁提交
- 提交信息使用中文 Conventional Commits 风格（如 `feat:`、`fix:`），单行主题、简洁描述；开发分支建议使用 `codex/` 前缀
