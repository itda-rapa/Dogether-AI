"""오픈채팅 대화에서 경로 생성 의도를 추출할 때 쓰는 요청/응답 모양."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

MAX_ROUTE_CONTEXT_MESSAGES = 30


class RouteIntentMessage(BaseModel):
    sender_id: str
    content: str
    sent_at: datetime

    @field_validator("sender_id", "content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()


class RouteIntentRequest(BaseModel):
    room_id: str = Field(..., description="오픈채팅방 ID")
    messages: List[RouteIntentMessage] = Field(
        ..., min_length=1, max_length=MAX_ROUTE_CONTEXT_MESSAGES,
        description="오래된 순서로 정렬한 최근 채팅(최대 30개)",
    )

    @field_validator("room_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()


class RoutePlaceQuery(BaseModel):
    query: str = Field(..., description="대화에 실제로 나온 장소 검색어")

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("장소 검색어는 비워둘 수 없어요.")
        return value.strip()


class RouteIntentResult(BaseModel):
    status: Literal["READY", "INSUFFICIENT_CONTEXT"]
    route_mode: Optional[Literal["POINTS", "ROUND_TRIP"]] = None
    activity_type: Optional[Literal["WALK", "RUN", "CYCLE"]] = None
    start: Optional[RoutePlaceQuery] = None
    waypoints: List[RoutePlaceQuery] = Field(default_factory=list)
    destination: Optional[RoutePlaceQuery] = None
    target_distance_km: Optional[float] = Field(default=None, ge=0.5, le=50)
    message: Optional[str] = None


__all__ = [
    "MAX_ROUTE_CONTEXT_MESSAGES", "RouteIntentMessage", "RouteIntentRequest",
    "RouteIntentResult", "RoutePlaceQuery",
]
