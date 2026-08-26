"""[약속 뽑아내기 - 단체 채팅방(v2)]

v1(1:1)과 하는 일은 같고, 앞뒤에 '이름표' 단계가 하나씩 더 붙습니다.

    1) 이름표 붙이기 : u-101 → P1, 대화를 "P1: 내용" 으로 다시 씀
    2) 프롬프트 생성 : 참여자 목록 + 대화
    3) AI 에게 질문   : Gemma 를 불러서 답을 받음
    4) JSON 뽑기      : AI 가 준 답에서 JSON 만 골라냄
    5) 카드로 변환    : 값 검사 + 정족수(2명) 확인
    6) 이름표 떼기    : P1 → u-101

값을 검사하고 정리하는 부분(parse_json, to_card)은 v1 것을 그대로 가져다 씁니다.
같은 규칙을 두 곳에서 관리하지 않기 위해서입니다.
"""

from typing import List, Optional, Sequence

from app.logging_config import get_logger
from app.prompts.meeting_prompt_v2 import SYSTEM_PROMPT_V2, build_user_prompt_v2
from app.schemas.meeting_v2 import MeetingDraftV2, MessageV2
from app.services.gemma_client import GemmaError, GemmaTimeoutError, chat_completion
from app.services.meeting_extractor import (
    ExtractionError,
    _is_empty_card,  # 빈 카드의 정의를 v1 과 똑같이 씁니다.
    parse_json,
    to_card,
)
from app.services.participant_mapper import (
    ParticipantMap,
    ParticipantMappingError,
    build_participant_map,
    to_labeled_lines,
    to_user_ids,
)

logger = get_logger(__name__)

# 약속이 성립하려면 몇 명이 필요한가. (M2_FLOW A-5)
# "제안한 사람 + 최소 1명 = 2명" 이며, 방에 몇 명이 있든 이 값은 고정입니다.
# 과반으로 잡으면 인원마다 기준이 달라져서 4명 방과 5명 방의 정답이 갈립니다.
MIN_AGREED_PARTICIPANTS = 2

# 대화가 이보다 적으면 AI 를 부르지 않습니다.
# 한 사람의 말 한 줄로는 누구도 동의할 수 없어서 결과가 반드시 빈 목록입니다.
_MIN_USABLE_LINES = 2


def _as_labels(value) -> List[str]:
    """AI 가 준 participants 값을 이름표 목록으로 만듭니다.

    ["P1","P2"] 로 오는 것이 정상이지만, "P1, P2" 처럼 문자열 하나로 올 때도
    있어서 그 경우도 받아줍니다. 그 밖의 모양은 빈 목록으로 봅니다.
    """

    if isinstance(value, str):
        return value.replace(",", " ").split()
    if isinstance(value, list):
        return value
    return []


def to_card_v2(
    data: dict,
    participant_map: ParticipantMap,
    reference_date: Optional[str] = None,
) -> MeetingDraftV2:
    """카드 dict 하나를 검사·정리해서 v2 카드로 바꿉니다. (이름표 떼기 포함)

    종류·날짜·시각·장소 검사는 v1 의 to_card 가 그대로 합니다.
    기준일보다 이전 날짜를 null 로 두는 것도 v1 과 같습니다.
    여기서는 participants(이름표)를 사용자 ID 로 되돌리는 일만 더 합니다.
    """

    base = to_card(data, reference_date)
    participant_ids = to_user_ids(_as_labels(data.get("participants")), participant_map)
    return MeetingDraftV2(**base.model_dump(), participant_ids=participant_ids)


def _meets_quorum(card: MeetingDraftV2) -> bool:
    """이 카드가 약속으로 성립할 만큼 사람이 모였는지 확인합니다. (A-5)

    프롬프트에도 같은 규칙이 들어있지만, AI 가 규칙을 어길 수 있으므로
    코드에서 한 번 더 막습니다. 이 목록을 보고 백엔드가 알림을 보내기 때문에
    "혼자만 있는 약속" 이 나가면 상대가 모르는 약속의 알림이 생깁니다.
    """

    return len(card.participant_ids) >= MIN_AGREED_PARTICIPANTS


