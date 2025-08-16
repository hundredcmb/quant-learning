# 数据库配置
import os
DB_MYSQL_URL = os.environ["DB_MYSQL_URL"] # 格式: "mysql+pymysql://username:password@host:port/dbname?charset=utf8mb4"

# 日志配置
import sys
import logging
logger = logging.getLogger()
handler = logging.StreamHandler(sys.stderr)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
