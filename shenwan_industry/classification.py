import os
import json

import pandas as pd
from tushare.pro.client import DataApi


class ShenWanIndustryNode:
    def __init__(self, index_code: str, industry_code: str, industry_name: str, level: int):
        self.index_code: str = index_code  # 指数代码
        self.industry_code: str = industry_code  # 行业代码
        self.industry_name: str = industry_name  # 行业名称
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

    def build_industries_by_tushare(self, pro: DataApi) -> None:
        """从 tushare 数据源构建申万三级行业树, 数据长期不变, 更推荐使用 build_industries"""
        df = pro.index_classify(src='SW2021')
        for _ix, row in df.iterrows():
            self.parse_industry_row(row)

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
            offset += batch_size
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
                    raise ValueError(f"找不到L3行业代码: {l3_code} 对应的节点")

        return count


if __name__ == "__main__":
    import tushare as ts
    from vnpy.trader.setting import SETTINGS

    token = SETTINGS["datafeed.password"]
    pro = ts.pro_api(token=token)

    tree = ShenWanIndustryTree()
    tree.build_industries()
    stock_count = tree.build_constituent_stocks_by_tushare(pro=pro)

    l1 = tree.level_to_nodes[1]
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
                    c_child.constituent_stocks
                )
