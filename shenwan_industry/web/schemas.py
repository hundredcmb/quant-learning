"""申万行业 Web 接口的请求模型。"""

from datetime import date

from pydantic import BaseModel


class DailyRankingRequest(BaseModel):
    date: date


class RangeRankingRequest(BaseModel):
    start_date: date
    end_date: date


class TokenConfigRequest(BaseModel):
    token: str = ""
