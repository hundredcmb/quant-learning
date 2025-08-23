import os
import sys
import logging
from pathlib import Path

# 加载环境变量
env_path = Path(f"{os.path.dirname(os.path.abspath(__file__))}/.env")
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ[key.strip()] = value.strip()

# 数据库配置
DB_MYSQL_URL = os.environ["DB_MYSQL_URL"]

# 日志配置
logger = logging.getLogger()
handler = logging.StreamHandler(sys.stderr)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
