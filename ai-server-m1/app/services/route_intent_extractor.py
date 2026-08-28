"""최근 오픈채팅 메시지를 새 경로 생성 요청으로 구조화합니다."""

import re
from typing import Optional, Sequence

from app.logging_config import get_logger
from app.prompts.route_intent_prompt import SYSTEM_PROMPT_ROUTE_INTENT, build_user_prompt_route_intent
from app.schemas.route_intent import RouteIntentMessage, RouteIntentResult, RoutePlaceQuery
from app.services.gemma_client import chat_completion
from app.services.meeting_extractor import ExtractionError, parse_json

logger = get_logger(__name__)

_ACTIVITY_CODES = {
    "walk": "WALK", "walking": "WALK", "산책": "WALK", "걷기": "WALK",
    "걷": "WALK", "걸을": "WALK",
    "run": "RUN", "running": "RUN", "러닝": "RUN", "뛰기": "RUN", "조깅": "RUN",
    "달리": "RUN", "달릴": "RUN", "뛸": "RUN",
    "cycle": "CYCLE", "cycling": "CYCLE", "bike": "CYCLE", "자전거": "CYCLE", "라이딩": "CYCLE",
    "바이크": "CYCLE",
}

_TEMPORAL_PREFIX = re.compile(
    r"^(?:(?:(?:오늘|내일|모레|글피|이틀\s*후|사흘\s*후)(?:에|부터)?"
    r"|\d{1,2}월\s*\d{1,2}일(?:에|부터)?)\s*)+"
)
_TIME_PREFIX = re.compile(
    r"^(?:(?:오전|오후|아침|저녁|밤|새벽)\s*)?"
    r"\d{1,2}(?:시|:\d{2})(?:\s*\d{1,2}분)?(?:에|부터)?\s*"
)
_START_PATTERN = re.compile(
    r"(?P<place>[^,.!?]{1,60}?)(?:에서|을\s*출발지로|를\s*출발지로)\s*"
    r"(?:출발|시작)(?:해서|하여|하고|해|하자|할까|할래)?"
)
_DISTANCE_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>km|킬로미터|킬로|m|미터)\b",
    re.IGNORECASE,
)
_DESTINATION_PATTERN = re.compile(
    r"(?P<place>[^,.!?]{1,60}?)(?:까지|을\s*목적지로|를\s*목적지로)"
)
_WAYPOINT_PATTERN = re.compile(
    r"(?P<place>[^,.!?]{1,40}?)(?:을|를)?\s*(?:경유(?:해서|하고|해)|들렀다가|거쳐서)"
)


def _format_messages(messages: Sequence[RouteIntentMessage]) -> str:
    return "\n".join(f"{message.sender_id}: {message.content}" for message in messages)


def _place(value, conversation: Optional[str] = None) -> Optional[RoutePlaceQuery]:
    if not isinstance(value, dict):
        return None
    query = str(value.get("query") or "").strip()
    # 모델이 대화에 없던 장소를 보충하지 못하게 코드에서도 차단합니다.
    if conversation is not None and query not in conversation:
        return None
    query = _clean_place(query)
    return RoutePlaceQuery(query=query) if query else None


def _activity(value) -> Optional[str]:
    if value is None:
        return None
    return _ACTIVITY_CODES.get(str(value).strip().lower())


def _distance_km(value) -> Optional[float]:
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return None
    return distance if 0.5 <= distance <= 50 else None


def _clean_place(value: str) -> str:
    value = _TEMPORAL_PREFIX.sub("", value.strip())
    value = _TIME_PREFIX.sub("", value)
    # 연결 표현이 장소 앞에 붙지 않도록 마지막 절만 사용합니다.
    value = re.split(r"(?:그리고|그럼|그러면|해서|하고)\s*", value)[-1].strip()
    # str.strip("은는이가...")는 문자 집합으로 동작해 '이매역'의 첫 '이'까지
    # 삭제한다. 완성된 조사만 문자열 끝에서 제거해야 장소 고유명이 보존된다.
    return re.sub(r"\s*(?:은|는|이|가|을|를|부터)$", "", value).strip()


