# 直接运行本脚本时，把仓库根目录加入 sys.path，以便 import config 等根目录模块
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from config import logger
from vnpy.trader.setting import SETTINGS
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database


def settings_example() -> None:
    """
    - 查看当前 vnpy 配置
    - 如果使用的是 vnpy 客户端集成环境, 配置文件 ~/.vntrader/vt_setting.json
    - 可以直接在 客户端UI 中修改配置或直接修改配置文件
    """
    # 查看全部 vnpy 配置
    logger.info(json.dumps(SETTINGS, indent=2))

    # 初始化数据库对象, 类型为 SETTINGS["database.name"], 会自动设置数据库连接参数
    database = get_database()
    logger.info(type(database))

    # 初始化数据服务对象, 类型为 SETTINGS["datafeed.name"], 会自动设置 token 或 用户名密码
    datafeed = get_datafeed()
    logger.info(type(datafeed))


if __name__ == '__main__':
    settings_example()
