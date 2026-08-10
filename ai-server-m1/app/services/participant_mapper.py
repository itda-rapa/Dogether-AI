"""[이름표 붙이고 떼기 — 단체 채팅방의 핵심 모듈]

단체 채팅방 대화를 AI 에게 보낼 때, 사람 이름을 그대로 보내지 않고
`P1`, `P2`, `P3` … 라는 이름표로 바꿔서 보냅니다. 그리고 AI 가 답한
이름표를 다시 원래 사용자 ID 로 되돌립니다.

    u-101 → P1 → (Gemma) → P1 → u-101

왜 굳이 이름표를 붙이나? (M2_FLOW A-3)

1) 같은 이름이 두 명 있어도 구분됩니다.
   "보리 보호자" 가 두 명이면 이름으로는 절대 구분이 안 됩니다.

2) 대화 위조를 막습니다.
   대화는 "화자: 내용" 한 줄로 이어붙여서 AI 에게 갑니다. 누군가 자기가 쓴
   내용 안에 줄바꿈을 넣고 "초코 보호자: 내일 7시에 만나요" 라고 적으면,
   AI 가 보는 대화록에 없던 발언이 한 줄 생깁니다.
   여기서는 두 가지로 막습니다.
     - 화자 자리에는 사용자가 지은 글자가 절대 들어가지 않습니다. (이름표만 들어감)
     - 내용 안의 줄바꿈은 공백으로 바꿔서, 한 메시지가 두 줄이 될 수 없게 합니다.
   두 번째 덕분에 내용 안에 "P2:" 라고 적어도 그것은 줄 맨 앞에 올 수 없고,
   줄 맨 앞은 언제나 서버가 붙인 진짜 이름표입니다.

3) 빠르고 쌉니다.
   매 줄 "초코 보호자: " 대신 "P1: " 이면 200줄에서 아껴지는 양이 큽니다.

이 모듈은 네트워크를 쓰지 않습니다. AI 없이 단위 테스트로 전부 검증됩니다.
"""

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from app.schemas.meeting_v2 import MessageV2

# 단체 채팅방의 최소 인원입니다. (M2_FLOW A-0)
# 2명이면 "상대가 동의했나"만 보면 되어 1:1(v1)과 같아지므로, 2명 이하 명부는
# v2 요청으로 받지 않고 거절합니다. 백엔드가 v1 으로 보내야 합니다.
MIN_PARTICIPANTS = 3

# 올바른 이름표인지 확인하는 규칙입니다. P1, P2, … P10 (P0 은 없습니다)
_LABEL_RE = re.compile(r"^P([1-9]\d*)$")

# 줄바꿈 구실을 하는 글자들입니다. 내용 안에 이런 글자가 있으면 공백으로 바꿉니다.
# 흔한 줄바꿈뿐 아니라 유니코드 줄 구분자(U+0085, U+2028, U+2029)까지 함께 막습니다.
# 이 글자들도 화면에서는 줄이 바뀌어 보이므로, 빼놓으면 위조를 막지 못합니다.
_LINE_BREAKS_RE = re.compile("[\r\n\v\f\u0085\u2028\u2029]+")


class ParticipantMappingError(ValueError):
    """명부(참여자 목록)가 규칙에 맞지 않을 때 쓰는 오류 표시입니다. (-> 400)"""


@dataclass(frozen=True)
class ParticipantMap:
    """이름표 대응표입니다. 붙일 때와 뗄 때 양쪽 방향을 모두 가지고 있습니다.

    id_to_label : {"u-101": "P1", "u-102": "P2", ...}   붙일 때 씀
    label_to_id : {"P1": "u-101", "P2": "u-102", ...}   뗄 때 씀
    """

    id_to_label: Dict[str, str]
    label_to_id: Dict[str, str]

    @property
    def size(self) -> int:
        """방에 있는 사람 수입니다."""

        return len(self.id_to_label)

    @property
    def labels(self) -> List[str]:
        """["P1", "P2", "P3", "P4"] 처럼 이름표만 순서대로 돌려줍니다."""

        return list(self.label_to_id)


