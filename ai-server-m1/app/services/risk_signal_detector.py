"""[위험 신호를 판단하기 (M3_FLOW 5-3 ②)]

범죄 탐지 흐름에서 AI 서버가 하는 일의 전부입니다. 흐름은 다섯 단계입니다.

    1) 이름표 붙이기 : u-101 → P1, 대화를 "P1: 내용" 으로 다시 씀
    2) 대상 확인     : 규칙에 걸린 사람이 명부에 있고, 마지막 줄의 주인인지 확인
    3) AI 에게 질문   : Gemma 를 불러서 한 줄을 받음
    4) 한 줄 해석     : 유형|위험도 또는 CLEAR
    5) 결과 만들기    : 판단 대상을 붙여서 돌려줌

지도 팝업 판단(place_intent_detector)과 뼈대가 같습니다. 다른 것은 셋뿐입니다.
    - 고를 값이 둘(유형, 위험도)이라 답이 단어 하나가 아니라 한 줄입니다.
    - 대화를 더 길게 봅니다. 사기는 한 줄로 완성되지 않습니다.
    - **로그에 대화를 남기지 않습니다.** (아래 참고)

[로그에 프롬프트를 남기지 않는 이유]

place_intent_detector 는 debug 로그에 프롬프트 원문을 남깁니다. 여기서는 남기지
않습니다. M3_FLOW 5-6 원칙 2 가 "대화 원문 미저장, 위험 신호와 시각만 남긴다"
인데, 이 경로를 지나는 대화는 **이미 규칙 필터에 걸린 대화**입니다. 계좌번호와
인증번호가 실제로 들어있을 가능성이 가장 높은 대화가, 디버그 로그를 켜는 순간
서버 로그 파일에 평문으로 쌓입니다. 운영자가 원문을 봐야 하는 통로는 5-7 의
'대화 조회' 하나뿐이고 그것은 조회 자체가 로그로 남습니다. 디버그 로그는
그 통로를 우회하는 뒷문이 됩니다.

[막힐 때 어느 쪽으로 넘어지나]

언제나 CLEAR(검토 큐에 올리지 않음) 쪽으로 넘어집니다. 5-5 의 이유입니다.
잘못 올린 건이 쌓이면 아무도 검토하지 않게 되고, 그러면 기능 전체가 죽습니다.

- 모델이 알아들을 수 없는 답을 준 경우      -> CLEAR (502 를 내지 않음)
- 유형은 읽었는데 위험도를 못 읽은 경우     -> 유형은 살리고 위험도는 LOW
- 대화에서 쓸 수 있는 줄이 하나도 안 남은 경우 -> CLEAR (모델을 부르지 않음)
- 모델 호출 자체가 실패한 경우               -> 502/504 (라우터에서 처리)

'위험도를 못 읽으면 LOW' 만 CLEAR 로 넘어지지 않습니다. 유형이 분명한 신호를
버리는 것과, 그 신호를 가장 낮은 무게로 넘기는 것 중에서는 후자가 낫습니다.
어차피 한 건으로는 아무 일도 일어나지 않고 누적이 되어야 올라갑니다. (5-4)
"""

import re
from typing import List, Optional, Sequence, Tuple

from app.logging_config import get_logger
from app.prompts.risk_signal_prompt import (
    SYSTEM_PROMPT_RISK_SIGNAL,
    build_user_prompt_risk_signal,
)
from app.schemas.meeting_v2 import MessageV2
from app.schemas.risk_signal import (
    ALLOWED_RISK_LEVELS,
    ALLOWED_RISK_TYPES,
    DECISION_CLEAR,
    DECISION_FLAG,
    LEVEL_LOW,
    RiskSignalVerdict,
)
from app.services.gemma_client import GemmaError, GemmaTimeoutError, chat_completion
from app.services.participant_mapper import (
    ParticipantMappingError,
    build_participant_map,
    to_labeled_lines,
)

logger = get_logger(__name__)

# 이 판단에 필요한 최소 인원입니다. 금전 사기는 1:1 방에서 가장 많이 일어납니다.
# 약속 추출(v2)의 3명과 다른 값이라 build_participant_map 에 넘겨줍니다.
MIN_ROOM_PARTICIPANTS = 2

# 모델이 낼 수 있는 답입니다. 유형과 위험도는 서로 겹치는 글자가 없어서
# 한 줄 안에서 따로따로 찾아낼 수 있습니다. (HIGH 가 어느 유형 이름에도 없음)
_CLEAR_TOKEN = DECISION_CLEAR

