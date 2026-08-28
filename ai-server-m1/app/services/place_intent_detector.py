"""[지도 팝업을 띄울지 판단하기 (M2_FLOW B-2)]

지도 기능에서 AI 서버가 하는 일의 전부입니다. 흐름은 다섯 단계입니다.

    1) 이름표 붙이기 : u-101 → P1, 대화를 "P1: 내용" 으로 다시 씀
    2) 대상 확인     : 키워드를 말한 사람이 명부에 있고, 마지막 줄의 주인인지 확인
    3) AI 에게 질문   : Gemma 를 불러서 단어 하나를 받음
    4) 단어 해석      : SHOW_HOSPITAL / SHOW_PHARMACY / SUPPRESS
    5) 결과 만들기    : 팝업을 볼 사람(=키워드를 말한 사람)을 붙여서 돌려줌

여기에 **좌표는 들어오지도, 나가지도 않습니다.** (B-5)
Gemma 는 외부 주소에 있으므로, 좌표를 이 경로에 태우면 그대로 외부로 나갑니다.

[막힐 때 어느 쪽으로 넘어지나]

이 기능은 '없어도 대화는 굴러가는' 부가 기능입니다. 그래서 판단이 애매하면
언제나 SUPPRESS(팝업 안 띄움) 쪽으로 넘어집니다.

- 모델이 알아들을 수 없는 답을 준 경우      -> SUPPRESS (502 를 내지 않음)
- 대화에서 쓸 수 있는 줄이 하나도 안 남은 경우 -> SUPPRESS (모델을 부르지 않음)
- 모델 호출 자체가 실패한 경우               -> 502/504 (라우터에서 처리)

앞의 둘과 마지막이 다른 이유: 앞의 둘은 '판단은 끝났고 결과가 SUPPRESS' 지만,
모델 호출 실패는 판단을 아예 못 한 것이라 서버 상태를 숨기면 장애가 안 보입니다.
받는 쪽은 오류가 오면 팝업을 띄우지 않으면 됩니다.
"""

import re
from typing import List, Optional, Sequence, Tuple

from app.logging_config import get_logger
from app.prompts.place_intent_prompt import (
    SYSTEM_PROMPT_PLACE_INTENT,
    build_user_prompt_place_intent,
)
from app.schemas.meeting_v2 import MessageV2
from app.schemas.place_intent import (
    ALLOWED_PLACE_TYPES,
    DECISION_SHOW,
    DECISION_SUPPRESS,
    PlaceIntentDecision,
)
from app.services.gemma_client import GemmaError, GemmaTimeoutError, chat_completion
from app.services.participant_mapper import (
    ParticipantMappingError,
    build_participant_map,
    to_labeled_lines,
)

logger = get_logger(__name__)

# 이 판단에 필요한 최소 인원입니다. 혼자 있는 오픈채팅방에서도 팝업은 떠야 합니다.
# 약속 추출(v2)의 3명과 다른 값이며, 그래서 build_participant_map 에 넘겨줍니다.
MIN_ROOM_PARTICIPANTS = 1

# 모델이 낼 수 있는 답과 그 뜻입니다.
_SHOW_TOKENS = {
    f"SHOW_{place_type}": place_type for place_type in ALLOWED_PLACE_TYPES
}
_SUPPRESS_TOKEN = "SUPPRESS"
_KNOWN_TOKENS = (*_SHOW_TOKENS, _SUPPRESS_TOKEN)

# 답 안에서 위 단어를 찾아낼 때 쓰는 규칙입니다.
# 앞뒤에 따옴표나 마침표가 붙어 와도 찾아내되, 단어 경계는 지킵니다.
_TOKEN_RE = re.compile("|".join(_KNOWN_TOKENS))


class PlaceIntentError(ValueError):
    """요청이 규칙에 맞지 않을 때 쓰는 오류 표시입니다. (-> 400)

    명부 문제는 ParticipantMappingError 가 따로 냅니다. 이쪽은 '명부는 멀쩡한데
    키워드를 말한 사람 지정이 대화와 맞지 않는' 경우입니다.
    """


def parse_decision(raw: str) -> Tuple[str, Optional[str]]:
    """모델이 준 답에서 판단 결과를 읽어냅니다.

    돌려주는 값: (decision, place_type)
        ("SHOW", "HOSPITAL") / ("SHOW", "PHARMACY") / ("SUPPRESS", None)

    읽는 순서:
    1) 앞뒤 군더더기를 털어낸 것이 아는 단어와 정확히 같으면 그것으로 정합니다.
    2) 아니면 답 전체에서 아는 단어를 모두 찾습니다.
       딱 한 종류만 나왔으면 그것으로 정합니다.
    3) 여러 종류가 섞여 있거나 하나도 없으면 SUPPRESS 입니다.

    3번이 중요합니다. "SHOW_HOSPITAL 이 아니라 SUPPRESS 입니다" 같은 답에서
    앞에 나온 단어를 집으면 팝업이 잘못 뜹니다. 무엇을 말하는지 알 수 없으면
    띄우지 않는 편이 낫습니다.
    """

    text = raw.strip().strip("`\"'*. \n").upper()

    if text in _SHOW_TOKENS:
        return DECISION_SHOW, _SHOW_TOKENS[text]
    if text == _SUPPRESS_TOKEN:
        return DECISION_SUPPRESS, None

    found = set(_TOKEN_RE.findall(text))
    if len(found) == 1:
        token = found.pop()
        if token in _SHOW_TOKENS:
            return DECISION_SHOW, _SHOW_TOKENS[token]
        return DECISION_SUPPRESS, None

    logger.warning(
        "[지도판단] 알 수 없는 응답이라 SUPPRESS 로 처리 — 찾은 단어 %s, 길이 %d자",
        sorted(found),
        len(raw),
    )
    return DECISION_SUPPRESS, None


