"""[데이터 모양 정하기 - 지도 팝업 판단(M2_FLOW B-2)]

지도 기능에서 AI 서버가 맡는 일은 **1단계 하나뿐**입니다.
"이 사람이 지금 지원 대상 시설을 찾는 중인가?" 를 판단해 인라인 지도를 띄울지 정합니다.

여기 오는 데이터에 **좌표는 절대 들어오지 않습니다.** (B-5)
Gemma 는 외부 주소에 있어서, 좌표가 여기로 오면 그대로 외부로 나가는 길이 됩니다.
좌표는 프론트와 백엔드 사이에서만 오갑니다.

들어올 때 : 방 명부 + 키워드가 들어있는 메시지 1~3줄 + 키워드를 말한 사람
나갈 때   : SHOW / SUPPRESS 와 (SHOW 일 때) 병원인지 약국인지, 그리고 팝업을 볼 사람
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.meeting_v2 import MessageV2

# 팝업을 띄울지 말지. AI 가 판단하는 값입니다.
DECISION_SHOW = "SHOW"
DECISION_SUPPRESS = "SUPPRESS"

# 찾고 있는 장소의 종류. SUPPRESS 일 때는 null 입니다.
PLACE_TYPE_HOSPITAL = "HOSPITAL"
PLACE_TYPE_PHARMACY = "PHARMACY"
PLACE_TYPE_ART_CENTER = "ART_CENTER"
PLACE_TYPE_ART_GALLERY = "ART_GALLERY"
PLACE_TYPE_BEAUTY = "BEAUTY"
PLACE_TYPE_MUSEUM = "MUSEUM"
PLACE_TYPE_SHOP = "SHOP"
PLACE_TYPE_RESTAURANT = "RESTAURANT"
PLACE_TYPE_TOUR_SPOT = "TOUR_SPOT"
PLACE_TYPE_OUTSOURCE = "OUTSOURCE"
PLACE_TYPE_CAFE = "CAFE"
PLACE_TYPE_RENTAL_HOUSE = "RENTAL_HOUSE"
PLACE_TYPE_HOTEL = "HOTEL"
ALLOWED_PLACE_TYPES = (
    PLACE_TYPE_HOSPITAL, PLACE_TYPE_PHARMACY, PLACE_TYPE_ART_CENTER,
    PLACE_TYPE_ART_GALLERY, PLACE_TYPE_BEAUTY, PLACE_TYPE_MUSEUM,
    PLACE_TYPE_SHOP, PLACE_TYPE_RESTAURANT, PLACE_TYPE_TOUR_SPOT,
    PLACE_TYPE_OUTSOURCE, PLACE_TYPE_CAFE, PLACE_TYPE_RENTAL_HOUSE,
    PLACE_TYPE_HOTEL,
)

# 한 번에 볼 수 있는 대화 줄 수입니다. (B-2: 키워드 메시지 + 바로 앞 2줄 = 3줄)
# 상한을 3으로 못 박는 이유: 이 판단에 필요한 것은 직전 맥락뿐이고, 더 보내봐야
# 비용만 늘고 옛날 이야기("어제 병원 다녀왔어요")가 섞여 오탐이 늘어납니다.
MAX_CONTEXT_MESSAGES = 3


class PlaceIntentRequest(BaseModel):
    """지도 팝업 판단 요청 모양입니다. (Spring Boot 가 보내주는 데이터)

    messages 의 **마지막 줄이 키워드가 들어있는 그 메시지**여야 합니다.
    "최근 3줄"이 아니라 "키워드가 있는 그 줄 + 앞 2줄"인 이유는, 여러 명이 동시에
    떠드는 방에서는 마지막 3줄에 정작 키워드 메시지가 안 들어갈 수 있어서입니다.
    """

    room_id: str = Field(..., description="대화방 ID", examples=["room-1"])

    # 방에 있는 사람들의 ID 목록입니다. 순서대로 P1, P2, P3 … 이름표가 붙습니다.
    # 혼자 있는 오픈채팅방에서도 팝업은 떠야 하므로 v2 약속 추출과 달리 1명부터 받습니다.
    # 개수 검사는 여기서 하지 않고 participant_mapper 가 합니다. (-> 400)
    # 스키마에서 걸면 422 가 되어 "인원이 안 맞음" 과 "형식이 틀림" 이 섞입니다.
    participants: List[str] = Field(
        ...,
        description="방에 있는 사람들의 사용자 ID 목록 (1명 이상)",
        examples=[["u-101", "u-102", "u-103", "u-104"]],
    )

    # 키워드를 말한 사람입니다. 팝업은 **이 사람에게만** 뜹니다.
    # 방 전체에 띄우면 나머지 사람들은 자기가 꺼내지도 않은 팝업을 보게 됩니다.
    trigger_sender_id: str = Field(
        ...,
        description="키워드를 말한 사람의 사용자 ID (팝업을 볼 사람)",
        examples=["u-103"],
    )

    # 판단에 쓸 대화입니다. 마지막 줄이 키워드 메시지입니다.
    messages: List[MessageV2] = Field(
        ...,
        min_length=1,
        max_length=MAX_CONTEXT_MESSAGES,
        description=f"키워드 메시지 + 바로 앞 대화 (1~{MAX_CONTEXT_MESSAGES}개, 시간 순서)",
    )

    @field_validator("room_id", "trigger_sender_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()


class PlaceIntentDecision(BaseModel):
    """지도 팝업 판단 결과입니다.

    프론트는 decision 이 "SHOW" 일 때만 팝업을 띄웁니다.
    그때 처음으로 위치 권한을 묻습니다. (그전까지 위치는 아무도 보지 않습니다.)
    """

    decision: str = Field(
        ...,
        description='팝업을 띄울지 여부 ("SHOW" 또는 "SUPPRESS")',
        examples=[DECISION_SHOW],
    )
    place_type: Optional[str] = Field(
        default=None,
        description="찾는 cultural_facility 카테고리. SUPPRESS 면 null",
        examples=[PLACE_TYPE_HOSPITAL],
    )
    target_user_id: str = Field(
        ...,
        description="팝업을 띄울 사람의 사용자 ID (요청의 trigger_sender_id 와 같음)",
        examples=["u-103"],
    )


__all__ = [
    "ALLOWED_PLACE_TYPES",
    "DECISION_SHOW",
    "DECISION_SUPPRESS",
    "MAX_CONTEXT_MESSAGES",
    "PLACE_TYPE_HOSPITAL",
    "PLACE_TYPE_PHARMACY",
    "PlaceIntentDecision",
    "PlaceIntentRequest",
]
