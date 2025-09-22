import os
import json
import warnings
from datetime import datetime

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
        self.constituent_stocks: list[str] = []  # 成分股代码列表, tushare 格式


class ShenWanIndustryTree:
    def __init__(self):
        self.root: ShenWanIndustryNode = ShenWanIndustryNode(
            index_code="",
            industry_code="",
            industry_name="",
            level=0,
        )
        self.index_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 指数代码到节点的映射
        self.industry_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业代码到节点的映射
        self.industry_name_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业名称到节点的映射
        self.level_to_nodes: dict[int, list[ShenWanIndustryNode]] = {1: [], 2: [], 3: []}
        self.constituent_stock_to_l3_node: dict[str, ShenWanIndustryNode] = {}
        self.stock_basic: dict[str, dict[str, str]] = {}  # 上市状态的股票 tushare 代码到信息的映射

    def build_industries(self):
        """从本地 JSON 数据源构建申万三级行业树"""
        current_file_path = os.path.abspath(__file__)
        current_dir_path = os.path.dirname(current_file_path)
        with open(f"{current_dir_path}/SW2021.json", "r", encoding="utf-8") as f:
            sw2021_list = json.load(f)
            for row in sw2021_list:
                self.parse_industry_row(row)
        self.build_industry_names()

    def build_industries_by_tushare(self, pro: DataApi) -> None:
        """从 tushare 数据源构建申万三级行业树, 数据长期不变, 更推荐使用 build_industries"""
        df = pro.index_classify(src='SW2021')
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

    def build_constituent_stocks_by_tushare(self, pro: DataApi, filter_unlisted: bool = True) -> int:
        """
        从 tushare 数据源获取各个行业的股票列表并填充到对应节点
        """
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if filter_unlisted and not self.stock_basic:
            df = pro.stock_basic(list_status='L', fields='ts_code,name')
            for _ix, row in df.iterrows():
                self.stock_basic[row['ts_code']] = row.to_dict()

        count = 0
        offset = 0
        batch_size = 1999
        while True:
            df = pro.index_member_all(offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                if filter_unlisted and (ts_code not in self.stock_basic):
                    continue

                l3_code = row['l3_code']
                if l3_node := self.index_code_to_node.get(l3_code):
                    l3_node.constituent_stocks.append(ts_code)
                    l3_node.parent.constituent_stocks.append(ts_code)
                    l3_node.parent.parent.constituent_stocks.append(ts_code)
                    self.constituent_stock_to_l3_node[ts_code] = l3_node
                    count += 1
                else:
                    raise ValueError(f"找不到 L3 行业代码 '{l3_code}' 对应的节点")

            offset += len(df)
            if batch_size > len(df):
                break

        return count

    def daily_rank_equal_weight(
        self,
        pro: DataApi,
        date: datetime,
    ) -> tuple[list[tuple[str, float, int]], list[tuple[str, float, int]], list[tuple[str, float, int]]]:
        """
        获取指定日期的行业涨幅(等权)排名
        """
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if not self.constituent_stock_to_l3_node:
            raise RuntimeError("请先加载行业成分股")

        offset = 0
        batch_size = 5999
        date_str = date.strftime("%Y%m%d")
        tushare_code_to_pct_chg: dict[str, float] = {}
        while True:
            df = pro.daily(trade_date=date_str, offset=offset, limit=batch_size)
            if len(df) == 0:
                 break
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                pct_chg = row['pct_chg']
                tushare_code_to_pct_chg[ts_code] = pct_chg

            offset += len(df)
            if batch_size > len(df):
                break

        if not tushare_code_to_pct_chg:
            raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

        # 行业index_code -> (行业index_code, 上涨百分比, 成分股数量)
        l1_chg_map: dict[str, tuple[str, float, int]] = {}
        l2_chg_map: dict[str, tuple[str, float, int]] = {}
        l3_chg_map: dict[str, tuple[str, float, int]] = {}

        for node_l1 in self.level_to_nodes[1]:
            l1_chg_map[node_l1.index_code] = (node_l1.index_code, 0, 0)
        for node_l2 in self.level_to_nodes[2]:
            l2_chg_map[node_l2.index_code] = (node_l2.index_code, 0, 0)
        for node_l3 in self.level_to_nodes[3]:
            l3_chg_map[node_l3.index_code] = (node_l3.index_code, 0, 0)

        stock_pool: set[str] = set()
        for ts_code in tushare_code_to_pct_chg:
            stock_pool.add(ts_code)
        for ts_code in self.constituent_stock_to_l3_node:
            stock_pool.add(ts_code)

        for ts_code in stock_pool:
            pct_chg = tushare_code_to_pct_chg.get(ts_code, 0.0) # 有交易数据则用实际涨幅, 停牌则按0%

            if not (l3_node := self.constituent_stock_to_l3_node.get(ts_code)):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L3 行业", RuntimeWarning)
                continue
            if not (l2_node := l3_node.parent):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L2 行业", RuntimeWarning)
                continue
            if not (l1_node := l2_node.parent):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L1 行业", RuntimeWarning)
                continue

            for l_node, l_chg_map in [(l3_node, l3_chg_map), (l2_node, l2_chg_map), (l1_node, l1_chg_map)]:
                l_index_code, l_pct_chg, l_count = l_chg_map.get(l_node.index_code)
                l_count_new = l_count + 1
                l_pct_chg_new = (l_pct_chg * l_count + pct_chg) / l_count_new
                l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)

        # 对行业涨幅由大到小排序
        l1_rank_list = sorted(
            [item for item in l1_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        l2_rank_list = sorted(
            [item for item in l2_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        l3_rank_list = sorted(
            [item for item in l3_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )

        return l1_rank_list, l2_rank_list, l3_rank_list

    def daily_rank(
        self,
        pro: DataApi,
        date: datetime,
    ) -> tuple[list[tuple[str, float, int]], list[tuple[str, float, int]], list[tuple[str, float, int]]]:
        """
        获取指定日期的行业涨幅(等权)排名
        """
        if not self.root.children:
            raise RuntimeError("请先构建行业树结构")

        if not self.constituent_stock_to_l3_node:
            raise RuntimeError("请先加载行业成分股")

        offset = 0
        batch_size = 999
        date_str = date.strftime("%Y%m%d")
        tushare_code_to_circ_mv: dict[str, float] = {}
        while True:
            df = pro.daily_basic(
                ts_code='',
                trade_date=date_str,
                fields='ts_code,circ_mv',
                offset=offset,
                limit=batch_size,
            )
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                circ_mv = row['circ_mv']
                tushare_code_to_circ_mv[ts_code] = circ_mv

            offset += len(df)
            if batch_size > len(df):
                break

        if not tushare_code_to_circ_mv:
            raise ValueError(f"没有获取到 {date_str} 交易日的流通市值数据")

        offset = 0
        batch_size = 5999
        tushare_code_to_pct_chg: dict[str, float] = {}
        while True:
            df = pro.daily(trade_date=date_str, offset=offset, limit=batch_size)
            if len(df) == 0:
                break
            for _ix, row in df.iterrows():
                ts_code = row['ts_code']
                pct_chg = row['pct_chg']
                tushare_code_to_pct_chg[ts_code] = pct_chg

            offset += len(df)
            if batch_size > len(df):
                break

        if not tushare_code_to_pct_chg:
            raise ValueError(f"没有获取到 {date_str} 交易日的行情数据")

        # 行业index_code -> (行业index_code, 上涨百分比, 成分股数量)
        l1_chg_map: dict[str, tuple[str, float, int]] = {}
        l2_chg_map: dict[str, tuple[str, float, int]] = {}
        l3_chg_map: dict[str, tuple[str, float, int]] = {}

        # 行业index_code -> (新增流通市值总和, 上一交易日流通市值总和)
        l1_circ_map: dict[str, tuple[float, float]] = {}
        l2_circ_map: dict[str, tuple[float, float]] = {}
        l3_circ_map: dict[str, tuple[float, float]] = {}

        for node_l1 in self.level_to_nodes[1]:
            l1_chg_map[node_l1.index_code] = (node_l1.index_code, 0, 0)
            l1_circ_map[node_l1.index_code] = (0, 0)
        for node_l2 in self.level_to_nodes[2]:
            l2_chg_map[node_l2.index_code] = (node_l2.index_code, 0, 0)
            l2_circ_map[node_l2.index_code] = (0, 0)
        for node_l3 in self.level_to_nodes[3]:
            l3_chg_map[node_l3.index_code] = (node_l3.index_code, 0, 0)
            l3_circ_map[node_l3.index_code] = (0, 0)

        stock_pool: set[str] = set()
        for ts_code in tushare_code_to_pct_chg:
            stock_pool.add(ts_code)
        for ts_code in self.constituent_stock_to_l3_node:
            stock_pool.add(ts_code)

        for ts_code in stock_pool:
            pct_chg = tushare_code_to_pct_chg.get(ts_code, 0.0)  # 有交易数据则用实际涨幅, 停牌则按0%

            if not (l3_node := self.constituent_stock_to_l3_node.get(ts_code)):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L3 行业", RuntimeWarning)
                continue
            if not (l2_node := l3_node.parent):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L2 行业", RuntimeWarning)
                continue
            if not (l1_node := l2_node.parent):
                warnings.warn(f"找不到股票 '{ts_code}' 对应的 L1 行业", RuntimeWarning)
                continue

            data_list = [
                (l3_node, l3_chg_map, l3_circ_map),
                (l2_node, l2_chg_map, l2_circ_map),
                (l1_node, l1_chg_map, l1_circ_map),
            ]

            for l_node, l_chg_map, l_circ_map in data_list:
                l_index_code, l_pct_chg, l_count = l_chg_map.get(l_node.index_code)
                l_circ1, l_circ2 = l_circ_map.get(l_node.index_code)
                l_count_new = l_count + 1
                l_circ_mv = tushare_code_to_circ_mv.get(ts_code)
                if l_circ_mv is None:
                    df = pro.daily_basic(
                        ts_code=ts_code,
                        fields='ts_code,trade_date,circ_mv',
                        offset=0,
                        limit=1,
                    )
                    for _ix, row in df.iterrows():
                        l_circ_mv = row['circ_mv']
                        tushare_code_to_circ_mv[ts_code] = l_circ_mv
                        break

                    if l_circ_mv is None:
                        raise ValueError(f"没有获取到 {ts_code} 的流通市值数据")

                # 新增流通市值
                l_circ1_new = l_circ_mv * pct_chg / 100 / (pct_chg / 100 + 1) + l_circ1

                # 上一交易日的流通市值
                l_circ2_new = l_circ_mv / (pct_chg / 100 + 1) + l_circ2

                l_pct_chg_new = l_circ1_new / l_circ2_new * 100
                l_chg_map[l_node.index_code] = (l_index_code, l_pct_chg_new, l_count_new)
                l_circ_map[l_node.index_code] = (l_circ1_new, l_circ2_new)

        # 对行业涨幅由大到小排序
        l1_rank_list = sorted(
            [item for item in l1_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        l2_rank_list = sorted(
            [item for item in l2_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        l3_rank_list = sorted(
            [item for item in l3_chg_map.values() if item[2] > 0],
            key=lambda x: x[1],
            reverse=True,
        )

        return l1_rank_list, l2_rank_list, l3_rank_list


if __name__ == "__main__":
    import tushare as ts
    from vnpy.trader.setting import SETTINGS

    token = SETTINGS["datafeed.password"]
    pro = ts.pro_api(token=token)

    tree = ShenWanIndustryTree()
    tree.build_industries()
    stock_count = tree.build_constituent_stocks_by_tushare(pro=pro)

    # l1 = tree.level_to_nodes[1]
    # for n in l1:
    #     print(n.industry_code, n.index_code, n.industry_name)
    #     for child in n.children:
    #         print(" " * 4, child.industry_code, child.index_code, child.industry_name)
    #         for c_child in child.children:
    #             print(
    #                 " " * 8,
    #                 c_child.industry_code,
    #                 c_child.index_code,
    #                 c_child.industry_name,
    #                 [tree.stock_basic[s]['name'] for s in c_child.constituent_stocks],
    #             )

    l1_rank_list, l2_rank_list, l3_rank_list = tree.daily_rank_equal_weight(pro=pro, date=datetime(2025, 9, 22))
    for index_code, pct_chg, count in l2_rank_list:
        print(
            f"{'+' if pct_chg >= 0 else ''}{pct_chg:.2f}%",
            tree.index_code_to_node[index_code].industry_name_long,
            count,
            [f"{tree.stock_basic[s]['name']}({s})" for s in tree.index_code_to_node[index_code].constituent_stocks],
        )

    print("\n\n")

    l1_rank_list, l2_rank_list, l3_rank_list = tree.daily_rank(pro=pro, date=datetime(2025, 9, 22))
    for index_code, pct_chg, count in l2_rank_list:
        print(
            f"{'+' if pct_chg >= 0 else ''}{pct_chg:.2f}%",
            tree.index_code_to_node[index_code].industry_name_long,
            count,
            [f"{tree.stock_basic[s]['name']}({s})" for s in tree.index_code_to_node[index_code].constituent_stocks],
        )