def _suppressed(target_user_id: str) -> PlaceIntentDecision:
    """팝업을 띄우지 않는 결과를 만듭니다."""

    return PlaceIntentDecision(
        decision=DECISION_SUPPRESS,
        place_type=None,
        target_user_id=target_user_id,
    )


def decide_place_intent(
    participants: Sequence[str],
    messages: List[MessageV2],
    trigger_sender_id: str,
) -> PlaceIntentDecision:
    """팝업을 띄울지 판단하는 '메인 함수' 입니다.

    participants: 방에 있는 사람들의 사용자 ID 목록 (1명 이상)
    messages: 키워드가 들어있는 메시지 + 바로 앞 대화 (마지막 줄이 키워드 메시지)
    trigger_sender_id: 키워드를 말한 사람. 팝업은 이 사람에게만 뜹니다.

    문제가 생기면:
        - ParticipantMappingError : 명부가 규칙에 안 맞음 (-> 400)
        - PlaceIntentError        : 키워드를 말한 사람 지정이 대화와 안 맞음 (-> 400)
        - GemmaTimeoutError       : AI 응답 시간 초과 (-> 504)
        - GemmaError              : AI 호출 실패 (-> 502)
    """

    target_id = trigger_sender_id.strip()

    # 1) 명부로 이름표 대응표를 만듭니다. (비어 있으면 여기서 거절됩니다)
    logger.info("[지도판단 1/5] 이름표 대응표 생성 — 명부 %d명", len(participants))
    participant_map = build_participant_map(participants, min_size=MIN_ROOM_PARTICIPANTS)

    # 2) 키워드를 말한 사람이 방에 있는 사람인지 확인합니다.
    target_label = participant_map.id_to_label.get(target_id)
    if target_label is None:
        raise PlaceIntentError(
            "키워드를 말한 사람이 방 명부에 없어요. "
            "(trigger_sender_id 가 participants 안에 있어야 합니다.)"
        )

    # 마지막 줄이 키워드 메시지여야 합니다. 이 순서가 깨지면 엉뚱한 사람의 말을
    # 기준으로 판단하게 되므로, 조용히 넘기지 않고 400 으로 돌려보냅니다.
    if messages[-1].sender_id.strip() != target_id:
        raise PlaceIntentError(
            "마지막 메시지가 키워드를 말한 사람의 것이 아니에요. "
            "(키워드가 들어있는 메시지를 맨 뒤에 두고 보내주세요.)"
        )

    # 3) 대화를 "P1: 내용" 형태로 다시 씁니다.
    #    (명부에 없는 사람의 메시지와, 정리 후 빈 줄이 되는 메시지는 버려집니다)
    labeled_lines = to_labeled_lines(messages, participant_map)
    logger.info(
        "[지도판단 2/5] 대화 정리 완료 — 대상 %s, %d줄 사용 (받은 %d줄)",
        target_label,
        len(labeled_lines),
        len(messages),
    )

    # 정리하고 나니 키워드 메시지가 사라졌다면(내용이 공백뿐이었다면) 판단할 것이
    # 없습니다. 모델을 부르지 않고 SUPPRESS 로 끝냅니다.
    if not labeled_lines or not labeled_lines[-1].startswith(f"{target_label}: "):
        logger.info("[지도판단] 판단할 키워드 메시지가 남지 않아 모델 호출 없이 SUPPRESS")
        return _suppressed(target_id)

    conversation_text = "\n".join(labeled_lines)

    # 4) AI 에게 물어봅니다.
    user_prompt = build_user_prompt_place_intent(conversation_text, target_label)
    logger.debug("[지도판단 3/5] user 프롬프트 원문:\n%s", user_prompt)

    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT_PLACE_INTENT},
        {"role": "user", "content": user_prompt},
    ]

    logger.info("[지도판단 3/5] Gemma 호출 시작")
    raw = chat_completion(chat_messages)
    logger.info("[지도판단 4/5] Gemma 응답 수신 — %d자", len(raw))
    logger.debug("[지도판단 4/5] Gemma 응답 원문:\n%s", raw)

    # 5) 답을 해석해서 결과를 만듭니다.
    decision, place_type = parse_decision(raw)
    # 로그에는 대화 내용이나 사용자 ID 를 남기지 않습니다. 판단 결과와 이름표면
    # 흐름을 따라가기에 충분하고, 이 로그는 대화가 오갈 때마다 쌓입니다.
    logger.info(
        "[지도판단 5/5] 판단 완료 — 대상 %s, decision=%s, place_type=%s",
        target_label,
        decision,
        place_type,
    )

    return PlaceIntentDecision(
        decision=decision,
        place_type=place_type,
        target_user_id=target_id,
    )


__all__ = [
    "MIN_ROOM_PARTICIPANTS",
    "GemmaError",
    "GemmaTimeoutError",
    "ParticipantMappingError",
    "PlaceIntentError",
    "decide_place_intent",
    "parse_decision",
]
