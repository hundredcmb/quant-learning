"""
申万行业树与成分数据层 (ShenWanIndustryTree)

- 行业树构建: 本地 SW2021.json 优先, tushare index_classify 备用
- 成分加载: index_member_all(当前Y+历史退出N) + stock_basic(L/D/P), 构建每股历史归属区间(ts_code_membership)
  与当前快照; 股票池过滤/归属解析按日期(date-aware)进行, 供历史日期榜单使用
- 股票池过滤: filter_stock_pool(锚点日期/末日参数化, 单日榜与区间榜共用)
- 行情/市值获取与缓存见 market_data.py (MarketDataProvider)
- 排行榜算法见 industry_ranking.py (单日榜 + 区间榜), 入口脚本见 daily_ranking.py / range_ranking.py
"""

import bisect
import os
import json
import warnings
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
from tushare.pro.client import DataApi

# 协作式取消检查: 需要取消时抛异常
CancelCheck = Callable[[], None]


class ShenWanIndustryNode:
    def __init__(self, index_code: str, industry_code: str, industry_name: str, level: str):
        self.index_code: str = index_code  # 指数代码
        self.industry_code: str = industry_code  # 行业代码
        self.industry_name: str = industry_name  # 行业名称
        self.industry_name_long: str = ""  # 行业名称, 1-2-3 级全称
        self.level: str = level  # 层级字符串："L1"/"L2"/"L3", 树根节点单独存在
        self.parent: ShenWanIndustryNode | None = None  # 父节点
        self.children: list[ShenWanIndustryNode] = []  # 子节点列表
        self.constituent_stocks: set[str] = set()  # 成分股代码列表, tushare 格式