# 답 안에서 위 단어를 찾아낼 때 쓰는 규칙입니다.
# 앞뒤에 따옴표나 마침표가 붙어 와도, "유형|위험도" 로 붙어 와도 찾아냅니다.
_TYPE_RE = re.compile("|".join(ALLOWED_RISK_TYPES))
_LEVEL_RE = re.compile("|".join(ALLOWED_RISK_LEVELS))


class RiskSignalError(ValueError):
    """요청이 규칙에 맞지 않을 때 쓰는 오류 표시입니다. (-> 400)

    명부 문제는 ParticipantMappingError 가 따로 냅니다. 이쪽은 '명부는 멀쩡한데
    판단 대상 지정이 대화와 맞지 않는' 경우입니다.
    """


def parse_verdict(raw: str) -> Tuple[str, Optional[str], Optional[str]]:
    """모델이 준 한 줄에서 판단 결과를 읽어냅니다.

    돌려주는 값: (decision, risk_type, risk_level)
        ("FLAG", "ACCOUNT_HANDOVER", "HIGH") / ("CLEAR", None, None)

    읽는 순서:
    1) 앞뒤 군더더기를 털어낸 것이 CLEAR 면 그것으로 끝냅니다.
    2) 답 전체에서 위험 유형을 찾습니다.
       - 딱 한 종류만 나왔고 CLEAR 가 섞여 있지 않으면 그 유형으로 정합니다.
       - 하나도 없거나, 두 종류가 섞였거나, CLEAR 가 함께 있으면 CLEAR 입니다.
    3) 위험도도 같은 방식으로 찾되, 못 읽으면 LOW 로 둡니다.

    2번에서 여러 종류를 CLEAR 로 넘기는 이유는 지도 팝업 판단과 같습니다.
    "ACCOUNT_HANDOVER 가 아니라 CLEAR 입니다" 같은 답에서 앞에 나온 단어를 집으면
    무고한 사람이 검토 큐에 올라갑니다. 무엇을 말하는지 알 수 없으면 올리지
    않는 편이 낫습니다.

    3번에서만 규칙이 다른 이유는 이 파일 맨 위 '막힐 때 어느 쪽으로 넘어지나' 에
    적었습니다. 유형이 분명한 신호를 위험도 한 글자 때문에 버리지 않습니다.
    """

    text = raw.strip().strip("`\"'*. \n").upper()

    if text == _CLEAR_TOKEN:
        return DECISION_CLEAR, None, None

    found_types = set(_TYPE_RE.findall(text))
    says_clear = _CLEAR_TOKEN in text

    if len(found_types) != 1 or says_clear:
        # 유형 없이 CLEAR 만 장황하게 말한 경우는 정상 답이라 경고하지 않습니다.
        if not (says_clear and not found_types):
            logger.warning(
                "[위험판단] 알 수 없는 응답이라 CLEAR 로 처리 — 찾은 유형 %s, "
                "CLEAR 포함 %s, 길이 %d자",
                sorted(found_types),
                says_clear,
                len(raw),
            )
        return DECISION_CLEAR, None, None

    risk_type = found_types.pop()

    found_levels = set(_LEVEL_RE.findall(text))
    if len(found_levels) == 1:
        risk_level = found_levels.pop()
    else:
        # 유형은 살리고 위험도만 가장 낮게 둡니다. 누적으로 걸러집니다. (5-4)
        logger.warning(
            "[위험판단] 위험도를 읽지 못해 %s 로 처리 — 찾은 위험도 %s",
            LEVEL_LOW,
            sorted(found_levels),
        )
        risk_level = LEVEL_LOW

    return DECISION_FLAG, risk_type, risk_level


def _cleared(suspect_user_id: str) -> RiskSignalVerdict:
    """검토 큐에 올리지 않는 결과를 만듭니다."""

    return RiskSignalVerdict(
        decision=DECISION_CLEAR,
        risk_type=None,
        risk_level=None,
        suspect_user_id=suspect_user_id,
    )


