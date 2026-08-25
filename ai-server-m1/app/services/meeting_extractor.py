"""[약속 뽑아내기 - 이 프로그램의 핵심]

전체 흐름은 4단계입니다.
1) 대화 정리   : 대화를 AI 가 읽기 좋은 글로 정리
2) AI 에게 질문 : Gemma 를 불러서 답을 받음
3) JSON 뽑기   : AI 가 준 답에서 JSON 만 깔끔하게 골라냄
4) 카드로 변환 : JSON 을 우리 '약속 카드 초안' 모양으로 바꾸고 값들을 검사
"""

import json
import re
from datetime import date, datetime
from typing import List, Optional

from app.logging_config import get_logger
from app.prompts.meeting_prompt import SYSTEM_PROMPT, build_user_prompt
from app.schemas.meeting import MeetingDraft, Message
from app.services.gemma_client import GemmaError, GemmaTimeoutError, chat_completion

# 이 모듈용 로거입니다. 이름이 "app.services.meeting_extractor" 라서
# "app" 로거의 설정(레벨·출력)을 그대로 물려받습니다.
logger = get_logger(__name__)


class ExtractionError(RuntimeError):
    """AI 의 답을 카드로 바꾸지 못했을 때 쓰는 오류 표시입니다. (-> 502)"""


# AI 가 답을 ```json ... ``` 처럼 감싸서 줄 때가 있습니다.
# 그 감싸는 기호(```)를 찾아서 벗겨내기 위한 규칙입니다.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def format_conversation(messages: List[Message]) -> str:
    """대화 목록을 '이름: 말' 형태의 한 덩어리 글로 정리합니다.

    예) 초코 보호자: 내일 저녁 7시에 중앙공원에서 산책할까요?
        보리 보호자: 좋아요. 내일 봬요!
    """

    return "\n".join(f"{m.sender}: {m.content}" for m in messages)


def _strip_code_fence(text: str) -> str:
    """답을 감싸고 있는 ``` 기호를 벗겨냅니다."""

    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def _as_card_list(parsed) -> List[dict]:
    """읽어낸 JSON 을 '카드 dict 들의 목록' 으로 통일합니다.

    - 배열이면 그 안의 dict 들만 추립니다.
    - dict 하나면 [그 dict] 로 감쌉니다. (모델이 배열 대신 객체 하나만 줄 때 대비)
    - 빈 배열이면 [] (약속 없음).
    """

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    raise ExtractionError("답이 카드(객체)나 카드 배열이 아니에요.")


def parse_json(raw: str) -> List[dict]:
    """AI 가 준 답에서 JSON 을 골라내어 '카드 dict 들의 목록' 으로 바꿉니다.

    AI 가 앞뒤에 다른 말을 붙여서 주더라도, 그 안의 JSON(배열 또는 객체)을
    최대한 찾아냅니다. 끝내 JSON 을 못 찾으면 ExtractionError 를 냅니다.
    """

    candidate = _strip_code_fence(raw)

    # 1차 시도: 통째로 JSON 으로 읽어봅니다. (배열/객체 모두 허용)
    try:
        return _as_card_list(json.loads(candidate))
    except json.JSONDecodeError:
        pass  # 실패하면 아래 2차 방법으로 넘어갑니다.

    # 2차 시도: 배열 기호 '[' ~ ']' 또는 객체 기호 '{' ~ '}' 범위를 잘라서 읽어봅니다.
    #           더 바깥을 감싸는 쪽(먼저 시작하는 기호)을 우선 시도합니다.
    for open_ch, close_ch in _outer_brackets_first(candidate):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            snippet = candidate[start : end + 1]
            try:
                return _as_card_list(json.loads(snippet))
            except json.JSONDecodeError:
                continue  # 다음 후보(기호쌍)로 넘어갑니다.

    # 여기까지 왔다면 JSON 을 찾지 못한 것입니다.
    raise ExtractionError("답에서 JSON 을 찾지 못했어요.")


def _outer_brackets_first(text: str):
    """'[' 와 '{' 중 텍스트에서 더 먼저 나오는 기호쌍을 앞에 둡니다."""

    array_pair = ("[", "]")
    object_pair = ("{", "}")
    i_arr = text.find("[")
    i_obj = text.find("{")
    if i_obj == -1:
        return [array_pair]
    if i_arr == -1:
        return [object_pair]
    return [array_pair, object_pair] if i_arr < i_obj else [object_pair, array_pair]


