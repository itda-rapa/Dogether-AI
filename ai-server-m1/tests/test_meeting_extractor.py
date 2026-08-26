"""[추출 기능 검사]

약속을 뽑아내는 핵심 코드가 잘 동작하는지 확인합니다.
진짜 AI 를 부르면 느리고 결과도 매번 다르므로,
여기서는 AI 대신 '가짜 답(fake_response)' 을 넣어 검사합니다.

* monkeypatch : 검사하는 동안만 진짜 함수를 가짜 함수로 살짝 바꿔치기하는 도구
"""

import pytest

from app.schemas.meeting import Message
from app.services import meeting_extractor
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import (
    ExtractionError,
    extract_meeting_drafts,
    format_conversation,
    parse_json,
    to_card,
    to_cards,
)


def _msg(sender: str, content: str) -> Message:
    # 테스트용 메시지를 간단히 만드는 도우미입니다.
    return Message(sender=sender, content=content, sent_at="2026-07-24T18:00:00+09:00")


def test_format_conversation_joins_sender_and_content():
    # 대화가 "이름: 말" 형태로 잘 정리되는지 확인합니다.
    messages = [_msg("초코 보호자", "내일 보자"), _msg("보리 보호자", "좋아요")]
    assert format_conversation(messages) == "초코 보호자: 내일 보자\n보리 보호자: 좋아요"


# --- parse_json : 이제 항상 '카드 dict 들의 목록' 을 돌려줍니다 ---

def test_parse_json_single_object_wrapped_in_list():
    # 객체 하나만 와도 목록으로 감싸주는지 확인합니다.
    assert parse_json('{"meeting_type": "산책"}') == [{"meeting_type": "산책"}]


def test_parse_json_array_of_cards():
    # 카드 배열은 그대로 목록으로 읽는지 확인합니다.
    raw = '[{"place": "한강공원"}, {"place": "서울숲"}]'
    assert parse_json(raw) == [{"place": "한강공원"}, {"place": "서울숲"}]


def test_parse_json_empty_array():
    # 빈 배열(약속 없음)은 빈 목록으로 읽는지 확인합니다.
    assert parse_json("[]") == []


def test_parse_json_strips_code_fence():
    # ``` 로 감싸진 답에서도 JSON 을 잘 꺼내는지 확인합니다.
    raw = '```json\n[{"meeting_type": null}]\n```'
    assert parse_json(raw) == [{"meeting_type": None}]


def test_parse_json_extracts_array_from_surrounding_text():
    # 앞뒤에 다른 말이 섞여 있어도 JSON 배열만 잘 골라내는지 확인합니다.
    raw = '설명입니다.\n[{"meeting_type": "나들이"}]\n끝.'
    assert parse_json(raw) == [{"meeting_type": "나들이"}]


def test_parse_json_extracts_object_from_surrounding_text():
    # 배열이 아닌 객체 하나가 섞여 있어도 골라내어 목록으로 감쌉니다.
    raw = '설명입니다.\n{"meeting_type": "나들이"}\n끝.'
    assert parse_json(raw) == [{"meeting_type": "나들이"}]


def test_parse_json_raises_on_garbage():
    # JSON 이 아예 없으면 오류를 내는지 확인합니다.
    with pytest.raises(ExtractionError):
        parse_json("여기에는 JSON 이 없습니다")


# --- to_card / to_cards : 값 검증·정리 ---

def test_to_card_keeps_valid_values():
    # 올바른 값들이 그대로 잘 담기는지 확인합니다. (종류는 코드로 통일)
    data = {
        "meeting_type": "WALK",
        "date": "2026-07-25",
        "time": "19:00",
        "place": "  중앙공원  ",  # 앞뒤 공백은 제거되어야 함
    }
    card = to_card(data)
    assert card.meeting_type == "WALK"
    assert card.date == "2026-07-25"
    assert card.time == "19:00"
    assert card.place == "중앙공원"


def test_to_card_maps_korean_type_to_code():
    # 모델이 한국어로 답해도 코드(WALK/PLAY/HOSPITAL/OTHER)로 통일되는지 확인합니다.
    assert to_card({"meeting_type": "산책"}).meeting_type == "WALK"
    assert to_card({"meeting_type": "나들이"}).meeting_type == "PLAY"
    assert to_card({"meeting_type": "병원 동행"}).meeting_type == "HOSPITAL"
    assert to_card({"meeting_type": "기타"}).meeting_type == "OTHER"
    # 코드로 와도 그대로 유지됩니다.
    assert to_card({"meeting_type": "HOSPITAL"}).meeting_type == "HOSPITAL"


