import json
from config import logger
from vnpy.trader.setting import SETTINGS


def log_settings():
    """查看当前配置, 其持久化在运行目录(默认是系统用户目录)里 /.vntrader/vt_setting.json"""
    logger.info(json.dumps(SETTINGS, indent=2))


if __name__ == '__main__':
    log_settings()