def analyze_risk_signal(
    participants: Sequence[str],
    messages: List[MessageV2],
    suspect_sender_id: str,
) -> RiskSignalVerdict:
    """위험 신호를 판단하는 '메인 함수' 입니다.

    participants: 방에 있는 사람들의 사용자 ID 목록 (2명 이상)
    messages: 규칙 필터에 걸린 메시지 + 앞 대화 (마지막 줄이 걸린 메시지)
    suspect_sender_id: 걸린 메시지를 보낸 사람. 판단 대상은 이 사람 한 명입니다.

    돌려주는 값에는 대화 내용이 들어가지 않습니다. (5-6 원칙 2)

    문제가 생기면:
        - ParticipantMappingError : 명부가 규칙에 안 맞음 (-> 400)
        - RiskSignalError         : 판단 대상 지정이 대화와 안 맞음 (-> 400)
        - GemmaTimeoutError       : AI 응답 시간 초과 (-> 504)
        - GemmaError              : AI 호출 실패 (-> 502)
    """

    suspect_id = suspect_sender_id.strip()

    # 1) 명부로 이름표 대응표를 만듭니다. (2명 미만이면 여기서 거절됩니다)
    logger.info("[위험판단 1/5] 이름표 대응표 생성 — 명부 %d명", len(participants))
    participant_map = build_participant_map(participants, min_size=MIN_ROOM_PARTICIPANTS)

    # 2) 규칙에 걸린 사람이 방에 있는 사람인지 확인합니다.
    suspect_label = participant_map.id_to_label.get(suspect_id)
    if suspect_label is None:
        raise RiskSignalError(
            "판단 대상이 방 명부에 없어요. "
            "(suspect_sender_id 가 participants 안에 있어야 합니다.)"
        )

    # 마지막 줄이 규칙에 걸린 메시지여야 합니다.
    # 이 순서가 깨지면 "저 이런 문자 받았어요" 라며 사기 문구를 옮겨 적은 사람이
    # 그 문구를 실제로 보낸 사람 대신 검토 큐에 올라갑니다. 400 으로 돌려보냅니다.
    if messages[-1].sender_id.strip() != suspect_id:
        raise RiskSignalError(
            "마지막 메시지가 판단 대상의 것이 아니에요. "
            "(규칙 필터에 걸린 메시지를 맨 뒤에 두고 보내주세요.)"
        )

    # 3) 대화를 "P1: 내용" 형태로 다시 씁니다.
    #    (명부에 없는 사람의 메시지와, 정리 후 빈 줄이 되는 메시지는 버려집니다)
    labeled_lines = to_labeled_lines(messages, participant_map)
    logger.info(
        "[위험판단 2/5] 대화 정리 완료 — 대상 %s, %d줄 사용 (받은 %d줄)",
        suspect_label,
        len(labeled_lines),
        len(messages),
    )

    # 정리하고 나니 판단할 메시지가 사라졌다면(내용이 공백뿐이었다면) 판단할 것이
    # 없습니다. 모델을 부르지 않고 CLEAR 로 끝냅니다.
    if not labeled_lines or not labeled_lines[-1].startswith(f"{suspect_label}: "):
        logger.info("[위험판단] 판단할 메시지가 남지 않아 모델 호출 없이 CLEAR")
        return _cleared(suspect_id)

    conversation_text = "\n".join(labeled_lines)

    # 4) AI 에게 물어봅니다.
    #    프롬프트는 로그로 남기지 않습니다. (파일 맨 위 참고)
    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT_RISK_SIGNAL},
        {
            "role": "user",
            "content": build_user_prompt_risk_signal(conversation_text, suspect_label),
        },
    ]

    logger.info("[위험판단 3/5] Gemma 호출 시작 — 대화 %d줄", len(labeled_lines))
    raw = chat_completion(chat_messages)
    logger.info("[위험판단 4/5] Gemma 응답 수신 — %d자", len(raw))

    # 5) 답을 해석해서 결과를 만듭니다.
    decision, risk_type, risk_level = parse_verdict(raw)
    # 로그에는 대화 내용이나 사용자 ID 를 남기지 않습니다. 판단 결과와 이름표면
    # 흐름을 따라가기에 충분합니다.
    logger.info(
        "[위험판단 5/5] 판단 완료 — 대상 %s, decision=%s, risk_type=%s, risk_level=%s",
        suspect_label,
        decision,
        risk_type,
        risk_level,
    )

    return RiskSignalVerdict(
        decision=decision,
        risk_type=risk_type,
        risk_level=risk_level,
        suspect_user_id=suspect_id,
    )


__all__ = [
    "MIN_ROOM_PARTICIPANTS",
    "GemmaError",
    "GemmaTimeoutError",
    "ParticipantMappingError",
    "RiskSignalError",
    "analyze_risk_signal",
    "parse_verdict",
]