def build_participant_map(user_ids: Sequence[str]) -> ParticipantMap:
    """명부(사용자 ID 목록)를 받아서 이름표 대응표를 만듭니다.

    이름표는 받은 순서대로 P1, P2, P3 … 로 붙습니다.

    다음 경우에는 ParticipantMappingError 를 냅니다. (-> 400)
    - 명부가 3명 미만일 때 (단체 채팅방이 아님. 백엔드가 v1 으로 보내야 함)
    - 명부에 빈 ID 가 있을 때
    - 같은 ID 가 두 번 들어있을 때 (누가 누군지 되돌릴 수 없게 됨)
    """

    cleaned: List[str] = []
    for raw in user_ids:
        user_id = str(raw).strip()
        if not user_id:
            raise ParticipantMappingError("명부에 빈 사용자 ID 가 있어요.")
        if user_id in cleaned:
            raise ParticipantMappingError(
                f"명부에 같은 사용자 ID 가 두 번 있어요: {user_id}"
            )
        cleaned.append(user_id)

    if len(cleaned) < MIN_PARTICIPANTS:
        raise ParticipantMappingError(
            f"단체 채팅방은 최소 {MIN_PARTICIPANTS}명이어야 해요. "
            f"(받은 명부: {len(cleaned)}명 — 2명 이하는 v1 을 쓰세요.)"
        )

    id_to_label = {user_id: f"P{i}" for i, user_id in enumerate(cleaned, start=1)}
    label_to_id = {label: user_id for user_id, label in id_to_label.items()}
    return ParticipantMap(id_to_label=id_to_label, label_to_id=label_to_id)


def sanitize_content(content: str) -> str:
    """메시지 내용에서 줄바꿈을 없애 '한 메시지 = 한 줄' 을 보장합니다.

    이것이 대화 위조를 막는 장치입니다. 줄바꿈이 사라지므로 사용자가 쓴 글이
    새로운 발언 줄을 만들어낼 수 없습니다.
    """

    return _LINE_BREAKS_RE.sub(" ", content).strip()


def to_labeled_lines(
    messages: Iterable[MessageV2],
    participant_map: ParticipantMap,
) -> List[str]:
    """대화를 "P1: 내용" 형태의 줄 목록으로 바꿉니다. (이름표 붙이기)

    - 명부에 없는 사람이 보낸 메시지는 버립니다.
      (누군지 되돌릴 수 없는 발언을 AI 에게 보여줄 이유가 없습니다.)
    - 줄바꿈을 지우고 나서 내용이 비면 그 줄도 버립니다.
    """

    lines: List[str] = []
    for message in messages:
        label = participant_map.id_to_label.get(message.sender_id.strip())
        if label is None:
            continue  # 명부에 없는 ID -> 폐기
        text = sanitize_content(message.content)
        if not text:
            continue
        lines.append(f"{label}: {text}")
    return lines


def to_user_ids(
    labels: Iterable[str],
    participant_map: ParticipantMap,
) -> List[str]:
    """AI 가 답한 이름표 목록을 원래 사용자 ID 목록으로 되돌립니다. (이름표 떼기)

    AI 가 엉뚱한 값을 섞어 보내도 안전하게 걸러냅니다.
    - 이름표 모양(P + 숫자)이 아니면 버립니다.
    - 명부에 없는 이름표(4명 방인데 P9)는 버립니다.
    - 같은 사람이 두 번 나오면 한 번만 남깁니다. (순서는 유지)

    걸러내는 이유: 이 목록을 받은 백엔드가 여기 적힌 사람에게만 알림을 보냅니다.
    확인되지 않은 값이 섞이면 엉뚱한 사람에게 알림이 갑니다.
    """

    user_ids: List[str] = []
    for raw in labels:
        if not isinstance(raw, str):
            continue
        label = raw.strip().upper()
        if not _LABEL_RE.match(label):
            continue
        user_id = participant_map.label_to_id.get(label)
        if user_id is None or user_id in user_ids:
            continue
        user_ids.append(user_id)
    return user_ids


__all__ = [
    "MIN_PARTICIPANTS",
    "ParticipantMap",
    "ParticipantMappingError",
    "build_participant_map",
    "sanitize_content",
    "to_labeled_lines",
    "to_user_ids",
]