def _clean_optional(value) -> Optional[str]:
    """빈 값이나 "null" 이라는 글자를 진짜 '없음(None)' 으로 바꿔줍니다."""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


# 모델이 코드(WALK 등) 또는 한국어(산책 등)로 답해도 코드로 통일합니다.
_TYPE_CODES = {
    "walk": "WALK", "산책": "WALK",
    "play": "PLAY", "나들이": "PLAY",
    "hospital": "HOSPITAL", "병원 동행": "HOSPITAL", "병원동행": "HOSPITAL",
    "other": "OTHER", "기타": "OTHER", "그 외": "OTHER", "그외": "OTHER",
}


def _normalize_type(value) -> Optional[str]:
    """약속 종류를 코드(WALK/PLAY/HOSPITAL/OTHER)로 통일합니다. 알 수 없으면 None."""

    text = _clean_optional(value)
    if text is None:
        return None
    # 코드는 대소문자 무시, 한국어는 소문자화해도 그대로라 함께 매칭됩니다.
    return _TYPE_CODES.get(text.lower())


def _parse_reference_date(reference_date: Optional[str]) -> Optional[date]:
    """기준일 문자열을 날짜로 바꿉니다. 없거나 형식이 틀리면 None.

    None 을 돌려주면 과거 날짜 검사를 건너뜁니다. 기준일이 이상하다고 해서
    멀쩡한 날짜까지 버리면, 원인이 기준일에 있는데 카드 쪽에서 문제를 찾게
    됩니다. (창구로 들어오는 요청은 스키마가 이미 형식을 보장합니다.)
    """

    if not reference_date:
        return None
    try:
        return datetime.strptime(str(reference_date).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_date(value, reference_date: Optional[str] = None) -> Optional[str]:
    """날짜가 진짜 'YYYY-MM-DD' 형식인지 확인합니다. 아니면 None.

    기준일(reference_date)을 함께 주면 **기준일보다 이전 날짜도 None** 으로
    둡니다. 지나간 날짜의 약속은 카드가 될 수 없기 때문입니다.

    프롬프트 규칙 4 에도 "과거 약속은 반환하지 않는다" 가 있지만, 그것은 AI 에게
    건네는 부탁이고 이것은 코드가 치는 그물입니다. 사용자가 보고 있는 수동 호출은
    이상한 날짜가 나와도 사람이 고치지만, 배치(스케줄러)는 보는 사람이 없어서
    그대로 저장됩니다. 정족수(_meets_quorum)를 프롬프트와 코드 양쪽에 둔 것과
    같은 이유입니다.

    기준일 '당일'은 남깁니다. 오늘 저녁 약속이 오늘 아침 배치에서 사라지면 안 됩니다.
    """

    text = _clean_optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()  # 형식 확인 겸 변환
    except ValueError:
        return None

    reference = _parse_reference_date(reference_date)
    if reference is not None and parsed < reference:
        # 왜 date 가 비었는지 나중에 로그로 알 수 있어야 합니다.
        logger.info(
            "[검증] 기준일(%s)보다 이전 날짜라 date 를 null 로 둡니다 — %s",
            reference,
            text,
        )
        return None
    return text


def _normalize_time(value) -> Optional[str]:
    """시각이 진짜 'HH:MM' 형식인지 확인합니다. 아니면 None."""

    text = _clean_optional(value)
    if text is None:
        return None
    try:
        datetime.strptime(text, "%H:%M")  # 형식이 맞는지 실제로 맞춰봅니다.
        return text
    except ValueError:
        return None


def to_card(data: dict, reference_date: Optional[str] = None) -> MeetingDraft:
    """읽어낸 데이터(dict)를 검사·정리해서 깔끔한 '약속 카드 초안' 으로 바꿉니다.

    (PRD AI-M1-06 응답 검증)
    - 약속 종류: 허용된 3종이 아니면 null
    - 날짜/시각: 형식이 틀리면 null
    - 날짜: 기준일을 주면 기준일보다 이전이어도 null
    - 빈 문자열: null 로 변환
    - 장소: 앞뒤 공백 제거

    reference_date 를 주지 않으면 과거 날짜 검사만 건너뜁니다. 나머지 검사는
    그대로라서, 기준일이 없는 자리(단위 테스트 등)에서도 그냥 쓸 수 있습니다.
    """

    return MeetingDraft(
        meeting_type=_normalize_type(data.get("meeting_type")),
        date=_normalize_date(data.get("date"), reference_date),
        time=_normalize_time(data.get("time")),
        place=_clean_optional(data.get("place")),
    )


def _is_empty_card(card: MeetingDraft) -> bool:
    """네 항목이 전부 null 인 '빈 카드' 인지 확인합니다."""

    return not any(
        (card.meeting_type, card.date, card.time, card.place)
    )


def to_cards(
    items: List[dict],
    reference_date: Optional[str] = None,
) -> List[MeetingDraft]:
    """카드 dict 목록을 검사·정리해서 카드 초안 목록으로 바꿉니다.

    - 각 dict 를 to_card 로 검증·정리합니다.
    - 네 항목이 모두 null 인 빈 카드는 걸러냅니다. (약속 없음 -> 빈 목록)
      나들이에서 place 만 다르고 나머지는 채워진 카드는 그대로 남습니다.

    기준일보다 이전 날짜는 to_card 에서 null 이 되므로, 날짜밖에 없던 과거
    카드는 여기서 '빈 카드' 가 되어 자연히 사라집니다.
    """

    cards = [to_card(item, reference_date) for item in items]
    return [c for c in cards if not _is_empty_card(c)]


def extract_meeting_drafts(
    messages: List[Message],
    reference_date: str,
) -> List[MeetingDraft]:
    """대화에서 약속 카드 초안(들)을 뽑아내는 '메인 함수' 입니다.

    messages: 대화 목록
    reference_date: '오늘'로 삼을 기준 날짜 (YYYY-MM-DD)

    돌려주는 값: 약속 카드 초안 목록.
        - 보통 0개(약속 없음) 또는 1개.
        - '나들이'에 장소가 여러 곳이면 장소마다 1개씩 여러 개.

    문제가 생기면:
        - GemmaTimeoutError : AI 응답 시간 초과 (-> 504)
        - GemmaError        : AI 호출 실패 (-> 502)
        - ExtractionError   : 답을 카드로 못 바꿈 (-> 502)
    """

    # 각 단계가 제대로 실행되는지 로그로 확인합니다.
    # - info : 단계 진행 상황 (평소에 보고 싶은 흐름)
    # - debug: 프롬프트/응답 원문 같은 자세한 내용 (문제를 파헤칠 때만 켬)

    # 1) 대화를 보기 좋게 정리합니다.
    logger.info("[추출 1/5] 대화 정리 시작 — 메시지 %d개", len(messages))
    conversation_text = format_conversation(messages)
    logger.info("[추출 1/5] 대화 정리 완료 — 총 %d자", len(conversation_text))

    # 2) AI 에게 물어볼 질문을 만듭니다.
    logger.info("[추출 2/5] 프롬프트 생성 시작 — 기준일 %s", reference_date)
    user_prompt = build_user_prompt(conversation_text, reference_date)
    logger.info("[추출 2/5] 프롬프트 생성 완료 — user 프롬프트 %d자", len(user_prompt))
    logger.debug("[추출 2/5] user 프롬프트 원문:\n%s", user_prompt)

    # AI 에게 보낼 말 묶음입니다.
    # - system: 규칙 설명서
    # - user: 실제 질문(대화 내용)
    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # 3) AI 에게 질문하고 답을 받음
    logger.info("[추출 3/5] Gemma 호출 시작")
    raw = chat_completion(chat_messages)
    logger.info("[추출 3/5] Gemma 응답 수신 — %d자", len(raw))
    logger.debug("[추출 3/5] Gemma 응답 원문:\n%s", raw)

    # 4) 답에서 JSON(카드 목록)만 골라냄
    logger.info("[추출 4/5] JSON 파싱 시작")
    data = parse_json(raw)
    logger.info("[추출 4/5] JSON 파싱 완료 — 후보 카드 %d개", len(data))

    # 5) JSON 을 카드 초안 목록으로 변환·검사 (빈 카드 제거, 과거 날짜 제거)
    logger.info("[추출 5/5] 카드 변환·검증 시작")
    cards = to_cards(data, reference_date)
    logger.info(
        "[추출 5/5] 카드 완성 — %d개: %s",
        len(cards),
        [c.model_dump() for c in cards],
    )
    return cards


# 이 파일에서 밖으로 꺼내 쓸 수 있는 것들의 목록입니다.
__all__ = [
    "ExtractionError",
    "GemmaError",
    "GemmaTimeoutError",
    "extract_meeting_drafts",
    "format_conversation",
    "parse_json",
    "to_card",
    "to_cards",
]