def test_to_card_drops_invalid_values():
    # 잘못된 값(허용 안 된 종류, 틀린 날짜/시각 형식)은 null 로 바뀌는지 확인합니다.
    data = {
        "meeting_type": "영화관람",   # 허용된 3종이 아님 -> null
        "date": "2026/07/25",        # 형식 틀림 -> null
        "time": "7시",               # 형식 틀림 -> null
        "place": "",                 # 빈 문자열 -> null
    }
    card = to_card(data)
    assert card.meeting_type is None
    assert card.date is None
    assert card.time is None
    assert card.place is None


def test_to_cards_drops_empty_cards():
    # 네 항목이 모두 null 인 빈 카드는 목록에서 걸러지는지 확인합니다.
    items = [
        {"meeting_type": "산책", "date": None, "time": None, "place": "중앙공원"},
        {"meeting_type": None, "date": None, "time": None, "place": None},  # 빈 카드
    ]
    cards = to_cards(items)
    assert len(cards) == 1
    assert cards[0].place == "중앙공원"


def test_to_cards_keeps_multiple_outing_places():
    # 나들이 장소별 카드가 여러 개면 모두 남는지 확인합니다.
    items = [
        {"meeting_type": "나들이", "date": "2026-07-26", "time": "10:00", "place": "한강공원"},
        {"meeting_type": "나들이", "date": "2026-07-26", "time": "10:00", "place": "서울숲"},
    ]
    cards = to_cards(items)
    assert [c.place for c in cards] == ["한강공원", "서울숲"]


def test_to_cards_empty_when_no_meeting():
    # 빈 카드만 있으면 빈 목록이 되는지 확인합니다.
    items = [{"meeting_type": None, "date": None, "time": None, "place": None}]
    assert to_cards(items) == []


# --- 과거 날짜 방어 : 기준일보다 이전 날짜는 null ---
#
# 프롬프트 규칙 4 가 이미 "과거 약속은 반환하지 않는다" 이지만, 그것은 AI 에게
# 건네는 부탁입니다. 배치(스케줄러)는 결과를 보는 사람이 없어서 AI 가 한 번
# 실수하면 그대로 저장되므로, 코드에서도 한 번 더 막습니다.

def test_to_card_drops_date_before_reference():
    # 기준일보다 이전 날짜는 형식이 맞아도 null 이 되는지 확인합니다.
    data = {"meeting_type": "WALK", "date": "2026-07-20", "place": "중앙공원"}
    card = to_card(data, reference_date="2026-07-24")
    assert card.date is None
    # 날짜만 지워지고 나머지 항목은 그대로 남습니다.
    assert card.meeting_type == "WALK"
    assert card.place == "중앙공원"


def test_to_card_keeps_reference_date_itself():
    # 기준일 '당일'은 과거가 아니므로 남아야 합니다.
    # (오늘 저녁 약속이 오늘 아침 배치에서 사라지면 안 됩니다.)
    card = to_card({"date": "2026-07-24"}, reference_date="2026-07-24")
    assert card.date == "2026-07-24"


def test_to_card_keeps_future_date():
    # 기준일보다 뒤인 날짜는 그대로 남는지 확인합니다.
    card = to_card({"date": "2026-07-25"}, reference_date="2026-07-24")
    assert card.date == "2026-07-25"


def test_to_card_without_reference_date_skips_past_check():
    # 기준일을 주지 않으면 과거 날짜 검사는 건너뜁니다. (형식 검사는 그대로)
    assert to_card({"date": "2020-01-01"}).date == "2020-01-01"


def test_to_card_ignores_broken_reference_date():
    # 기준일 형식이 깨져 있으면 검사를 건너뜁니다.
    # 기준일이 원인인 문제를 카드 쪽에서 찾게 만들지 않기 위해서입니다.
    card = to_card({"date": "2020-01-01"}, reference_date="2026/07/24")
    assert card.date == "2020-01-01"


def test_to_cards_drops_card_that_had_only_past_date():
    # 날짜밖에 없던 과거 카드는 date 가 null 이 되면서 '빈 카드' 가 되어 사라집니다.
    items = [
        {"meeting_type": None, "date": "2026-07-01", "time": None, "place": None},
        {"meeting_type": "WALK", "date": "2026-07-25", "time": None, "place": None},
    ]
    cards = to_cards(items, reference_date="2026-07-24")
    assert len(cards) == 1
    assert cards[0].date == "2026-07-25"


