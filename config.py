import sys
import logging

# 日志配置
# Tushare token 统一存放在仓库根 config_store.py 管理的 .quant-learning/settings.json
# （申万与 holders 共用）；仅 vnpy 示例部分仍从 ~/.vntrader/vt_setting.json 读取。
# 项目已弃用 .env 环境变量。
logger = logging.getLogger()
handler = logging.StreamHandler(sys.stderr)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
