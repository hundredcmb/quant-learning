import os
import json

import pandas as pd
from tushare.pro.client import DataApi


class ShenWanIndustryNode:
    def __init__(self, industry_code: str, industry_name: str, level: int):
        self.industry_code: str = industry_code  # 行业代码
        self.industry_name: str = industry_name  # 行业名称
        self.level: int = level  # 层级：0/1/2/3, 0是树根节点
        self.parent: ShenWanIndustryNode | None = None  # 父节点
        self.children: list[ShenWanIndustryNode] = []  # 子节点列表


class ShenWanIndustryTree:
    def __init__(self):
        self.root: ShenWanIndustryNode = ShenWanIndustryNode(industry_code="", industry_name="", level=0)
        self.industry_code_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业代码到节点的映射
        self.industry_name_to_node: dict[str, ShenWanIndustryNode] = {}  # 行业名称到节点的映射
        self.level_to_nodes: dict[int, list[ShenWanIndustryNode]] = {1: [], 2: [], 3: []}

    def build(self):
        current_file_path = os.path.abspath(__file__)
        current_dir_path = os.path.dirname(current_file_path)
        with open(f"{current_dir_path}/SW2021.json", "r", encoding="utf-8") as f:
            sw2021_list = json.load(f)
            for row in sw2021_list:
                self.parse_row(row)

    def build_by_tushare(self, pro: DataApi) -> None:
        df = pro.index_classify(src='SW2021')
        for _ix, row in df.iterrows():
            self.parse_row(row)

    def parse_row(self, row: dict[str, str] | pd.Series) -> None:
        level = row['level']
        industry_code = row['industry_code']
        industry_name = row['industry_name']
        parent_code = row['parent_code']

        node = ShenWanIndustryNode(industry_code=industry_code, industry_name=industry_name, level=level)
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


if __name__ == "__main__":
    tree = ShenWanIndustryTree()
    tree.build()
    l1 = tree.level_to_nodes[1]

    for node in l1:
        print(node.industry_code, node.industry_name)
        for child in node.children:
            print("    ", child.industry_code, child.industry_name)
            for child_child in child.children:
                print("        ", child_child.industry_code, child_child.industry_name)
