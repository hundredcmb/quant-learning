"""申万行业 Web 接口的请求模型。"""

from datetime import date

from pydantic import BaseModel


class DailyRankingRequest(BaseModel):
    date: date


class RangeRankingRequest(BaseModel):
    start_date: date
    end_date: date
    chain: bool = True  # True=官方逐日链式(默认, Web 无选择 UI); False=静态权重(仅 API/CLI 对照用)


class TokenConfigRequest(BaseModel):
    token: str = ""
