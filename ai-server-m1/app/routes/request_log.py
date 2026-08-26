"""[요청 꼬리표 — 어느 방의 요청이 어디서 왔는지 로그에 남기기]

배치(스케줄러)가 방을 하나씩 돌면서 약속 카드를 뽑을 때, **어느 방에서
실패했는지 알 수 없으면 다시 돌릴 대상을 고를 수 없습니다.** 응답은 카드
배열뿐이라 room_id 가 되돌아가지 않기 때문에, 서버 로그에 남겨두는 것이
유일한 단서입니다. 수동 호출은 사용자가 그 방을 보고 있어서 문제가 없지만,
배치는 사람이 보지 않습니다.

여기서 만든 꼬리표를 창구(routes)의 로그 앞에 붙입니다.

    [v1 요청] room=room-1 source=scheduler — 메시지 30개
    [v1 실패] room=room-1 source=scheduler — AI 응답 시간 초과

'어디서 왔는지' 는 요청 본문이 아니라 **헤더**(X-Request-Source)로 받습니다.
본문에 넣으면 v1/v2 요청 규격이 바뀌어서 문서와 회귀 테스트가 함께 움직이지만,
헤더는 규격을 건드리지 않습니다.

**판단 로직은 이 값을 보지 않습니다.** 같은 대화면 누가 불렀든 같은 카드가
나와야 합니다. 출처에 따라 결과가 달라지면 "수동으로는 나오는데 배치로는
안 나온다" 가 생겨서 재현이 불가능해집니다. 이 값은 오직 로그용입니다.
"""

import re
from typing import Optional

from fastapi import Header

# 백엔드가 보낼 헤더 이름입니다.
SOURCE_HEADER = "X-Request-Source"

# 이 세 값만 로그에 찍힙니다.
SOURCE_MANUAL = "manual"        # 사용자가 화면에서 직접 누른 호출
SOURCE_SCHEDULER = "scheduler"  # 배치(스케줄러)가 자동으로 돈 호출
SOURCE_UNKNOWN = "unknown"      # 헤더가 없거나 모르는 값일 때

_KNOWN_SOURCES = (SOURCE_MANUAL, SOURCE_SCHEDULER)

# 꼬리표에 들어갈 공백류를 한 칸으로 눌러주는 규칙입니다.
#
# room_id 에 줄바꿈이 들어오면 로그 한 줄이 두 줄로 쪼개져서, 나중에 로그를
# 읽을 때 진짜 서버가 남긴 줄인지 구분할 수 없게 됩니다.
# (participant_mapper 가 대화 위조를 막는 것과 같은 이유입니다.)
#
# `\s` 는 줄바꿈뿐 아니라 유니코드 줄 구분자(U+0085, U+2028, U+2029)와
# 탭·연속 공백까지 함께 잡습니다. 꼬리표는 사람이 눈으로 읽는 값이라
# 공백까지 정리되는 편이 낫습니다.
_WHITESPACE_RE = re.compile(r"\s+")

# 로그에 남길 room_id 의 최대 길이입니다. 이보다 길면 잘라냅니다.
MAX_ROOM_ID_IN_LOG = 64


def normalize_source(raw: Optional[str]) -> str:
    """헤더 값을 정해진 세 값 중 하나로 맞춥니다.

    모르는 값이 와도 **오류를 내지 않고** "unknown" 으로 둡니다.
    로그를 남기려다 멀쩡한 요청을 막으면 안 되기 때문입니다.
    (헤더는 어디까지나 꼬리표이지, 요청이 갖춰야 할 조건이 아닙니다.)
    """

    if not raw:
        return SOURCE_UNKNOWN
    text = raw.strip().lower()
    return text if text in _KNOWN_SOURCES else SOURCE_UNKNOWN


def get_request_source(
    raw: Optional[str] = Header(
        default=None,
        alias=SOURCE_HEADER,
        description=(
            "요청 출처 (manual / scheduler). 로그에만 쓰이며 추출 결과에는 "
            "영향을 주지 않습니다. 없으면 unknown 으로 기록됩니다."
        ),
    ),
) -> str:
    """창구에서 `Depends(get_request_source)` 로 받아 쓰는 함수입니다.

    헤더 선언을 이 한곳에만 두어, 창구마다 같은 내용을 다시 적지 않게 합니다.
    (Swagger 문서에도 선택 헤더로 함께 표시됩니다.)
    """

    return normalize_source(raw)


def request_tag(room_id: str, source: str) -> str:
    """로그 앞에 붙일 꼬리표를 만듭니다.

    예) room=room-1 source=scheduler

    room_id 가 비어 있으면 `(없음)` 으로 적습니다. 지금 v1/v2 요청 규격은
    빈 room_id 를 막지 않기 때문에, 로그에서라도 비어 있다는 사실이 보여야
    배치에서 짝을 못 찾는 원인을 바로 알 수 있습니다.
    """

    room = _WHITESPACE_RE.sub(" ", str(room_id)).strip()
    if len(room) > MAX_ROOM_ID_IN_LOG:
        room = room[:MAX_ROOM_ID_IN_LOG] + "…"
    return f"room={room or '(없음)'} source={source}"


__all__ = [
    "MAX_ROOM_ID_IN_LOG",
    "SOURCE_HEADER",
    "SOURCE_MANUAL",
    "SOURCE_SCHEDULER",
    "SOURCE_UNKNOWN",
    "get_request_source",
    "normalize_source",
    "request_tag",
]
