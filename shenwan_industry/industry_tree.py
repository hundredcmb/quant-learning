"""
申万行业树与成分数据层 (ShenWanIndustryTree)

- 行业树构建: 本地 SW2021.json 优先, tushare index_classify 备用
- 成分加载: index_member_all + stock_basic(L/D/P), 记录 in_date / delist_date 供历史日期过滤
- 股票池过滤: filter_stock_pool(锚点日期/末日参数化, 单日榜与区间榜共用)
- 行情/市值获取与缓存见 market_data.py (MarketDataProvider)
- 排行榜算法见 industry_ranking.py (单日榜 + 区间榜), 入口脚本见 daily_ranking.py / range_ranking.py
"""

import os
import json
import warnings
from datetime import datetime
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
        从 tushare 数据源获取各个行业的股票列表并填充到对应节点
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

        count = 0
        offset = 0
        batch_size = 1999
        while True:
            df = self.pro.index_member_all(offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for row in df.itertuples(index=False):
                ts_code = row.ts_code
                if filter_unlisted and (ts_code not in self.stock_basic):
                    continue

                in_date = getattr(row, 'in_date', None)
                if in_date is not None and not pd.isna(in_date):
                    self.ts_code_to_in_date[ts_code] = str(in_date)

                l3_code = row.l3_code
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

    def filter_stock_pool(
        self,
        stock_pool: set[str],
        anchor_date: datetime,
        end_date: datetime,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, list[str]]:
        """过滤股票池, 返回被剔除股票的类别明细 {类别: [ts_code, ...]}

        - 剔除缓存中记录的无行业分类的股票 (no_industry)
        - 剔除 anchor 日期之后才纳入行业的成分 (in_date_later, 避免前视偏差)
        - 剔除 end 日期之前已退市的股票 (delisted, 退市日当天及之前正常参与)
        - 剔除 anchor 日期当天及之后才上市的股票 (not_listed)
        单日榜调用传 (date, date); 区间榜传 (区间起始日, 区间末日)。
        """
        anchor_str = anchor_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        excluded: dict[str, list[str]] = {
            "no_industry": [],
            "in_date_later": [],
            "delisted": [],
            "not_listed": [],
        }

        # 剔除缓存中记录的无行业分类的股票
        for no_industry_stock in self.no_industry_stocks:
            if no_industry_stock in stock_pool:
                stock_pool.discard(no_industry_stock)
                excluded["no_industry"].append(no_industry_stock)

        # 剔除 anchor 日期之后才纳入行业的成分(避免前视偏差)
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            in_date = self.ts_code_to_in_date.get(ts_code)
            if in_date is not None and in_date > anchor_str:
                stock_pool.discard(ts_code)
                excluded["in_date_later"].append(ts_code)

        # 剔除 end 日期之前已退市的股票(退市后不再参与, 退市日当天及之前正常参与)
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            delist_date = self.ts_code_to_delist_date.get(ts_code)
            if delist_date is not None and delist_date < end_str:
                stock_pool.discard(ts_code)
                excluded["delisted"].append(ts_code)

        # 剔除 anchor 日期当天及之后才上市的股票
        for idx, ts_code in enumerate(list(stock_pool)):
            if cancel_check is not None and idx % 500 == 0:
                cancel_check()
            list_date_str = self.stock_basic.get(ts_code, {}).get('list_date')
            if pd.isna(list_date_str) or str(list_date_str).strip() == "":
                continue
            list_date = datetime.strptime(str(list_date_str), "%Y%m%d")
            if list_date >= anchor_date:
                stock_pool.discard(ts_code)
                excluded["not_listed"].append(ts_code)

        return excluded

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
