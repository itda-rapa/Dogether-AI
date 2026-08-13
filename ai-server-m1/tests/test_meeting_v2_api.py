"""[API(창구) 검사 - 단체 채팅방(v2)]

단체 채팅방 창구가 올바르게 답하는지 확인합니다.
- 잘 된 경우: 참여자가 담긴 카드 목록을 돌려주는가?
- 명부가 3명 미만: 400 으로 거절하는가? (v1 으로 보내야 하는 요청)
- 문제 상황: 알맞은 오류 번호(502, 504)를 돌려주는가?
- v1 창구는 그대로 살아있는가? (기존 회귀 테스트가 계속 유효해야 함)
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import meeting_v2 as meeting_v2_route
from app.schemas.meeting_v2 import MeetingDraftV2
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError
from app.services.participant_mapper import ParticipantMappingError

EXTRACT_V2_URL = "/api/v2/meeting-drafts/extract"
EXTRACT_V1_URL = "/api/v1/meeting-drafts/extract"

FOUR = ["u-101", "u-102", "u-103", "u-104"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _message(sender_id: str, content: str, minute: int = 0) -> dict:
    return {
        "sender_id": sender_id,
        "content": content,
        "sent_at": f"2026-07-24T18:0{minute}:00+09:00",
    }


def _request_body(messages: list, participants: list = None) -> dict:
    return {
        "room_id": "room-1",
        "reference_date": "2026-07-28",
        "participants": FOUR if participants is None else participants,
        "messages": messages,
    }


# 4명 방의 기본 대화입니다. (M2_FLOW A-1 의 예시)
VALID_MESSAGES = [
    _message("u-101", "토요일 3시 중앙공원 어때요?", 0),
    _message("u-102", "좋아요 갈게요", 1),
    _message("u-103", "저는 그날 병원이라 어려워요", 2),
]


def test_extract_v2_success(client, monkeypatch):
    # 참여자가 담긴 카드 목록을 정상적으로 돌려주는지 확인합니다.
    def fake_extract(participants, messages, reference_date):
        return [
            MeetingDraftV2(
                meeting_type="WALK",
                date="2026-08-01",
                time="15:00",
                place="중앙공원",
                participant_ids=["u-101", "u-102"],
            )
        ]

    monkeypatch.setattr(meeting_v2_route, "extract_meeting_drafts_v2", fake_extract)

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["meeting_type"] == "WALK"
    assert body[0]["place"] == "중앙공원"
    # 침묵한 u-104 와 거절한 u-103 은 빠져 있어야 합니다.
    assert body[0]["participant_ids"] == ["u-101", "u-102"]


def test_extract_v2_empty_list_when_no_meeting(client, monkeypatch):
    # 약속이 없으면 빈 목록을 돌려줍니다.
    monkeypatch.setattr(
        meeting_v2_route,
        "extract_meeting_drafts_v2",
        lambda participants, messages, reference_date: [],
    )

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 200
    assert resp.json() == []


def test_extract_v2_rejects_two_person_roster(client):
    # 명부가 2명이면 v1 이 처리할 요청이므로 400 으로 거절합니다.
    body = _request_body(VALID_MESSAGES, participants=["u-101", "u-102"])
    resp = client.post(EXTRACT_V2_URL, json=body)
    assert resp.status_code == 400


def test_extract_v2_accepts_three_person_roster(client, monkeypatch):
    # 3명은 단체 채팅방의 최소 인원이므로 정상 접수됩니다.
    monkeypatch.setattr(
        meeting_v2_route,
        "extract_meeting_drafts_v2",
        lambda participants, messages, reference_date: [],
    )
    body = _request_body(VALID_MESSAGES, participants=["u-101", "u-102", "u-103"])
    resp = client.post(EXTRACT_V2_URL, json=body)
    assert resp.status_code == 200


def test_extract_v2_rejects_duplicate_roster_ids(client):
    # 명부에 같은 ID 가 두 번 있으면 400 입니다.
    body = _request_body(VALID_MESSAGES, participants=["u-101", "u-102", "u-101"])
    resp = client.post(EXTRACT_V2_URL, json=body)
    assert resp.status_code == 400


def test_extract_v2_rejects_too_few_messages(client):
    # 메시지가 2개 미만이면 형식 오류입니다. (422)
    resp = client.post(EXTRACT_V2_URL, json=_request_body([VALID_MESSAGES[0]]))
    assert resp.status_code == 422


def test_extract_v2_rejects_too_many_messages(client):
    # 메시지가 200개를 초과하면 형식 오류입니다. (422)
    too_many = [_message("u-101", f"메시지 {i}", i % 10) for i in range(201)]
    resp = client.post(EXTRACT_V2_URL, json=_request_body(too_many))
    assert resp.status_code == 422


def test_extract_v2_rejects_bad_reference_date(client):
    # 기준 날짜 형식 검사는 v1 과 똑같이 동작합니다. (422)
    body = _request_body(VALID_MESSAGES)
    body["reference_date"] = "2026/07/28"
    resp = client.post(EXTRACT_V2_URL, json=body)
    assert resp.status_code == 422


def test_extract_v2_rejects_v1_message_shape(client):
    # v1 모양(sender)으로 보내면 걸러냅니다. v2 는 sender_id 를 요구합니다. (422)
    body = _request_body(
        [
            {"sender": "초코 보호자", "content": "안녕", "sent_at": "2026-07-24T18:00:00+09:00"},
            {"sender": "보리 보호자", "content": "안녕", "sent_at": "2026-07-24T18:01:00+09:00"},
        ]
    )
    resp = client.post(EXTRACT_V2_URL, json=body)
    assert resp.status_code == 422


def test_extract_v2_mapping_error_returns_400(client, monkeypatch):
    # 명부 오류는 400 으로 답합니다.
    def _boom(participants, messages, reference_date):
        raise ParticipantMappingError("명부가 이상해요")

    monkeypatch.setattr(meeting_v2_route, "extract_meeting_drafts_v2", _boom)

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 400


def test_extract_v2_gemma_failure_returns_502(client, monkeypatch):
    def _boom(participants, messages, reference_date):
        raise GemmaError("연결 실패")

    monkeypatch.setattr(meeting_v2_route, "extract_meeting_drafts_v2", _boom)

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 502


def test_extract_v2_timeout_returns_504(client, monkeypatch):
    def _slow(participants, messages, reference_date):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(meeting_v2_route, "extract_meeting_drafts_v2", _slow)

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 504


def test_extract_v2_parse_failure_returns_502(client, monkeypatch):
    def _boom(participants, messages, reference_date):
        raise ExtractionError("JSON 파싱 실패")

    monkeypatch.setattr(meeting_v2_route, "extract_meeting_drafts_v2", _boom)

    resp = client.post(EXTRACT_V2_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 502


def test_v1_route_still_exists(client):
    # v2 를 붙인 뒤에도 v1 창구가 그대로 살아있는지 확인합니다.
    # (요청 모양이 v2 와 달라서, v2 본문을 보내면 422 로 거절되는 것이 정상입니다.)
    resp = client.post(EXTRACT_V1_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 422
