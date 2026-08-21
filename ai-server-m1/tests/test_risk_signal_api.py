"""[API(창구) 검사 - 위험 신호 판단 (M3_FLOW 5-3 ②)]

위험 신호 판단 창구가 올바르게 답하는지 확인합니다.
- 잘 된 경우: FLAG/CLEAR 와 유형·위험도, 판단 대상을 돌려주는가?
- 잘못된 요청: 400 / 422 로 거절하는가?
- 문제 상황: 알맞은 오류 번호(502, 504)를 돌려주는가?
- 응답에 대화 원문이나 조치가 섞이지 않는가? (5-6 원칙 1·2)
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import risk_signal as risk_signal_route
from app.schemas.risk_signal import MAX_CONTEXT_MESSAGES, RiskSignalVerdict
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.participant_mapper import ParticipantMappingError
from app.services.risk_signal_detector import RiskSignalError

ANALYZE_URL = "/api/v1/risk-signals/analyze"

PAIR = ["u-101", "u-102"]
SUSPECT = "u-102"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _message(sender_id: str, content: str, minute: int = 0) -> dict:
    return {
        "sender_id": sender_id,
        "content": content,
        "sent_at": f"2026-08-21T18:{minute:02d}:00+09:00",
    }


VALID_MESSAGES = [
    _message("u-101", "사료 나눔 글 보고 연락드려요", 0),
    _message(SUSPECT, "네 안녕하세요", 1),
    _message(SUSPECT, "통장 잠깐만 빌려주시면 건당 30만원 드려요", 2),
]


def _body(messages: list = None, participants: list = None, suspect: str = SUSPECT) -> dict:
    return {
        "room_id": "room-1",
        "participants": PAIR if participants is None else participants,
        "suspect_sender_id": suspect,
        "messages": VALID_MESSAGES if messages is None else messages,
    }


def _fake_verdict(monkeypatch, decision: str, risk_type=None, risk_level=None) -> None:
    def _fake(participants, messages, suspect_sender_id):
        return RiskSignalVerdict(
            decision=decision,
            risk_type=risk_type,
            risk_level=risk_level,
            suspect_user_id=suspect_sender_id,
        )

    monkeypatch.setattr(risk_signal_route, "analyze_risk_signal", _fake)


def test_analyze_flag(client, monkeypatch):
    _fake_verdict(monkeypatch, "FLAG", "ACCOUNT_HANDOVER", "HIGH")

    resp = client.post(ANALYZE_URL, json=_body())

    assert resp.status_code == 200
    assert resp.json() == {
        "decision": "FLAG",
        "risk_type": "ACCOUNT_HANDOVER",
        "risk_level": "HIGH",
        "suspect_user_id": SUSPECT,
    }


def test_analyze_clear(client, monkeypatch):
    _fake_verdict(monkeypatch, "CLEAR")

    resp = client.post(ANALYZE_URL, json=_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "CLEAR"
    assert body["risk_type"] is None
    assert body["risk_level"] is None


def test_analyze_response_has_no_action_and_no_conversation(client, monkeypatch):
    # 응답에 조치(경고·정지)가 없어야 합니다. 자동 제재를 하지 않기 때문입니다. (원칙 1)
    # 대화 원문도 없어야 합니다. 저장되면 원칙 2 가 깨집니다.
    _fake_verdict(monkeypatch, "FLAG", "ACCOUNT_HANDOVER", "HIGH")

    body = client.post(ANALYZE_URL, json=_body()).json()

    assert set(body) == {"decision", "risk_type", "risk_level", "suspect_user_id"}
    assert "통장" not in "".join(str(value) for value in body.values())


def test_analyze_accepts_single_message(client, monkeypatch):
    # 앞 대화가 없어도(방의 첫 메시지여도) 판단은 가능합니다.
    _fake_verdict(monkeypatch, "CLEAR")

    resp = client.post(ANALYZE_URL, json=_body([_message(SUSPECT, "계좌 좀 빌려주세요")]))
    assert resp.status_code == 200


def test_analyze_accepts_group_room(client, monkeypatch):
    # 1:1 이 기본이지만 단톡방에서도 받습니다.
    _fake_verdict(monkeypatch, "CLEAR")

    body = _body(
        [_message("u-103", "링크 눌러서 인증번호 좀 불러주세요")],
        participants=["u-101", "u-102", "u-103", "u-104"],
        suspect="u-103",
    )
    resp = client.post(ANALYZE_URL, json=body)
    assert resp.status_code == 200


def test_analyze_rejects_too_many_messages(client):
    # 상한을 넘기면 형식 오류입니다. 필요 이상의 대화를 외부 모델로 보내지 않습니다.
    too_many = [
        _message("u-101", f"메시지 {i}", i) for i in range(MAX_CONTEXT_MESSAGES + 1)
    ]
    resp = client.post(ANALYZE_URL, json=_body(too_many))
    assert resp.status_code == 422


def test_analyze_rejects_empty_messages(client):
    resp = client.post(ANALYZE_URL, json=_body([]))
    assert resp.status_code == 422


def test_analyze_rejects_blank_suspect(client):
    resp = client.post(ANALYZE_URL, json=_body(suspect="   "))
    assert resp.status_code == 422


def test_analyze_rejects_v1_message_shape(client):
    # v1 모양(sender)으로 보내면 걸러냅니다. sender_id 를 요구합니다. (422)
    body = _body(
        [{"sender": "초코 보호자", "content": "계좌 좀", "sent_at": "2026-08-21T18:00:00+09:00"}]
    )
    resp = client.post(ANALYZE_URL, json=body)
    assert resp.status_code == 422


def test_analyze_ignores_unknown_fields(client, monkeypatch):
    # 백엔드가 규칙 필터 결과를 덧붙여 보내도 스키마에 없는 값이라 무시됩니다.
    # 규칙에 걸렸다는 사실 자체는 판단 근거가 아니므로 모델에게 알려주지 않습니다.
    _fake_verdict(monkeypatch, "CLEAR")

    body = _body()
    body["rule_hits"] = ["ACCOUNT_NUMBER", "REMITTANCE"]
    body["risk_score"] = 42

    resp = client.post(ANALYZE_URL, json=body)

    assert resp.status_code == 200
    assert "rule_hits" not in resp.json()


def test_analyze_mapping_error_returns_400(client, monkeypatch):
    def _boom(participants, messages, suspect_sender_id):
        raise ParticipantMappingError("명부가 이상해요")

    monkeypatch.setattr(risk_signal_route, "analyze_risk_signal", _boom)

    resp = client.post(ANALYZE_URL, json=_body())
    assert resp.status_code == 400


def test_analyze_target_error_returns_400(client, monkeypatch):
    def _boom(participants, messages, suspect_sender_id):
        raise RiskSignalError("대상 지정이 이상해요")

    monkeypatch.setattr(risk_signal_route, "analyze_risk_signal", _boom)

    resp = client.post(ANALYZE_URL, json=_body())
    assert resp.status_code == 400


def test_analyze_gemma_failure_returns_502(client, monkeypatch):
    def _boom(participants, messages, suspect_sender_id):
        raise GemmaError("연결 실패 http://mtvs2026.work/v1")

    monkeypatch.setattr(risk_signal_route, "analyze_risk_signal", _boom)

    resp = client.post(ANALYZE_URL, json=_body())
    assert resp.status_code == 502
    # 모델 주소 같은 내부 정보는 밖으로 내보내지 않습니다.
    assert "mtvs2026" not in resp.json()["detail"]


def test_analyze_timeout_returns_504(client, monkeypatch):
    def _slow(participants, messages, suspect_sender_id):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(risk_signal_route, "analyze_risk_signal", _slow)

    resp = client.post(ANALYZE_URL, json=_body())
    assert resp.status_code == 504


def test_existing_routes_still_exist(client):
    # 창구를 하나 더 붙인 뒤에도 기존 창구들이 그대로 살아있는지 확인합니다.
    # (빈 본문을 보내면 422 로 거절되는 것이 정상입니다.)
    assert client.post("/api/v1/meeting-drafts/extract", json={}).status_code == 422
    assert client.post("/api/v2/meeting-drafts/extract", json={}).status_code == 422
    assert client.post("/api/v1/place-intent/decide", json={}).status_code == 422
    assert client.get("/health").status_code == 200
