"""[API(창구) 검사 - 지도 팝업 판단 (M2_FLOW B-2)]

팝업 판단 창구가 올바르게 답하는지 확인합니다.
- 잘 된 경우: SHOW/SUPPRESS 와 장소 종류, 팝업 대상을 돌려주는가?
- 잘못된 요청: 400 / 422 로 거절하는가?
- 문제 상황: 알맞은 오류 번호(502, 504)를 돌려주는가?
- 좌표가 섞여 와도 서버가 그것을 받아 쓰지 않는가? (B-5)
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import place_intent as place_intent_route
from app.schemas.place_intent import PlaceIntentDecision
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.participant_mapper import ParticipantMappingError
from app.services.place_intent_detector import PlaceIntentError

DECIDE_URL = "/api/v1/place-intent/decide"

FOUR = ["u-101", "u-102", "u-103", "u-104"]
TRIGGER = "u-103"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _message(sender_id: str, content: str, minute: int = 0) -> dict:
    return {
        "sender_id": sender_id,
        "content": content,
        "sent_at": f"2026-07-24T18:0{minute}:00+09:00",
    }


VALID_MESSAGES = [
    _message("u-101", "강아지가 밤새 설사를 했어요", 0),
    _message("u-102", "괜찮으려나요", 1),
    _message(TRIGGER, "근처에 지금 문 연 동물병원 어디 있어요?", 2),
]


def _body(messages: list = None, participants: list = None, trigger: str = TRIGGER) -> dict:
    return {
        "room_id": "room-1",
        "participants": FOUR if participants is None else participants,
        "trigger_sender_id": trigger,
        "messages": VALID_MESSAGES if messages is None else messages,
    }


def _fake_decision(monkeypatch, decision: str, place_type=None) -> None:
    def _fake(participants, messages, trigger_sender_id):
        return PlaceIntentDecision(
            decision=decision, place_type=place_type, target_user_id=trigger_sender_id
        )

    monkeypatch.setattr(place_intent_route, "decide_place_intent", _fake)


def test_decide_show(client, monkeypatch):
    _fake_decision(monkeypatch, "SHOW", "HOSPITAL")

    resp = client.post(DECIDE_URL, json=_body())

    assert resp.status_code == 200
    assert resp.json() == {
        "decision": "SHOW",
        "place_type": "HOSPITAL",
        "target_user_id": TRIGGER,
    }


def test_decide_suppress(client, monkeypatch):
    _fake_decision(monkeypatch, "SUPPRESS")

    resp = client.post(DECIDE_URL, json=_body())

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"
    assert resp.json()["place_type"] is None


def test_decide_accepts_single_message(client, monkeypatch):
    # 앞 대화가 없어도(방의 첫 메시지여도) 판단은 가능합니다.
    _fake_decision(monkeypatch, "SHOW", "HOSPITAL")

    resp = client.post(DECIDE_URL, json=_body([_message(TRIGGER, "병원 어디 있어요?")]))
    assert resp.status_code == 200


def test_decide_accepts_two_person_room(client, monkeypatch):
    # 1:1 방도 받습니다. (약속 추출 v2 와 달리 2명부터)
    _fake_decision(monkeypatch, "SUPPRESS")

    body = _body(
        [_message("u-102", "병원 어디 있어요?")],
        participants=["u-101", "u-102"],
        trigger="u-102",
    )
    resp = client.post(DECIDE_URL, json=body)
    assert resp.status_code == 200


def test_decide_rejects_too_many_messages(client):
    # 4줄 이상은 형식 오류입니다. (키워드 메시지 + 앞 2줄 = 최대 3줄)
    too_many = [_message("u-101", f"메시지 {i}", i) for i in range(4)]
    resp = client.post(DECIDE_URL, json=_body(too_many))
    assert resp.status_code == 422


def test_decide_rejects_empty_messages(client):
    resp = client.post(DECIDE_URL, json=_body([]))
    assert resp.status_code == 422


def test_decide_rejects_blank_trigger(client):
    resp = client.post(DECIDE_URL, json=_body(trigger="   "))
    assert resp.status_code == 422


def test_decide_rejects_v1_message_shape(client):
    # v1 모양(sender)으로 보내면 걸러냅니다. sender_id 를 요구합니다. (422)
    body = _body(
        [{"sender": "초코 보호자", "content": "병원 어디", "sent_at": "2026-07-24T18:00:00+09:00"}]
    )
    resp = client.post(DECIDE_URL, json=body)
    assert resp.status_code == 422


def test_decide_ignores_unknown_fields_like_coordinates(client, monkeypatch):
    # 좌표가 섞여 와도 스키마에 없는 값이라 무시됩니다.
    # 좌표는 프론트와 백엔드 사이에서만 오가야 합니다. (B-5)
    _fake_decision(monkeypatch, "SUPPRESS")

    body = _body()
    body["latitude"] = 37.4979
    body["longitude"] = 127.0276

    resp = client.post(DECIDE_URL, json=body)

    assert resp.status_code == 200
    assert "latitude" not in resp.json()


def test_decide_mapping_error_returns_400(client, monkeypatch):
    def _boom(participants, messages, trigger_sender_id):
        raise ParticipantMappingError("명부가 이상해요")

    monkeypatch.setattr(place_intent_route, "decide_place_intent", _boom)

    resp = client.post(DECIDE_URL, json=_body())
    assert resp.status_code == 400


def test_decide_target_error_returns_400(client, monkeypatch):
    def _boom(participants, messages, trigger_sender_id):
        raise PlaceIntentError("대상 지정이 이상해요")

    monkeypatch.setattr(place_intent_route, "decide_place_intent", _boom)

    resp = client.post(DECIDE_URL, json=_body())
    assert resp.status_code == 400


def test_decide_gemma_failure_returns_502(client, monkeypatch):
    def _boom(participants, messages, trigger_sender_id):
        raise GemmaError("연결 실패 http://mtvs2026.work/v1")

    monkeypatch.setattr(place_intent_route, "decide_place_intent", _boom)

    resp = client.post(DECIDE_URL, json=_body())
    assert resp.status_code == 502
    # 모델 주소 같은 내부 정보는 밖으로 내보내지 않습니다.
    assert "mtvs2026" not in resp.json()["detail"]


def test_decide_timeout_returns_504(client, monkeypatch):
    def _slow(participants, messages, trigger_sender_id):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(place_intent_route, "decide_place_intent", _slow)

    resp = client.post(DECIDE_URL, json=_body())
    assert resp.status_code == 504


def test_meeting_routes_still_exist(client):
    # 창구를 하나 더 붙인 뒤에도 약속 추출 창구들이 그대로 살아있는지 확인합니다.
    # (빈 본문을 보내면 422 로 거절되는 것이 정상입니다.)
    assert client.post("/api/v1/meeting-drafts/extract", json={}).status_code == 422
    assert client.post("/api/v2/meeting-drafts/extract", json={}).status_code == 422
