"""[요청 꼬리표 검사]

배치(스케줄러)가 방을 하나씩 돌 때 어느 방에서 실패했는지 로그로 찾을 수
있어야 합니다. 응답에는 room_id 가 담기지 않으므로 로그가 유일한 단서입니다.

여기서 확인하는 것은 두 가지입니다.
- 헤더 값을 manual / scheduler / unknown 세 값으로 잘 맞추는가
- 실제 창구가 방 ID와 출처를 로그에 남기는가 (성공했을 때도, 실패했을 때도)

app 로거는 uvicorn 과 겹쳐 찍히지 않도록 propagate=False 로 두었습니다.
그래서 pytest 의 caplog 가 기본 상태로는 이 로그를 잡지 못합니다.
아래 app_logs 픽스처가 caplog 의 수집기를 app 로거에 직접 달아줍니다.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import meeting as meeting_route
from app.routes import meeting_v2 as meeting_v2_route
from app.routes.request_log import (
    MAX_ROOM_ID_IN_LOG,
    SOURCE_HEADER,
    SOURCE_MANUAL,
    SOURCE_SCHEDULER,
    SOURCE_UNKNOWN,
    normalize_source,
    request_tag,
)
from app.schemas.meeting import MeetingDraft
from app.services.gemma_client import GemmaTimeoutError

V1_URL = "/api/v1/meeting-drafts/extract"
V2_URL = "/api/v2/meeting-drafts/extract"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def app_logs(caplog):
    """app 로거가 남기는 로그를 테스트에서 읽을 수 있게 해줍니다."""

    logger = logging.getLogger("app")
    logger.addHandler(caplog.handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    caplog.set_level(logging.INFO)
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.setLevel(previous_level)


def _v1_body(room_id: str = "room-1") -> dict:
    return {
        "room_id": room_id,
        "reference_date": "2026-07-24",
        "messages": [
            {
                "sender": "초코 보호자",
                "content": "내일 저녁 7시에 중앙공원에서 산책할까요?",
                "sent_at": "2026-07-24T18:00:00+09:00",
            },
            {
                "sender": "보리 보호자",
                "content": "좋아요. 내일 봬요!",
                "sent_at": "2026-07-24T18:01:00+09:00",
            },
        ],
    }


def _v2_body(room_id: str = "room-9") -> dict:
    body = _v1_body(room_id)
    body["participants"] = ["u-101", "u-102", "u-103", "u-104"]
    body["messages"] = [
        {
            "sender_id": "u-101",
            "content": "토요일 3시 중앙공원 어때요?",
            "sent_at": "2026-07-24T18:00:00+09:00",
        },
        {
            "sender_id": "u-102",
            "content": "좋아요 갈게요",
            "sent_at": "2026-07-24T18:01:00+09:00",
        },
    ]
    return body


# --- normalize_source : 헤더 값 맞추기 ---

def test_normalize_source_known_values():
    # 대소문자와 앞뒤 공백이 섞여 와도 정해진 값으로 맞춰지는지 확인합니다.
    assert normalize_source("manual") == SOURCE_MANUAL
    assert normalize_source("SCHEDULER") == SOURCE_SCHEDULER
    assert normalize_source("  scheduler  ") == SOURCE_SCHEDULER


def test_normalize_source_missing_or_unknown():
    # 헤더가 없거나 모르는 값이면 unknown 입니다. (오류를 내지 않습니다)
    assert normalize_source(None) == SOURCE_UNKNOWN
    assert normalize_source("") == SOURCE_UNKNOWN
    assert normalize_source("batch") == SOURCE_UNKNOWN


# --- request_tag : 로그에 붙일 꼬리표 ---

def test_request_tag_basic():
    assert request_tag("room-1", SOURCE_SCHEDULER) == "room=room-1 source=scheduler"


def test_request_tag_marks_empty_room_id():
    # 빈 room_id 는 요청 규격이 막지 않으므로, 로그에서라도 보여야 합니다.
    assert request_tag("", SOURCE_MANUAL) == "room=(없음) source=manual"
    assert request_tag("   ", SOURCE_MANUAL) == "room=(없음) source=manual"


def test_request_tag_flattens_line_breaks():
    # room_id 에 줄바꿈이 들어와도 로그 한 줄이 쪼개지지 않아야 합니다.
    tag = request_tag("room-1\n[v1 완료] room=가짜", SOURCE_MANUAL)
    assert "\n" not in tag
    assert tag.startswith("room=room-1 ")


def test_request_tag_truncates_long_room_id():
    # 지나치게 긴 값은 잘라서 로그를 어지럽히지 않게 합니다.
    tag = request_tag("r" * 200, SOURCE_MANUAL)
    assert "…" in tag
    assert len(tag) < 200


# --- 창구가 실제로 로그를 남기는가 ---

def test_v1_logs_room_and_source_on_success(client, app_logs, monkeypatch):
    # 성공한 요청도 방 ID·출처와 함께 남는지 확인합니다.
    monkeypatch.setattr(
        meeting_route,
        "extract_meeting_drafts",
        lambda messages, reference_date: [MeetingDraft(meeting_type="WALK")],
    )

    resp = client.post(
        V1_URL, json=_v1_body("room-77"), headers={SOURCE_HEADER: "scheduler"}
    )

    assert resp.status_code == 200
    assert "room=room-77 source=scheduler" in app_logs.text
    assert "[v1 완료]" in app_logs.text


def test_v1_logs_room_and_source_on_failure(client, app_logs, monkeypatch):
    # 실패한 요청이야말로 꼭 남아야 합니다. 배치가 다시 돌릴 대상을 여기서 찾습니다.
    def _slow(messages, reference_date):
        raise GemmaTimeoutError("시간 초과")

    monkeypatch.setattr(meeting_route, "extract_meeting_drafts", _slow)

    resp = client.post(
        V1_URL, json=_v1_body("room-88"), headers={SOURCE_HEADER: "scheduler"}
    )

    assert resp.status_code == 504
    assert "[v1 실패] room=room-88 source=scheduler" in app_logs.text


def test_v1_logs_unknown_source_without_header(client, app_logs, monkeypatch):
    # 헤더를 안 보내도 요청은 그대로 처리되고, 출처만 unknown 으로 남습니다.
    monkeypatch.setattr(
        meeting_route,
        "extract_meeting_drafts",
        lambda messages, reference_date: [],
    )

    resp = client.post(V1_URL, json=_v1_body("room-99"))

    assert resp.status_code == 200
    assert "room=room-99 source=unknown" in app_logs.text


def test_v1_unknown_header_value_does_not_reject(client, monkeypatch):
    # 모르는 헤더 값 때문에 멀쩡한 요청이 막히면 안 됩니다.
    monkeypatch.setattr(
        meeting_route,
        "extract_meeting_drafts",
        lambda messages, reference_date: [],
    )

    resp = client.post(V1_URL, json=_v1_body(), headers={SOURCE_HEADER: "cron-job"})

    assert resp.status_code == 200


def test_v2_logs_room_and_source(client, app_logs, monkeypatch):
    # v2 창구도 같은 꼬리표를 남기는지 확인합니다.
    monkeypatch.setattr(
        meeting_v2_route,
        "extract_meeting_drafts_v2",
        lambda participants, messages, reference_date: [],
    )

    resp = client.post(
        V2_URL, json=_v2_body("room-55"), headers={SOURCE_HEADER: "manual"}
    )

    assert resp.status_code == 200
    assert "room=room-55 source=manual" in app_logs.text
    assert "[v2 완료]" in app_logs.text


def test_v2_logs_participant_error(client, app_logs):
    # 명부 오류(400)도 어느 방에서 났는지 남아야 합니다.
    body = _v2_body("room-56")
    body["participants"] = ["u-101", "u-102"]  # 3명 미만 -> 400

    resp = client.post(V2_URL, json=body, headers={SOURCE_HEADER: "scheduler"})

    assert resp.status_code == 400
    assert "[v2 실패] room=room-56 source=scheduler" in app_logs.text


def test_max_room_id_in_log_is_reasonable():
    # 상한이 너무 짧으면 실제 방 ID 가 잘려서 짝을 못 찾습니다.
    assert MAX_ROOM_ID_IN_LOG >= 32