class ShenWanIndustryTree:
    def __init__(self, tushare_pro: DataApi):
        self.root: ShenWanIndustryNode = ShenWanIndustryNode(
            index_code="",
            industry_code="",
            industry_name="",
            level="",
        )
        self.pro: DataApi = tushare_pro  # tushare pro api(仅构建行业树/成分时使用)
        self.index_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 指数代码到节点的映射
        self.industry_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业代码到节点的映射
        self.industry_name_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业名称到节点的映射
        self.level_to_nodes: dict[int, list[ShenWanIndustryNode]] = {1: [], 2: [], 3: []}
        self.constituent_stock_to_l3_node: dict[str, ShenWanIndustryNode] = {}
        self.stock_basic: dict[str, dict[str, str]] = {}  # 上市状态的股票 tushare 代码到信息的映射
        self.no_industry_stocks: set[str] = set()  # 没有行业代码的股票集合
        self.ts_code_to_in_date: dict[str, str] = {}  # 成分股 -> 纳入申万行业的日期(YYYYMMDD), 用于历史日期过滤
        self.ts_code_to_delist_date: dict[str, str] = {}  # 成分股 -> 退市日期(YYYYMMDD), 用于历史日期过滤
        self.ts_code_membership: dict[str, list[tuple[str, str, str | None]]] = {}  # 每股历史归属区间: [(l3_code, in_date, out_date|None), ...], 按 in_date 升序
        self.all_member_codes: set[str] = set()  # 有(过)申万行业归属的股票集合(Y∪N), 榜单股票池底
        self._trade_days_cache: dict[tuple[str, str], list[str]] = {}  # 新股"上市第6交易日"计数的交易日窗口缓存(精确匹配)
        self._trade_days_spans: list[tuple[str, str, list[str]]] = []  # 交易日历跨度缓存: (起, 止, 升序列表), 查询被包含时切片命中

    def build_industries(self):
        """从本地 JSON 数据源构建申万三级行业树"""
        current_file_path = os.path.abspath(__file__)
        current_dir_path = os.path.dirname(current_file_path)
        with open(f"{current_dir_path}/data/SW2021.json", "r", encoding="utf-8") as f:
            sw2021_list = json.load(f)
            for row in sw2021_list:
                self.parse_industry_row(row)
        self.build_industry_names()

    def build_industries_by_tushare(self) -> None:
        """从 tushare 数据源构建申万三级行业树, 数据长期不变, 更推荐使用 build_industries"""
        df = self.pro.index_classify(src='SW2021')
        for row in df.itertuples(index=False):
            self.parse_industry_row(row._asdict())
        self.build_industry_names()

    def build_industry_names(self):
        """生成所有行业名全称"""
        for node in self.level_to_nodes[1]:
            node.industry_name_long = node.industry_name
        for node in self.level_to_nodes[2]:
            node.industry_name_long = node.parent.industry_name + "-" + node.industry_name
        for node in self.level_to_nodes[3]:
            node.industry_name_long = node.parent.industry_name_long + "-" + node.industry_name

    def parse_industry_row(self, row: dict[str, str] | pd.Series) -> None:
        """解析申万行业数据行, 创建节点并添加到树中"""
        level = row['level']
        index_code = row['index_code']
        industry_code = row['industry_code']
        industry_name = row['industry_name']
        parent_code = row['parent_code']

        node = ShenWanIndustryNode(
            index_code=index_code,
            industry_code=industry_code,
            industry_name=industry_name,
            level=level,
        )
        self.index_code_to_node[index_code] = node
        self.industry_code_to_node[industry_code] = node
        self.industry_name_to_node[industry_name] = node

        if level == "L1":
            self.root.children.append(node)
            node.parent = self.root
            self.level_to_nodes[1].append(node)
        else:
            parent_node = self.industry_code_to_node[parent_code]
            parent_node.children.append(node)
            node.parent = parent_node

            if level == "L2":
                self.level_to_nodes[2].append(node)
            elif level == "L3":
                self.level_to_nodes[3].append(node)

    def build_constituent_stocks_by_tushare(self, filter_unlisted: bool = True) -> int:
        """
        从 tushare 数据源获取各个行业的股票列表并填充到对应节点。

        每次构建**实时拉取** index_member_all 两次: 默认(is_new=Y, 当前成分) + is_new='N'(历史退出,
        out_date 非空), 拼成每股完整历史归属区间 ts_code_membership(不落盘、不缓存, 见 roadmap)。
        - 当前成分(Y): 同时维护当前快照结构(constituent_stock_to_l3_node / 节点成分集合 / in_date)
        - 历史退出(N): 只入 ts_code_membership / all_member_codes, 不填当前快照; l3_code 无法入树则跳过并告警
        """
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if filter_unlisted and not self.stock_basic:
            df_l = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
            for row in df_l.itertuples(index=False):
                self.stock_basic[row.ts_code] = row._asdict()
            # 退市/暂停上市股票也纳入, 供历史日期分析使用(按退市日期截断)
            for status in ('D', 'P'):
                df_status = self.pro.stock_basic(
                    list_status=status,
                    fields='ts_code,name,list_date,delist_date',
                )
                for row in df_status.itertuples(index=False):
                    self.stock_basic[row.ts_code] = row._asdict()
                    delist_date = row.delist_date
                    if delist_date is not None and not pd.isna(delist_date):
                        self.ts_code_to_delist_date[row.ts_code] = str(delist_date)

        batch_size = 1999

        def _pull(is_new: str | None) -> list[tuple[str, str, str, str | None]]:
            """分页拉取 index_member_all, 返回 [(ts_code, l3_code, in_date, out_date|None)];
            is_new=None=当前成分(Y), 'N'=历史退出"""
            records: list[tuple[str, str, str, str | None]] = []
            offset = 0
            while True:
                kw = {"offset": offset, "limit": batch_size}
                if is_new is not None:
                    kw["is_new"] = is_new
                df = self.pro.index_member_all(**kw)
                if len(df) == 0:
                    break
                for row in df.itertuples(index=False):
                    in_date = getattr(row, "in_date", None)
                    out_date = getattr(row, "out_date", None)
                    in_s = str(in_date) if (in_date is not None and not pd.isna(in_date)) else ""
                    out_s: str | None = (
                        str(out_date) if (out_date is not None and not pd.isna(out_date)) else None
                    )
                    records.append((row.ts_code, row.l3_code, in_s, out_s))
                offset += len(df)
                if batch_size > len(df):
                    break
            return records

        count = 0  # 当前成分(Y) 数量, 与旧版一致
        y_records = _pull(None)
        n_records = _pull("N")

        # 当前成分(Y): 填当前快照 + 记 membership
        for ts_code, l3_code, in_s, out_s in y_records:
            if filter_unlisted and (ts_code not in self.stock_basic):
                continue
            if not (l3_node := self.index_code_to_node.get(l3_code)):
                raise ValueError(f"找不到 L3 行业代码 '{l3_code}' 对应的节点")
            self.all_member_codes.add(ts_code)
            self.ts_code_membership.setdefault(ts_code, []).append((l3_code, in_s, out_s))
            if in_s:
                self.ts_code_to_in_date[ts_code] = in_s
            l3_node.constituent_stocks.add(ts_code)
            l3_node.parent.constituent_stocks.add(ts_code)
            l3_node.parent.parent.constituent_stocks.add(ts_code)
            self.constituent_stock_to_l3_node[ts_code] = l3_node
            count += 1

        # 历史退出(N): 只入 membership / all_member_codes, 不入当前快照; l3 无法入树跳过并告警
        n_skipped = 0
        for ts_code, l3_code, in_s, out_s in n_records:
            if filter_unlisted and (ts_code not in self.stock_basic):
                continue
            if l3_code not in self.index_code_to_node:
                n_skipped += 1
                continue
            self.all_member_codes.add(ts_code)
            self.ts_code_membership.setdefault(ts_code, []).append((l3_code, in_s, out_s))
        if n_skipped:
            warnings.warn(
                f"index_member_all(is_new='N') 有 {n_skipped} 条 L3 行业代码无法入树, 已跳过",
                RuntimeWarning,
            )

        # 每股区间按 in_date 升序(缺失 in_date 排最前, 日期匹配时会被 in_date 非空检查排除)
        for ts_code in self.ts_code_membership:
            self.ts_code_membership[ts_code].sort(key=lambda rec: rec[1])

        return count

    def print_constituent_stocks(self):
        """打印申万一二三级行业及所有成分股"""
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if not self.constituent_stock_to_l3_node:
            raise RuntimeError("请先加载行业成分股")

        l1 = self.level_to_nodes[1]
        for n in l1:
            print(n.industry_code, n.index_code, n.industry_name)
            for child in n.children:
                print(" " * 4, child.industry_code, child.index_code, child.industry_name)
                for c_child in child.children:
                    print(
                        " " * 8,
                        c_child.industry_code,
                        c_child.index_code,
                        c_child.industry_name,
                        [self.stock_basic[s]['name'] for s in c_child.constituent_stocks],
                    )

    def filter_stock_pool(
        self,
        stock_pool: set[str],
        anchor_date: datetime,
        end_date: datetime,
        cancel_check: CancelCheck | None = None,
        restructure_excluded: set[str] | None = None,
    ) -> dict[str, list[str]]:
        """过滤股票池, 返回被剔除股票的类别明细 {类别: [ts_code, ...]}

        - 剔除缓存中记录的无行业分类的股票 (no_industry)
        - 剔除 anchor 日期无任何行业归属区间的成分 (not_member: 含未来纳入 in_date>anchor
          与历史已退出 out_date<=anchor, 避免前视偏差与历史退出残留)
        - 区间模式额外剔除 anchor 覆盖区间在 end 之前已结束的股票 (left_mid_range, 区间末前调出)
        - 剔除 end 日期之前已退市的股票 (delisted, 退市日当天及之前正常参与)
        - 剔除未上市 / 上市未满 6 个交易日的股票 (not_listed, 官方 4.4.3 新股上市第 6 个交易日才纳入)
        - 剔除当日处于 4.4.14 重整转增剔除窗口的股票 (restructure_window: 官方自除权日退出、
          转增股本上市日次一交易日重新计入; 窗口集合由 market_data.get_restructure_excluded 提供)
        单日榜调用传 (date, date); 区间榜传 (区间起始日, 区间末日)。
        """
        anchor_str = anchor_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        excluded: dict[str, list[str]] = {
            "no_industry": [],
            "not_member": [],
            "left_mid_range": [],
            "delisted": [],
            "not_listed": [],
            "restructure_window": [],
        }

        # 剔除缓存中记录的无行业分类的股票
        for no_industry_stock in self.no_industry_stocks:
            if no_industry_stock in stock_pool:
                stock_pool.discard(no_industry_stock)
                excluded["no_industry"].append(no_industry_stock)

        # 按历史归属区间: 剔除 anchor 日无覆盖区间的成分(未来纳入/历史退出), 区间末前调出的剔除
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            anchor_interval = self._get_interval_on(ts_code, anchor_str)
            if anchor_interval is None:
                stock_pool.discard(ts_code)
                excluded["not_member"].append(ts_code)
                continue
            _l3, _in, anchor_out = anchor_interval
            if anchor_out is not None and anchor_out <= end_str:
                stock_pool.discard(ts_code)
                excluded["left_mid_range"].append(ts_code)

        # 剔除 end 日期之前已退市的股票(退市后不再参与, 退市日当天及之前正常参与)
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            delist_date = self.ts_code_to_delist_date.get(ts_code)
            if delist_date is not None and delist_date < end_str:
                stock_pool.discard(ts_code)
                excluded["delisted"].append(ts_code)

        # 剔除未上市 / 上市未满 6 个交易日的股票(官方 4.4.3: 新股上市第 6 个交易日才纳入指数;
        # 注册制新股前 5 日无涨跌幅限制、波动剧烈, 过早计入会污染行业涨幅)
        # 快路径: list_date 距 anchor 超过 24 历日的必有 ≥6 个交易日(含周末/节假日余量), 直接放行;
        # 仅对近 24 历日上市的新股按交易日历精确计数(一次 trade_cal, 窗口在实例内缓存复用)
        borderline: list[tuple[str, str]] = []  # (ts_code, list_date_YYYYMMDD)
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            list_date_str = self.stock_basic.get(ts_code, {}).get('list_date')
            if pd.isna(list_date_str) or str(list_date_str).strip() == "":
                continue
            list_date_s = str(list_date_str)
            if datetime.strptime(list_date_s, "%Y%m%d") < (anchor_date - timedelta(days=24)):
                continue  # 早已上市, 必有 ≥6 个交易日
            borderline.append((ts_code, list_date_s))
        if borderline:
            earliest = min(d for _, d in borderline)
            if earliest > anchor_str:
                earliest = anchor_str
            days = self._trading_days_window(earliest, anchor_str)
            for ts_code, list_date_s in borderline:
                # [list_date, anchor] 内含 anchor 的交易日数 < 6 → 未满第 6 个交易日, 剔除
                if sum(1 for d in days if list_date_s <= d <= anchor_str) < 6:
                    stock_pool.discard(ts_code)
                    excluded["not_listed"].append(ts_code)

        # 剔除当日处于 4.4.14 重整转增剔除窗口的股票(官方自除权日退出、上市日次一交易日重新计入)
        if restructure_excluded:
            for ts_code in restructure_excluded:
                if ts_code in stock_pool:
                    stock_pool.discard(ts_code)
                    excluded["restructure_window"].append(ts_code)

        return excluded

    def _trading_days_window(self, start_str: str, end_str: str) -> list[str]:
        """[start_str, end_str] 区间内的交易日(升序, YYYYMMDD)

        先精确匹配, 再匹配已缓存的更宽跨度(如链式区间榜预取的 起点−24天然日), 子区间切片命中零请求
        """
        key = (start_str, end_str)
        cached = self._trade_days_cache.get(key)
        if cached is not None:
            return cached
        for span_start, span_end, days in self._trade_days_spans:
            if span_start <= start_str and end_str <= span_end:
                left = bisect.bisect_left(days, start_str)
                right = bisect.bisect_right(days, end_str)
                return days[left:right]
        df = self.pro.trade_cal(
            exchange='SSE',
            start_date=start_str,
            end_date=end_str,
            is_open='1',
            fields='cal_date',
        )
        result = sorted(df['cal_date'].astype(str).tolist())
        self._trade_days_cache[key] = result
        self._trade_days_spans.append((start_str, end_str, result))
        return result

    def _get_interval_on(self, ts_code: str, date_str: str) -> tuple[str, str, str | None] | None:
        """返回 ts_code 覆盖 date_str(YYYYMMDD) 的归属区间 (l3_code, in_date, out_date|None), 无则 None"""
        for l3_code, in_date, out_date in self.ts_code_membership.get(ts_code, ()):
            if in_date and in_date <= date_str and (out_date is None or out_date > date_str):
                return l3_code, in_date, out_date
        return None

    def get_l3_on(self, ts_code: str, date: datetime) -> ShenWanIndustryNode | None:
        """股票在指定日期的 L3 行业节点(按历史归属区间), 无覆盖区间返回 None"""
        rec = self._get_interval_on(ts_code, date.strftime("%Y%m%d"))
        return self.index_code_to_node.get(rec[0]) if rec else None

    def get_stock_industry_nodes(
        self,
        ts_code: str,
        date: datetime | None = None,
    ) -> tuple[ShenWanIndustryNode | None, ShenWanIndustryNode | None, ShenWanIndustryNode | None]:
        """根据股票代码获取其行业树节点; 传 date 时按历史归属区间解析当日所属行业

        - 不传 date: 退回当前快照查找(兼容旧调用)
        - 传 date: 按 ts_code_membership 取当日覆盖区间; 无任何归属记录视为数据异常(告警并记入
          no_industry_stocks), 有记录但当日不在任一行业则安静返回三 None(正常的历史退出情形)
        """
        if date is not None:
            if ts_code not in self.ts_code_membership:
                warnings.warn(f"找不到股票 '{ts_code}' 的历史行业归属", RuntimeWarning)
                self.no_industry_stocks.add(ts_code)
                return None, None, None
            l3_node = self.get_l3_on(ts_code, date)
            if l3_node is None:
                return None, None, None
        else:
            if not (l3_node := self.constituent_stock_to_l3_node.get(ts_code)):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L3 行业", RuntimeWarning)
                self.no_industry_stocks.add(ts_code)
                return None, None, None
        if not (l2_node := l3_node.parent):
            warnings.warn(f"找不到股票 '{ts_code}' 对应的 L2 行业", RuntimeWarning)
            self.no_industry_stocks.add(ts_code)
            return None, None, None
        if not (l1_node := l2_node.parent):
            warnings.warn(f"找不到股票 '{ts_code}' 对应的 L1 行业", RuntimeWarning)
            self.no_industry_stocks.add(ts_code)
            return None, None, None
        return l1_node, l2_node, l3_node
