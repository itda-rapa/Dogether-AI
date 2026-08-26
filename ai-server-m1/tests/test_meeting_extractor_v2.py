"""[추출 기능 검사 - 단체 채팅방(v2)]

단체 채팅방에서 약속을 뽑아내는 코드가 잘 동작하는지 확인합니다.
진짜 AI 를 부르지 않고 '가짜 답' 을 넣어 검사합니다.

여기서 가장 중요한 것은 M2_FLOW A-5 의 정족수 규칙입니다.
    제안한 사람 + 최소 1명 = 2명. 침묵은 동의도 거절도 아니다.
"""

import pytest

from app.schemas.meeting_v2 import MessageV2
from app.services import meeting_extractor_v2
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError
from app.services.meeting_extractor_v2 import (
    extract_meeting_drafts_v2,
    to_card_v2,
    to_cards_v2,
)
from app.services.participant_mapper import (
    ParticipantMappingError,
    build_participant_map,
)

FOUR = ["u-101", "u-102", "u-103", "u-104"]


def _msg(sender_id: str, content: str) -> MessageV2:
    return MessageV2(
        sender_id=sender_id, content=content, sent_at="2026-07-24T18:00:00+09:00"
    )


# 4명 방의 기본 대화입니다. (A-1 의 예시)
GROUP_MESSAGES = [
    _msg("u-101", "토요일 3시 중앙공원 어때요?"),
    _msg("u-102", "좋아요 갈게요"),
    _msg("u-103", "저는 그날 병원이라 어려워요"),
]


def _card(participants, **overrides) -> dict:
    # 가짜 AI 답에 넣을 카드 하나를 만드는 도우미입니다.
    card = {
        "meeting_type": "WALK",
        "date": "2026-08-01",
        "time": "15:00",
        "place": "중앙공원",
        "participants": participants,
    }
    card.update(overrides)
    return card


# --- to_card_v2 : 이름표를 ID 로 되돌리기 ---

def test_to_card_v2_maps_participants_back_to_ids():
    # 카드의 이름표가 사용자 ID 로 바뀌는지 확인합니다.
    pmap = build_participant_map(FOUR)
    card = to_card_v2(_card(["P1", "P2"]), pmap)

    assert card.meeting_type == "WALK"
    assert card.place == "중앙공원"
    assert card.participant_ids == ["u-101", "u-102"]


def test_to_card_v2_accepts_comma_string_participants():
    # AI 가 배열 대신 "P1, P2" 문자열로 답해도 읽어냅니다.
    pmap = build_participant_map(FOUR)
    card = to_card_v2(_card("P1, P2"), pmap)
    assert card.participant_ids == ["u-101", "u-102"]


def test_to_card_v2_without_participants_key():
    # participants 가 아예 없으면 빈 목록이 됩니다. (정족수에서 걸러집니다)
    pmap = build_participant_map(FOUR)
    card = to_card_v2({"meeting_type": "WALK", "place": "중앙공원"}, pmap)
    assert card.participant_ids == []


# --- to_cards_v2 : 정족수(A-5) 확인 ---

def test_quorum_keeps_card_with_two_participants():
    # 동의 2명(제안자 + 1명)이면 카드가 만들어집니다. (A-5 첫째 줄)
    pmap = build_participant_map(FOUR)
    cards = to_cards_v2([_card(["P1", "P2"])], pmap)
    assert len(cards) == 1
    assert cards[0].participant_ids == ["u-101", "u-102"]


def test_quorum_drops_card_with_only_proposer():
    # 제안자 혼자면(나머지 침묵) 카드를 만들지 않습니다. (A-5 둘째 줄)
    pmap = build_participant_map(FOUR)
    assert to_cards_v2([_card(["P1"])], pmap) == []


def test_quorum_keeps_card_with_three_participants():
    # 3명이 동의해도 그대로 카드가 됩니다. (A-5 셋째 줄)
    pmap = build_participant_map(FOUR)
    cards = to_cards_v2([_card(["P1", "P2", "P3"])], pmap)
    assert cards[0].participant_ids == ["u-101", "u-102", "u-103"]


def test_quorum_drops_card_when_labels_are_unknown():
    # 명부에 없는 이름표만 남으면 참여자가 없는 셈이라 카드가 사라집니다.
    pmap = build_participant_map(FOUR)
    assert to_cards_v2([_card(["P8", "P9"])], pmap) == []


def test_to_cards_v2_drops_date_before_reference():
    # 과거 날짜 방어가 v2 에서도 똑같이 걸리는지 확인합니다.
    # (v1 의 to_card 를 그대로 쓰므로 규칙은 한 곳에만 있습니다.)
    pmap = build_participant_map(FOUR)
    cards = to_cards_v2(
        [_card(["P1", "P2"], date="2026-07-01")],
        pmap,
        reference_date="2026-07-24",
    )
    assert len(cards) == 1
    assert cards[0].date is None                       # 과거 날짜라 지워짐
    assert cards[0].participant_ids == ["u-101", "u-102"]  # 참여자는 그대로


def test_to_cards_v2_without_reference_date_skips_past_check():
    # 기준일을 주지 않으면 과거 날짜 검사는 건너뜁니다. (기존 호출부 호환)
    pmap = build_participant_map(FOUR)
    cards = to_cards_v2([_card(["P1", "P2"], date="2020-01-01")], pmap)
    assert cards[0].date == "2020-01-01"