def test_extract_passes_reference_date_to_past_check(monkeypatch):
    # 전체 흐름에서도 기준일이 카드 변환까지 이어지는지 확인합니다.
    # (여기가 끊어져 있으면 단위 테스트만 통과하고 실제로는 막히지 않습니다.)
    fake_response = (
        '[{"meeting_type": "WALK", "date": "2026-07-01", '
        '"time": "19:00", "place": "중앙공원"}]'
    )
    monkeypatch.setattr(
        meeting_extractor, "chat_completion", lambda messages: fake_response
    )

    messages = [
        _msg("초코 보호자", "지난주에 중앙공원에서 산책했었죠"),
        _msg("보리 보호자", "네, 즐거웠어요"),
    ]
    cards = extract_meeting_drafts(messages, reference_date="2026-07-24")
    assert len(cards) == 1
    assert cards[0].date is None       # 과거 날짜라 지워짐
    assert cards[0].time == "19:00"    # 나머지는 그대로


# --- extract_meeting_drafts : 전체 흐름 ---

def test_extract_meeting_drafts_happy_path(monkeypatch):
    # 전체 과정이 잘 이어져서 카드 1개짜리 목록이 나오는지 확인합니다.
    fake_response = (
        '[{"meeting_type": "산책", "date": "2026-07-25", '
        '"time": "19:00", "place": "중앙공원"}]'
    )
    # AI 호출 부분을 '가짜 답을 주는 함수' 로 바꿔치기합니다.
    monkeypatch.setattr(
        meeting_extractor, "chat_completion", lambda messages: fake_response
    )

    messages = [
        _msg("초코 보호자", "내일 저녁 7시에 중앙공원에서 산책할까요?"),
        _msg("보리 보호자", "좋아요. 내일 봬요!"),
    ]
    cards = extract_meeting_drafts(messages, reference_date="2026-07-24")

    assert len(cards) == 1
    assert cards[0].meeting_type == "WALK"
    assert cards[0].date == "2026-07-25"
    assert cards[0].time == "19:00"
    assert cards[0].place == "중앙공원"


def test_extract_meeting_drafts_outing_multiple_places(monkeypatch):
    # 나들이 장소가 여러 곳이면 장소별 카드 목록이 나오는지 확인합니다.
    fake_response = (
        '[{"meeting_type": "나들이", "date": "2026-07-26", "time": "10:00", "place": "한강공원"},'
        ' {"meeting_type": "나들이", "date": "2026-07-26", "time": "10:00", "place": "서울숲"}]'
    )
    monkeypatch.setattr(
        meeting_extractor, "chat_completion", lambda messages: fake_response
    )

    cards = extract_meeting_drafts([_msg("a", "나들이 가자"), _msg("b", "좋아")], "2026-07-24")

    assert {c.place for c in cards} == {"한강공원", "서울숲"}
    assert all(c.meeting_type == "PLAY" for c in cards)


def test_extract_meeting_drafts_empty_list_when_no_meeting(monkeypatch):
    # 약속이 없으면 빈 목록이 나오는지 확인합니다. (빈 배열 응답)
    monkeypatch.setattr(meeting_extractor, "chat_completion", lambda messages: "[]")
    assert extract_meeting_drafts([_msg("a", "안녕"), _msg("b", "응")], "2026-07-24") == []


def test_extract_meeting_drafts_propagates_gemma_error(monkeypatch):
    # AI 호출이 실패하면 그 오류가 그대로 전달되는지 확인합니다.
    def _boom(messages):
        raise GemmaError("연결 실패")

    monkeypatch.setattr(meeting_extractor, "chat_completion", _boom)

    with pytest.raises(GemmaError):
        extract_meeting_drafts([_msg("a", "안녕"), _msg("b", "응")], "2026-07-24")


def test_extract_meeting_drafts_propagates_timeout(monkeypatch):
    # AI 응답 시간 초과 오류도 그대로 전달되는지 확인합니다.
    def _slow(messages):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(meeting_extractor, "chat_completion", _slow)

    with pytest.raises(GemmaTimeoutError):
        extract_meeting_drafts([_msg("a", "안녕"), _msg("b", "응")], "2026-07-24")


def test_extract_meeting_drafts_raises_on_bad_json(monkeypatch):
    # AI 답이 JSON 이 아니면 추출 오류를 내는지 확인합니다.
    monkeypatch.setattr(
        meeting_extractor, "chat_completion", lambda messages: "JSON 아님"
    )

    with pytest.raises(ExtractionError):
        extract_meeting_drafts([_msg("a", "안녕"), _msg("b", "응")], "2026-07-24")
