"""[데이터 모양 정하기]

들어오는 데이터(대화)와 나가는 데이터(약속 카드 초안)가
'어떤 모양이어야 하는지' 를 정해둔 파일입니다.

여기서 모양을 정해두면, 이상한 데이터가 들어왔을 때
서버가 알아서 "형식이 틀렸어요!" 하고 걸러줍니다. (그러면 422 오류)
"""

import re
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# 약속 종류 코드는 이 네 가지 중 하나만 허용합니다.
# WALK(산책) / PLAY(나들이) / HOSPITAL(병원 동행) / OTHER(그 외 종류). 없으면 null.
ALLOWED_MEETING_TYPES = ("WALK", "PLAY", "HOSPITAL", "OTHER")

# 기준 날짜가 "YYYY-MM-DD" 모양인지 검사할 때 쓰는 규칙입니다.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Message(BaseModel):
    """대화 한 줄입니다. '누가(sender)' '무슨 말(content)' 을 '언제(sent_at)' 했는지 담습니다."""

    sender: str = Field(..., description="말한 사람", examples=["초코 보호자"])
    content: str = Field(..., description="한 말", examples=["내일 저녁 7시에 중앙공원에서 산책할까요?"])
    # 보낸 시각. "2026-07-24T18:00:00+09:00" 같은 형식이면 자동으로 인식합니다.
    sent_at: datetime = Field(..., description="메시지 보낸 시각(ISO 8601)")

    # sender 와 content 가 비어있지(공백만 있지) 않은지 검사합니다.
    @field_validator("sender", "content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()  # 앞뒤 공백은 잘라냅니다.


class MeetingDraftRequest(BaseModel):
    """서버에 보내는 요청 모양입니다. (Spring Boot 가 보내주는 데이터)"""

    room_id: str = Field(..., description="대화방 ID", examples=["room-1"])
    # "내일", "모레" 같은 말을 진짜 날짜로 바꿀 때 기준이 되는 '오늘 날짜'.
    reference_date: str = Field(
        ...,
        description="상대 날짜 계산 기준일 (YYYY-MM-DD)",
        examples=["2026-07-24"],
    )
    # 대화 목록. 최소 2개 ~ 최대 200개 여야 합니다. (범위를 벗어나면 422 오류)
    # Gemma가 한 번에 읽을 수 있는 양을 고려해, 하루치 대화를 넉넉히 담도록
    # 상한을 200으로 둡니다.
    messages: List[Message] = Field(
        ...,
        min_length=2,
        max_length=200,
        description="시간 순서대로 정렬된 대화 목록 (2~200개)",
    )

    # 기준 날짜가 "YYYY-MM-DD" 모양인지 검사합니다.
    @field_validator("reference_date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        if not _DATE_RE.match(value):
            raise ValueError("reference_date 는 YYYY-MM-DD 형식이어야 해요.")
        return value


class MeetingDraft(BaseModel):
    """대화에서 뽑아낸 '약속 카드 초안' 입니다. (네 가지 항목만 담습니다)"""

    meeting_type: Optional[str] = Field(
        default=None,
        description="약속 종류 코드 (WALK 산책 / PLAY 나들이 / HOSPITAL 병원 동행 / OTHER 그 외) 또는 null",
    )
    date: Optional[str] = Field(default=None, description="약속 날짜 (YYYY-MM-DD) 또는 null")
    time: Optional[str] = Field(default=None, description="약속 시각 (HH:MM) 또는 null")
    place: Optional[str] = Field(default=None, description="약속 장소 또는 null")


class ErrorResponse(BaseModel):
    """문제가 생겼을 때 돌려주는 응답 모양입니다."""

    detail: str = Field(..., description="무엇이 잘못됐는지 설명하는 글")
