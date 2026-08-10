"""[데이터 모양 정하기 - 단체 채팅방(v2)]

v1(1:1)과 달라지는 점은 딱 두 가지입니다.

들어올 때 : 방에 누가 있는지(명부, participants)가 함께 옵니다.
             그리고 발신자를 이름(sender)이 아니라 ID(sender_id)로 받습니다.
             같은 이름이 두 명이어도 ID 로는 구분되기 때문입니다.

나갈 때   : 카드마다 "이 약속에 누가 들어있는지"(participant_ids)가 붙습니다.
             백엔드는 여기 적힌 사람에게만 알림을 보냅니다.

v1 파일(schemas/meeting.py)은 고치지 않습니다. 여기서 물려받아 쓰기만 합니다.
"""

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, field_validator

from app.schemas.meeting import MeetingDraft, MeetingDraftRequest


class MessageV2(BaseModel):
    """단체 채팅방의 대화 한 줄입니다.

    v1 의 Message 와 달리 '이름' 이 아니라 '사용자 ID' 로 누가 말했는지 알려줍니다.
    이 ID 는 AI 에게 그대로 가지 않고 P1, P2 같은 이름표로 바뀌어서 갑니다.
    """

    sender_id: str = Field(..., description="말한 사람의 사용자 ID", examples=["u-101"])
    content: str = Field(
        ...,
        description="한 말",
        examples=["토요일 3시에 중앙공원 어때요?"],
    )
    sent_at: datetime = Field(..., description="메시지 보낸 시각(ISO 8601)")

    @field_validator("sender_id", "content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()


class MeetingDraftV2Request(MeetingDraftRequest):
    """단체 채팅방 요청 모양입니다. (Spring Boot 가 보내주는 데이터)

    v1 요청(MeetingDraftRequest)을 물려받아서 room_id, reference_date 와
    그 검사 규칙을 그대로 씁니다. 달라지는 부분만 여기서 다시 정합니다.

    participants 개수는 여기서 검사하지 않습니다. 3명 미만이면
    participant_mapper 가 걸러서 400 으로 답합니다.
    (스키마에서 걸면 422 가 되어 "인원이 안 맞음" 과 "형식이 틀림" 이 섞입니다.)
    """

    # 방에 있는 사람들의 ID 목록(명부)입니다. 순서대로 P1, P2, P3 … 이름표가 붙습니다.
    participants: List[str] = Field(
        ...,
        description="방에 있는 사람들의 사용자 ID 목록 (3명 이상)",
        examples=[["u-101", "u-102", "u-103", "u-104"]],
    )
    # 대화 목록. 개수 제한은 v1 과 같습니다. (2~200개, 벗어나면 422)
    # 담기는 모양만 Message -> MessageV2 로 바뀝니다.
    messages: List[MessageV2] = Field(
        ...,
        min_length=2,
        max_length=200,
        description="시간 순서대로 정렬된 대화 목록 (2~200개)",
    )


class MeetingDraftV2(MeetingDraft):
    """단체 채팅방에서 뽑아낸 약속 카드 초안입니다.

    v1 카드(종류·날짜·시각·장소)에 '이 약속에 속한 사람들' 이 하나 더 붙습니다.
    제안한 사람과 동의한 사람만 들어갑니다. 침묵한 사람과 거절한 사람은 빠집니다.
    """

    participant_ids: List[str] = Field(
        default_factory=list,
        description="이 약속에 속한 사람들의 사용자 ID (제안자 + 동의한 사람)",
        examples=[["u-101", "u-102"]],
    )


__all__ = [
    "MeetingDraftV2",
    "MeetingDraftV2Request",
    "MessageV2",
]
