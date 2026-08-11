import sys
import logging

# 日志配置
# 本项目已弃用 .env 环境变量，所有配置（Tushare token、数据库连接等）
# 统一从 vnpy 全局配置动态获取（~/.vntrader/vt_setting.json）
logger = logging.getLogger()
handler = logging.StreamHandler(sys.stderr)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