def to_cards_v2(
    items: List[dict],
    participant_map: ParticipantMap,
    reference_date: Optional[str] = None,
) -> List[MeetingDraftV2]:
    """카드 dict 목록을 v2 카드 목록으로 바꿉니다.

    두 가지를 걸러냅니다.
    - 네 항목이 전부 null 인 빈 카드 (v1 과 같음)
    - 참여자가 2명 미만인 카드 (정족수 미달 -> 약속이 아님)

    기준일보다 이전 날짜가 null 이 되는 것도 v1 과 같습니다.
    """

    cards = [to_card_v2(item, participant_map, reference_date) for item in items]
    kept: List[MeetingDraftV2] = []
    for card in cards:
        if _is_empty_card(card):
            continue
        if not _meets_quorum(card):
            logger.info(
                "[추출v2] 정족수 미달로 카드 제외 — 참여자 %d명 (필요 %d명)",
                len(card.participant_ids),
                MIN_AGREED_PARTICIPANTS,
            )
            continue
        kept.append(card)
    return kept


def extract_meeting_drafts_v2(
    participants: Sequence[str],
    messages: List[MessageV2],
    reference_date: str,
) -> List[MeetingDraftV2]:
    """단체 채팅방 대화에서 약속 카드 초안(들)을 뽑아내는 '메인 함수' 입니다.

    participants: 방에 있는 사람들의 사용자 ID 목록 (3명 이상)
    messages: 대화 목록
    reference_date: '오늘'로 삼을 기준 날짜 (YYYY-MM-DD)

    돌려주는 값: 카드마다 participant_ids 가 채워진 약속 카드 초안 목록.

    문제가 생기면:
        - ParticipantMappingError : 명부가 규칙에 안 맞음 (-> 400)
        - GemmaTimeoutError       : AI 응답 시간 초과 (-> 504)
        - GemmaError              : AI 호출 실패 (-> 502)
        - ExtractionError         : 답을 카드로 못 바꿈 (-> 502)
    """

    # 1) 명부로 이름표 대응표를 만듭니다. (3명 미만이면 여기서 거절됩니다)
    logger.info("[추출v2 1/6] 이름표 대응표 생성 — 명부 %d명", len(participants))
    participant_map = build_participant_map(participants)

    # 2) 대화를 "P1: 내용" 형태로 다시 씁니다.
    #    (명부에 없는 사람의 메시지는 여기서 버려집니다)
    labeled_lines = to_labeled_lines(messages, participant_map)
    dropped = len(messages) - len(labeled_lines)
    logger.info(
        "[추출v2 2/6] 대화 정리 완료 — %d줄 사용, %d줄 폐기",
        len(labeled_lines),
        dropped,
    )
    if len(labeled_lines) < _MIN_USABLE_LINES:
        # 쓸 수 있는 대화가 거의 없으면 AI 를 부르지 않고 바로 빈 목록을 돌려줍니다.
        logger.info("[추출v2] 쓸 수 있는 대화가 부족해 Gemma 호출 없이 빈 목록 반환")
        return []

    conversation_text = "\n".join(labeled_lines)

    # 3) AI 에게 물어볼 질문을 만듭니다. (침묵한 사람을 알 수 있도록 참여자 목록도 넣음)
    logger.info("[추출v2 3/6] 프롬프트 생성 시작 — 기준일 %s", reference_date)
    user_prompt = build_user_prompt_v2(
        conversation_text, reference_date, participant_map.labels
    )
    logger.info("[추출v2 3/6] 프롬프트 생성 완료 — user 프롬프트 %d자", len(user_prompt))
    logger.debug("[추출v2 3/6] user 프롬프트 원문:\n%s", user_prompt)

    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT_V2},
        {"role": "user", "content": user_prompt},
    ]

    # 4) AI 에게 질문하고 답을 받습니다.
    logger.info("[추출v2 4/6] Gemma 호출 시작")
    raw = chat_completion(chat_messages)
    logger.info("[추출v2 4/6] Gemma 응답 수신 — %d자", len(raw))
    logger.debug("[추출v2 4/6] Gemma 응답 원문:\n%s", raw)

    # 5) 답에서 JSON(카드 목록)만 골라냅니다.
    logger.info("[추출v2 5/6] JSON 파싱 시작")
    data = parse_json(raw)
    logger.info("[추출v2 5/6] JSON 파싱 완료 — 후보 카드 %d개", len(data))

    # 6) 카드로 변환하면서 이름표를 원래 ID 로 되돌립니다.
    logger.info("[추출v2 6/6] 카드 변환·검증 시작")
    cards = to_cards_v2(data, participant_map, reference_date)
    logger.info(
        "[추출v2 6/6] 카드 완성 — %d개: %s",
        len(cards),
        [c.model_dump() for c in cards],
    )
    return cards


__all__ = [
    "MIN_AGREED_PARTICIPANTS",
    "ExtractionError",
    "GemmaError",
    "GemmaTimeoutError",
    "ParticipantMappingError",
    "extract_meeting_drafts_v2",
    "to_card_v2",
    "to_cards_v2",
]
