"""[데이터 모양 정하기 - 위험 신호 판단(M3_FLOW 5-3 ②)]

범죄 탐지 흐름에서 AI 서버가 맡는 일은 **② 맥락 판단 하나뿐**입니다.

    메시지 → ① 규칙 필터(백엔드) → ② AI 맥락 판단(여기) → ③ 점수 누적(백엔드)
                                                          → ④ 운영자 검토 큐(백엔드)

여기 오는 것은 **규칙 필터에 이미 걸린 메시지**뿐입니다. (M3_FLOW 5-6 원칙 4)
규칙에 안 걸린 대화는 이 창구에 오지 않으므로, 이 서버는 상시 감시를 하지 않습니다.

[이 파일이 담지 않는 것과 그 이유]

- **판정 근거 문장을 돌려주지 않습니다.** 5-6 원칙 2 가 "대화 원문 미저장"인데,
  근거를 문장으로 돌려주면 그 안에 대화 원문이 그대로 실려 나갑니다. 그것을
  백엔드가 감지 이력에 저장하는 순간 원칙이 깨집니다. 그래서 나가는 값은
  **유형과 위험도라는 정해진 단어뿐**입니다. 5-7 운영자 화면이 요구하는
  "유형 · 시각 · 위험도" 중 시각은 백엔드가 이미 가지고 있습니다.

- **조치(경고·정지)를 돌려주지 않습니다.** 5-6 원칙 1 자동 제재 없음.
  이 서버는 검토 대상을 좁히기만 하고, 판단은 사람이 합니다.

- **누적 점수를 계산하지 않습니다.** 5-4 의 누적은 '같은 사람이 여러 방에서
  반복하는가' 라서 방 하나만 보는 이 서버가 알 수 없습니다. 백엔드 몫입니다.

들어올 때 : 방 명부 + 규칙에 걸린 메시지 + 앞 대화 + 그 메시지를 보낸 사람
나갈 때   : FLAG / CLEAR 와 (FLAG 일 때) 위험 유형 · 위험도
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.meeting_v2 import MessageV2

# 검토 큐로 올릴지 말지. AI 가 판단하는 값입니다.
# FLAG 라고 해서 제재가 되는 것이 아니라, 사람이 볼 목록에 한 줄이 쌓일 뿐입니다.
DECISION_FLAG = "FLAG"
DECISION_CLEAR = "CLEAR"

# 위험 유형 3종입니다. M3_FLOW 5-1 표의 2·3·4 번과 하나씩 짝입니다.
# 1번(거절 후 반복 접촉)은 차단 이력 조회만으로 끝나서 AI 가 필요 없고,
# 그래서 여기 없습니다. (5-2)
RISK_CREDENTIAL_REQUEST = "CREDENTIAL_REQUEST"  # 2번 URL·앱 설치 후 인증 정보 요구
RISK_ACCOUNT_HANDOVER = "ACCOUNT_HANDOVER"      # 3번 계좌·명의 제공 유도
RISK_THREAT_REMITTANCE = "THREAT_REMITTANCE"    # 4번 유포 협박·사칭 + 송금 요구
ALLOWED_RISK_TYPES = (
    RISK_CREDENTIAL_REQUEST,
    RISK_ACCOUNT_HANDOVER,
    RISK_THREAT_REMITTANCE,
)

# 위험도 3단계입니다. 백엔드가 ③ 점수 누적에서 가중치로 씁니다.
# 여기서 임계값을 정하지 않는 이유: 임계값은 하루 몇 건이 올라오는지를 보고
# 조정하는 값인데(5-5), 그 건수는 이 서버가 볼 수 없습니다.
LEVEL_HIGH = "HIGH"      # 다르게 해석할 여지가 거의 없음
LEVEL_MEDIUM = "MEDIUM"  # 정황은 뚜렷하나 정상 거래로 읽힐 여지가 있음
LEVEL_LOW = "LOW"        # 의심스럽지만 근거가 약함
ALLOWED_RISK_LEVELS = (LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW)

# 한 번에 볼 수 있는 대화 줄 수입니다.
#
# 지도 팝업 판단(3줄)보다 넉넉한 이유: 사기는 한 줄로 완성되지 않습니다.
# "링크 하나 보낼게요" → "설치했어요" → "인증번호 좀 불러주세요" 처럼 여러 줄에
# 걸쳐 나뉘고, 마지막 줄만 보면 "인증번호 좀" 이라는 평범한 부탁이 됩니다.
#
# 그렇다고 무한정 늘리지 않는 이유: 여기 담기는 줄은 그대로 외부 모델로 나갑니다.
# 판단에 필요한 최소한만 보내는 것이 원칙이라, 앞뒤 맥락이 잡히는 선에서 끊습니다.
MAX_CONTEXT_MESSAGES = 10


class RiskSignalRequest(BaseModel):
    """위험 신호 판단 요청 모양입니다. (분석 Consumer 가 보내주는 데이터)

    messages 의 **마지막 줄이 규칙 필터에 걸린 그 메시지**여야 합니다.
    "최근 N줄" 이 아닌 이유는 지도 팝업 판단과 같습니다. 여러 명이 오가는 방에서
    마지막 N줄을 그냥 자르면 정작 걸린 메시지가 빠질 수 있습니다.
    """

    room_id: str = Field(..., description="대화방 ID", examples=["room-1"])

    # 방에 있는 사람들의 ID 목록입니다. 순서대로 P1, P2, P3 … 이름표가 붙습니다.
    # 금전 사기는 1:1 방에서 가장 많이 일어나므로 2명부터 받습니다.
    # 개수 검사는 여기서 하지 않고 participant_mapper 가 합니다. (-> 400)
    participants: List[str] = Field(
        ...,
        description="방에 있는 사람들의 사용자 ID 목록 (2명 이상)",
        examples=[["u-101", "u-102"]],
    )

    # 규칙 필터에 걸린 메시지를 보낸 사람입니다. 판단 대상은 이 사람 한 명입니다.
    # 상대가 한 말은 맥락으로만 읽습니다.
    suspect_sender_id: str = Field(
        ...,
        description="규칙 필터에 걸린 메시지를 보낸 사람의 사용자 ID (판단 대상)",
        examples=["u-102"],
    )

    # 판단에 쓸 대화입니다. 마지막 줄이 규칙에 걸린 메시지입니다.
    messages: List[MessageV2] = Field(
        ...,
        min_length=1,
        max_length=MAX_CONTEXT_MESSAGES,
        description=(
            f"규칙에 걸린 메시지 + 바로 앞 대화 (1~{MAX_CONTEXT_MESSAGES}개, 시간 순서)"
        ),
    )

    @field_validator("room_id", "suspect_sender_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("빈 칸은 넣을 수 없어요.")
        return value.strip()


class RiskSignalVerdict(BaseModel):
    """위험 신호 판단 결과입니다.

    백엔드는 decision 이 "FLAG" 일 때만 위험 점수를 누적합니다. (5-3 ③)
    CLEAR 는 아무것도 하지 않는다는 뜻이며, 기록도 남기지 않습니다.

    **여기에는 대화 내용이 들어가지 않습니다.** (5-6 원칙 2)
    운영자가 원문을 봐야 하는 경우는 5-7 의 '대화 조회' 이며, 그때는 조회 자체가
    로그로 남습니다. 이 응답을 그대로 저장하는 경로에는 원문이 섞이면 안 됩니다.
    """

    decision: str = Field(
        ...,
        description='검토 대상으로 올릴지 여부 ("FLAG" 또는 "CLEAR")',
        examples=[DECISION_FLAG],
    )
    risk_type: Optional[str] = Field(
        default=None,
        description=(
            '위험 유형 ("CREDENTIAL_REQUEST" / "ACCOUNT_HANDOVER" / '
            '"THREAT_REMITTANCE"). CLEAR 면 null'
        ),
        examples=[RISK_ACCOUNT_HANDOVER],
    )
    risk_level: Optional[str] = Field(
        default=None,
        description='위험도 ("HIGH" / "MEDIUM" / "LOW"). CLEAR 면 null',
        examples=[LEVEL_HIGH],
    )
    suspect_user_id: str = Field(
        ...,
        description="판단 대상의 사용자 ID (요청의 suspect_sender_id 와 같음)",
        examples=["u-102"],
    )


__all__ = [
    "ALLOWED_RISK_LEVELS",
    "ALLOWED_RISK_TYPES",
    "DECISION_CLEAR",
    "DECISION_FLAG",
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "MAX_CONTEXT_MESSAGES",
    "RISK_ACCOUNT_HANDOVER",
    "RISK_CREDENTIAL_REQUEST",
    "RISK_THREAT_REMITTANCE",
    "RiskSignalRequest",
    "RiskSignalVerdict",
]