def _explicit_intent(messages: Sequence[RouteIntentMessage]) -> Optional[RouteIntentResult]:
    """명시적인 단일 메시지는 외부 모델 장애와 무관하게 안전하게 처리합니다."""
    for message in reversed(messages):
        text = message.content.strip()
        activity_type = next(
            (code for keyword, code in _ACTIVITY_CODES.items() if keyword in text.lower()),
            None,
        )
        start_match = _START_PATTERN.search(text)
        if activity_type is None or start_match is None:
            continue
        start_text = _clean_place(start_match.group("place"))
        if not start_text:
            continue

        distance_match = _DISTANCE_PATTERN.search(text)
        if distance_match is not None:
            value = float(distance_match.group("value"))
            unit = distance_match.group("unit").lower()
            distance_km = value / 1000 if unit in {"m", "미터"} else value
            if 0.5 <= distance_km <= 50:
                return RouteIntentResult(
                    status="READY", route_mode="ROUND_TRIP",
                    activity_type=activity_type,
                    start=RoutePlaceQuery(query=start_text),
                    target_distance_km=distance_km,
                )

        tail = text[start_match.end():]
        destination_matches = list(_DESTINATION_PATTERN.finditer(tail))
        if not destination_matches:
            continue
        destination_text = _clean_place(destination_matches[-1].group("place"))
        if not destination_text or destination_text == start_text:
            continue
        waypoints = []
        for match in _WAYPOINT_PATTERN.finditer(tail):
            query = _clean_place(match.group("place"))
            if query and query not in {start_text, destination_text}:
                waypoints.append(RoutePlaceQuery(query=query))
        return RouteIntentResult(
            status="READY", route_mode="POINTS", activity_type=activity_type,
            start=RoutePlaceQuery(query=start_text), waypoints=waypoints,
            destination=RoutePlaceQuery(query=destination_text),
        )
    return None


def to_route_intent(
    data: dict, messages: Optional[Sequence[RouteIntentMessage]] = None
) -> RouteIntentResult:
    conversation = "\n".join(message.content for message in messages) if messages is not None else None
    start = _place(data.get("start"), conversation)
    destination = _place(data.get("destination"), conversation)
    raw_waypoints = data.get("waypoints") if isinstance(data.get("waypoints"), list) else []
    waypoints = [place for item in raw_waypoints if (place := _place(item, conversation)) is not None]
    activity_type = _activity(data.get("activity_type"))
    distance_km = _distance_km(data.get("target_distance_km"))
    raw_mode = str(data.get("route_mode") or "").strip().upper()
    if raw_mode not in {"POINTS", "ROUND_TRIP"}:
        raw_mode = "ROUND_TRIP" if distance_km is not None and destination is None else "POINTS"
    route_mode = raw_mode
    ready = start is not None and activity_type is not None and (
        (route_mode == "ROUND_TRIP" and distance_km is not None)
        or (route_mode == "POINTS" and destination is not None)
    )
    message = str(data.get("message") or "").strip() or None
    if not ready and message is None:
        missing = []
        if start is None:
            missing.append("출발지")
        if route_mode == "POINTS" and destination is None:
            missing.append("목적지")
        if route_mode == "ROUND_TRIP" and distance_km is None:
            missing.append("총거리")
        if activity_type is None:
            missing.append("운동 종류")
        message = f"{', '.join(missing)} 정보를 대화에서 확인하지 못했습니다."

    return RouteIntentResult(
        status="READY" if ready else "INSUFFICIENT_CONTEXT", route_mode=route_mode,
        activity_type=activity_type, start=start, waypoints=waypoints,
        destination=destination, target_distance_km=distance_km, message=message,
    )


def extract_route_intent(messages: Sequence[RouteIntentMessage]) -> RouteIntentResult:
    explicit = _explicit_intent(messages)
    if explicit is not None:
        logger.info("[경로의도] 명시적 요청을 로컬 파서로 처리: 방식=%s", explicit.route_mode)
        return explicit
    raw = chat_completion([
        {"role": "system", "content": SYSTEM_PROMPT_ROUTE_INTENT},
        {"role": "user", "content": build_user_prompt_route_intent(_format_messages(messages))},
    ])
    items = parse_json(raw)
    if not items:
        raise ExtractionError("AI 응답에 경로 판단 결과가 없어요.")
    result = to_route_intent(items[0], messages)
    logger.info("[경로의도] 상태=%s 방식=%s 운동=%s 경유지=%d 거리=%s", result.status, result.route_mode, result.activity_type, len(result.waypoints), result.target_distance_km)
    return result


__all__ = ["extract_route_intent", "to_route_intent"]