def test_extract_v2_passes_reference_date_to_past_check(monkeypatch):
    # 전체 흐름에서도 기준일이 카드 변환까지 이어지는지 확인합니다.
    fake_response = (
        '[{"meeting_type": "WALK", "date": "2026-07-01", "time": "15:00", '
        '"place": "중앙공원", "participants": ["P1", "P2"]}]'
    )
    monkeypatch.setattr(
        meeting_extractor_v2, "chat_completion", lambda messages: fake_response
    )

    cards = extract_meeting_drafts_v2(
        participants=FOUR,
        messages=GROUP_MESSAGES,
        reference_date="2026-07-24",
    )
    assert len(cards) == 1
    assert cards[0].date is None
    assert cards[0].time == "15:00"


def test_to_cards_v2_drops_empty_card():
    # 네 항목이 모두 null 이면 참여자가 있어도 빈 카드라 걸러냅니다.
    pmap = build_participant_map(FOUR)
    empty = _card(
        ["P1", "P2"], meeting_type=None, date=None, time=None, place=None
    )
    assert to_cards_v2([empty], pmap) == []


def test_to_cards_v2_judges_each_card_separately():
    # 카드마다 참여자를 따로 판단합니다. 정족수 미달인 카드만 빠집니다.
    pmap = build_participant_map(FOUR)
    items = [
        _card(["P1", "P2"], place="서울숲"),
        _card(["P3"], place="한강공원"),  # 혼자 -> 탈락
    ]
    cards = to_cards_v2(items, pmap)
    assert [c.place for c in cards] == ["서울숲"]


# --- extract_meeting_drafts_v2 : 전체 흐름 ---

def _fake_answer(monkeypatch, raw: str) -> dict:
    """AI 호출을 가짜 답으로 바꾸고, 실제로 보낸 프롬프트를 담아 돌려줍니다."""

    sent = {}

    def _fake(messages):
        sent["messages"] = messages
        return raw

    monkeypatch.setattr(meeting_extractor_v2, "chat_completion", _fake)
    return sent


def test_extract_v2_happy_path(monkeypatch):
    # 전체 과정이 이어져서 참여자가 담긴 카드가 나오는지 확인합니다.
    _fake_answer(
        monkeypatch,
        '[{"meeting_type": "WALK", "date": "2026-08-01", "time": "15:00",'
        ' "place": "중앙공원", "participants": ["P1", "P2"]}]',
    )

    cards = extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28")

    assert len(cards) == 1
    assert cards[0].meeting_type == "WALK"
    assert cards[0].date == "2026-08-01"
    assert cards[0].participant_ids == ["u-101", "u-102"]


def test_extract_v2_sends_labels_not_real_names(monkeypatch):
    # AI 에게 가는 글에 사용자 ID 가 들어가지 않고 이름표만 들어가는지 확인합니다.
    sent = _fake_answer(monkeypatch, "[]")

    extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28")

    user_prompt = sent["messages"][1]["content"]
    assert "P1: 토요일 3시 중앙공원 어때요?" in user_prompt
    assert "참여자: P1, P2, P3, P4" in user_prompt
    for user_id in FOUR:
        assert user_id not in user_prompt


def test_extract_v2_rejects_small_roster():
    # 명부가 3명 미만이면 AI 를 부르기 전에 거절합니다. (-> 400)
    with pytest.raises(ParticipantMappingError):
        extract_meeting_drafts_v2(["u-101", "u-102"], GROUP_MESSAGES, "2026-07-28")


def test_extract_v2_skips_model_when_no_usable_lines(monkeypatch):
    # 명부에 없는 사람들의 메시지만 오면 AI 를 부르지 않고 빈 목록을 돌려줍니다.
    def _should_not_be_called(messages):
        raise AssertionError("Gemma 를 부르면 안 됩니다.")

    monkeypatch.setattr(meeting_extractor_v2, "chat_completion", _should_not_be_called)

    outsiders = [_msg("u-900", "안녕하세요"), _msg("u-901", "반가워요")]
    assert extract_meeting_drafts_v2(FOUR, outsiders, "2026-07-28") == []


def test_extract_v2_empty_list_when_no_meeting(monkeypatch):
    # 약속이 없으면 빈 목록이 나옵니다.
    _fake_answer(monkeypatch, "[]")
    assert extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28") == []


def test_extract_v2_drops_card_below_quorum(monkeypatch):
    # AI 가 규칙을 어기고 혼자짜리 카드를 줘도 코드가 막아냅니다.
    _fake_answer(
        monkeypatch,
        '[{"meeting_type": "WALK", "date": "2026-08-01", "time": "15:00",'
        ' "place": "중앙공원", "participants": ["P1"]}]',
    )
    assert extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28") == []


def test_extract_v2_propagates_gemma_error(monkeypatch):
    # AI 호출 실패가 그대로 전달되는지 확인합니다.
    def _boom(messages):
        raise GemmaError("연결 실패")

    monkeypatch.setattr(meeting_extractor_v2, "chat_completion", _boom)

    with pytest.raises(GemmaError):
        extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28")


def test_extract_v2_propagates_timeout(monkeypatch):
    # AI 응답 시간 초과도 그대로 전달되는지 확인합니다.
    def _slow(messages):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(meeting_extractor_v2, "chat_completion", _slow)

    with pytest.raises(GemmaTimeoutError):
        extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28")


def test_extract_v2_raises_on_bad_json(monkeypatch):
    # AI 답이 JSON 이 아니면 추출 오류를 냅니다.
    _fake_answer(monkeypatch, "JSON 아님")

    with pytest.raises(ExtractionError):
        extract_meeting_drafts_v2(FOUR, GROUP_MESSAGES, "2026-07-28")
