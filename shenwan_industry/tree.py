"""
申万行业树与成分数据层 (ShenWanIndustryTree)

- 行业树构建: 本地 SW2021.json 优先, tushare index_classify 备用
- 成分加载: index_member_all + stock_basic(L/D/P), 记录 in_date / delist_date 供历史日期过滤
- 行情/市值获取: daily / daily_basic, 带按日内存缓存
- 排行榜逻辑见 ranking.py (单日榜 + 区间榜)
"""

import os
import json
import math
import warnings
from datetime import datetime, timedelta

import pandas as pd
from tushare.pro.client import DataApi


class ShenWanIndustryNode:
    def __init__(self, index_code: str, industry_code: str, industry_name: str, level: int):
        self.index_code: str = index_code  # 指数代码
        self.industry_code: str = industry_code  # 行业代码
        self.industry_name: str = industry_name  # 行业名称
        self.industry_name_long: str = ""  # 行业名称, 1-2-3 级全称
        self.level: int = level  # 层级：0/1/2/3, 0是树根节点
        self.parent: ShenWanIndustryNode | None = None  # 父节点
        self.children: list[ShenWanIndustryNode] = []  # 子节点列表
        self.constituent_stocks: set[str] = set()  # 成分股代码列表, tushare 格式


class ShenWanIndustryTree:
    def __init__(self, tushare_pro: DataApi):
        self.root: ShenWanIndustryNode = ShenWanIndustryNode(
            index_code="",
            industry_code="",
            industry_name="",
            level=0,
        )
        self.pro: DataApi = tushare_pro  # tushare pro api
        self.index_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 指数代码到节点的映射
        self.industry_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业代码到节点的映射
        self.industry_name_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业名称到节点的映射
        self.level_to_nodes: dict[int, list[ShenWanIndustryNode]] = {1: [], 2: [], 3: []}
        self.constituent_stock_to_l3_node: dict[str, ShenWanIndustryNode] = {}
        self.stock_basic: dict[str, dict[str, str]] = {}  # 上市状态的股票 tushare 代码到信息的映射
        self.no_industry_stocks: set[str] = set()  # 没有行业代码的股票集合
        self.ts_code_to_pct_chg_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股涨跌幅数据
        self.ts_code_to_circ_mv_cache: dict[datetime, dict[str, float]] = {}  # 日期 -> A股流通市值数据
        self.ts_code_to_in_date: dict[str, str] = {}  # 成分股 -> 纳入申万行业的日期(YYYYMMDD), 用于历史日期过滤
        self.ts_code_to_delist_date: dict[str, str] = {}  # 成分股 -> 退市日期(YYYYMMDD), 用于历史日期过滤

    def build_industries(self):
        """从本地 JSON 数据源构建申万三级行业树"""
        current_file_path = os.path.abspath(__file__)
        current_dir_path = os.path.dirname(current_file_path)
        with open(f"{current_dir_path}/SW2021.json", "r", encoding="utf-8") as f:
            sw2021_list = json.load(f)
            for row in sw2021_list:
                self.parse_industry_row(row)
        self.build_industry_names()

    def build_industries_by_tushare(self) -> None:
        """从 tushare 数据源构建申万三级行业树, 数据长期不变, 更推荐使用 build_industries"""
        df = self.pro.index_classify(src='SW2021')
        for _ix, row in df.iterrows():
            self.parse_industry_row(row)
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
        从 tushare 数据源获取各个行业的股票列表并填充到对应节点
        """
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if filter_unlisted and not self.stock_basic:
            df_l = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
            for _ix, row in df_l.iterrows():
                self.stock_basic[row['ts_code']] = row.to_dict()
            # 退市/暂停上市股票也纳入, 供历史日期分析使用(按退市日期截断)
            for status in ('D', 'P'):
                df_status = self.pro.stock_basic(
                    list_status=status,
                    fields='ts_code,name,list_date,delist_date',
                )
                for _ix, row in df_status.iterrows():
                    self.stock_basic[row['ts_code']] = row.to_dict()
                    delist_date = row['delist_date']
                    if delist_date is not None and not pd.isna(delist_date):
                        self.ts_code_to_delist_date[row['ts_code']] = str(delist_date)

        count = 0
        offset = 0
        batch_size = 1999
        while True:
            df = self.pro.index_member_all(offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                if filter_unlisted and (ts_code not in self.stock_basic):
                    continue

                in_date = row.get('in_date')
                if in_date is not None and not pd.isna(in_date):
                    self.ts_code_to_in_date[ts_code] = str(in_date)

                l3_code = row['l3_code']
                if l3_node := self.index_code_to_node.get(l3_code):
                    l3_node.constituent_stocks.add(ts_code)
                    l3_node.parent.constituent_stocks.add(ts_code)
                    l3_node.parent.parent.constituent_stocks.add(ts_code)
                    self.constituent_stock_to_l3_node[ts_code] = l3_node
                    count += 1
                else:
                    raise ValueError(f"找不到 L3 行业代码 '{l3_code}' 对应的节点")

            offset += len(df)
            if batch_size > len(df):
                break

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

    def get_ts_code_to_pct_chg(self, date: datetime) -> dict[str, float | None]:
        """获取某日的行情数据: ts_code -> 涨跌幅(%), 数据异常时为 None"""
        ts_code_to_pct_chg: dict[str, float | None] = self.ts_code_to_pct_chg_cache.get(date) or {}
        if ts_code_to_pct_chg:
            return ts_code_to_pct_chg

        offset = 0
        batch_size = 5999
        date_str = date.strftime("%Y%m%d")
        while True:
            df = self.pro.daily(trade_date=date_str, offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                pre_close = row['pre_close']
                close = row['close']
                if pd.isna(pre_close) or pd.isna(close):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    ts_code_to_pct_chg[ts_code] = None
                    continue
                pre_close_f = float(pre_close)
                close_f = float(close)
                if not (math.isfinite(pre_close_f) and pre_close_f > 0 and math.isfinite(close_f)):
                    warnings.warn(
                        f"跳过涨跌幅异常数据: {ts_code} {date_str} pre_close={pre_close} close={close}",
                        RuntimeWarning,
                    )
                    ts_code_to_pct_chg[ts_code] = None
                    continue
                pct_chg = (close_f - pre_close_f) / pre_close_f * 100
                ts_code_to_pct_chg[ts_code] = pct_chg

            offset += len(df)
            if batch_size > len(df):
                break

        if ts_code_to_pct_chg:
            self.ts_code_to_pct_chg_cache[date] = ts_code_to_pct_chg

        return ts_code_to_pct_chg

    def get_ts_code_to_circ_mv(self, date: datetime) -> dict[str, float]:
        """获取A股某日的流通市值数据: ts_code -> 流通市值"""
        ts_code_to_circ_mv: dict[str, float] = self.ts_code_to_circ_mv_cache.get(date) or {}
        if ts_code_to_circ_mv:
            return ts_code_to_circ_mv

        offset = 0
        batch_size = 5999  # 官方单次上限 6000, 留 1 余量; 全市场一次拉完
        date_str = date.strftime("%Y%m%d")
        while True:
            df = self.pro.daily_basic(
                ts_code='',
                trade_date=date_str,
                fields='ts_code,circ_mv',
                offset=offset,
                limit=batch_size,
            )
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                circ_mv = row['circ_mv']
                if pd.isna(circ_mv):
                    continue
                ts_code_to_circ_mv[ts_code] = circ_mv

            offset += len(df)
            if batch_size > len(df):
                break

        if ts_code_to_circ_mv:
            self.ts_code_to_circ_mv_cache[date] = ts_code_to_circ_mv

        return ts_code_to_circ_mv

    def filter_stock_pool(self, date: datetime, stock_pool: set[str]) -> None:
        """过滤股票池"""
        date_str = date.strftime("%Y%m%d")

        # 剔除缓存中记录的无行业分类的股票
        for no_industry_stock in self.no_industry_stocks:
            stock_pool.discard(no_industry_stock)

        # 剔除分析日期之后才纳入行业的成分(避免前视偏差)
        for ts_code in list(stock_pool):
            in_date = self.ts_code_to_in_date.get(ts_code)
            if in_date is not None and in_date > date_str:
                stock_pool.discard(ts_code)

        # 剔除分析日期晚于退市日的股票(退市后不再参与, 退市日当天及之前正常参与)
        for ts_code in list(stock_pool):
            delist_date = self.ts_code_to_delist_date.get(ts_code)
            if delist_date is not None and delist_date < date_str:
                stock_pool.discard(ts_code)

        # 剔除未上市的股票
        for ts_code in self.stock_basic:
            list_date_str = self.stock_basic[ts_code]['list_date']
            if pd.isna(list_date_str) or str(list_date_str).strip() == "":
                continue
            list_date = datetime.strptime(str(list_date_str), "%Y%m%d")
            if list_date >= date:
                stock_pool.discard(ts_code)

    def get_stock_industry_nodes(
        self,
        ts_code: str,
    ) -> tuple[ShenWanIndustryNode | None, ShenWanIndustryNode | None, ShenWanIndustryNode | None]:
        """根据股票代码获取其行业树节点"""
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
