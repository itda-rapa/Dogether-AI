"""[API(창구) 검사]

밖에서 요청을 보냈을 때 서버가 올바르게 답하는지 확인합니다.
- 잘 된 경우: 약속 카드 초안을 돌려주는가?
- 잘못된 요청: 제대로 걸러내는가? (422)
- 문제 상황: 알맞은 오류 번호(502, 504)를 돌려주는가?

TestClient : 진짜 서버를 켜지 않고도 요청을 흉내 내서 검사하게 해주는 도구
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import meeting as meeting_route
from app.schemas.meeting import MeetingDraft
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError

# 검사할 창구(주소)
EXTRACT_URL = "/api/v1/meeting-drafts/extract"


@pytest.fixture
def client() -> TestClient:
    # 검사에 사용할 가짜 손님(client)을 만들어 줍니다.
    return TestClient(create_app())


def _message(sender: str, content: str, minute: int = 0) -> dict:
    # 테스트용 메시지 하나를 만드는 도우미입니다.
    return {
        "sender": sender,
        "content": content,
        "sent_at": f"2026-07-24T18:0{minute}:00+09:00",
    }


def _request_body(messages: list) -> dict:
    # 테스트용 요청 본문을 만드는 도우미입니다.
    return {"room_id": "room-1", "reference_date": "2026-07-24", "messages": messages}


# 대부분의 검사에서 공통으로 쓸, 개수가 알맞은(2개) 메시지 목록
VALID_MESSAGES = [
    _message("초코 보호자", "내일 저녁 7시에 중앙공원에서 산책할까요?", 0),
    _message("보리 보호자", "좋아요. 내일 봬요!", 1),
]


def test_health(client):
    # 서버가 살아있는지 확인하는 /health 가 잘 답하는지 봅니다.
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_extract_success(client, monkeypatch):
    # 정상적으로 약속 카드 초안 목록을 돌려주는지 확인합니다.
    # (진짜 AI 대신 미리 만든 카드를 돌려주도록 바꿔치기)
    def fake_extract(messages, reference_date):
        return [
            MeetingDraft(
                meeting_type="WALK", date="2026-07-25", time="19:00", place="중앙공원"
            )
        ]

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", fake_extract)

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["meeting_type"] == "WALK"
    assert body[0]["date"] == "2026-07-25"
    assert body[0]["time"] == "19:00"
    assert body[0]["place"] == "중앙공원"


def test_extract_outing_multiple_places(client, monkeypatch):
    # 나들이에 장소가 여러 곳이면 장소마다 카드가 담긴 목록을 돌려주는지 확인합니다.
    def fake_extract(messages, reference_date):
        return [
            MeetingDraft(
                meeting_type="PLAY", date="2026-07-26", time="10:00", place="한강공원"
            ),
            MeetingDraft(
                meeting_type="PLAY", date="2026-07-26", time="10:00", place="서울숲"
            ),
        ]

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", fake_extract)

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert {c["place"] for c in body} == {"한강공원", "서울숲"}
    assert all(c["meeting_type"] == "PLAY" for c in body)


def test_extract_empty_list_when_no_meeting(client, monkeypatch):
    # 약속이 없는 대화는 빈 목록([])을 돌려주는지 확인합니다.
    monkeypatch.setattr(
        meeting_route,
        "extract_meeting_drafts",
        lambda messages, reference_date: [],
    )

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))

    assert resp.status_code == 200
    assert resp.json() == []


def test_extract_rejects_too_few_messages(client):
    # 메시지가 2개 미만이면 잘못된 요청으로 걸러내는지 확인합니다. (422)
    resp = client.post(EXTRACT_URL, json=_request_body([VALID_MESSAGES[0]]))
    assert resp.status_code == 422


def test_extract_accepts_max_200_messages(client, monkeypatch):
    # 메시지가 정확히 200개(상한)면 정상 접수되는지 확인합니다.
    monkeypatch.setattr(
        meeting_route,
        "extract_meeting_drafts",
        lambda messages, reference_date: [],
    )
    exactly_200 = [_message("사람", f"메시지 {i}", i % 10) for i in range(200)]
    resp = client.post(EXTRACT_URL, json=_request_body(exactly_200))
    assert resp.status_code == 200


def test_extract_rejects_too_many_messages(client):
    # 메시지가 200개를 초과하면 잘못된 요청으로 걸러내는지 확인합니다. (422)
    too_many = [_message("사람", f"메시지 {i}", i % 10) for i in range(201)]
    resp = client.post(EXTRACT_URL, json=_request_body(too_many))
    assert resp.status_code == 422


def test_extract_rejects_bad_reference_date(client):
    # 기준 날짜 형식이 틀리면 걸러내는지 확인합니다. (422)
    body = _request_body(VALID_MESSAGES)
    body["reference_date"] = "2026/07/24"
    resp = client.post(EXTRACT_URL, json=body)
    assert resp.status_code == 422


def test_extract_gemma_failure_returns_502(client, monkeypatch):
    # AI 호출이 실패하면 502 번호로 답하는지 확인합니다.
    def _boom(messages, reference_date):
        raise GemmaError("연결 실패")

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", _boom)

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 502


def test_extract_timeout_returns_504(client, monkeypatch):
    # AI 응답 시간이 초과되면 504 번호로 답하는지 확인합니다.
    def _slow(messages, reference_date):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", _slow)

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 504


def test_extract_parse_failure_returns_502(client, monkeypatch):
    # AI 답을 카드로 바꾸지 못하면 502 번호로 답하는지 확인합니다.
    def _boom(messages, reference_date):
        raise ExtractionError("JSON 파싱 실패")

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", _boom)

    resp = client.post(EXTRACT_URL, json=_request_body(VALID_MESSAGES))
    assert resp.status_code == 502
