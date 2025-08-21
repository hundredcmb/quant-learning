import json
from config import logger
from vnpy.trader.setting import SETTINGS


def settings_example() -> None:
    """
    - 查看当前 vnpy 配置
    - 如果使用的是 vnpy 客户端集成环境, 配置文件 ~/.vntrader/vt_setting.json
    - 可以直接在 客户端UI 中修改配置或直接修改配置文件
    """
    logger.info(json.dumps(SETTINGS, indent=2))


if __name__ == '__main__':
    settings_example()
