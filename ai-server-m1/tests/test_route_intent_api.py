import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import route_intent as route_intent_route
from app.schemas.route_intent import RouteIntentResult, RoutePlaceQuery
from app.services.gemma_client import GemmaError, GemmaTimeoutError

URL = "/api/v2/routes/extract"


@pytest.fixture
def client():
    return TestClient(create_app())


def _body(count=2):
    return {
        "room_id": "1",
        "messages": [
            {
                "sender_id": f"u-{index}",
                "content": f"경로 대화 {index}",
                "sent_at": f"2026-08-28T09:{index:02d}:00+09:00",
            }
            for index in range(count)
        ],
    }


def test_extract_route_intent_success(client, monkeypatch):
    monkeypatch.setattr(
        route_intent_route,
        "extract_route_intent",
        lambda messages: RouteIntentResult(
            status="READY",
            activity_type="WALK",
            start=RoutePlaceQuery(query="서울숲"),
            destination=RoutePlaceQuery(query="한강공원"),
        ),
    )
    response = client.post(URL, json=_body())
    assert response.status_code == 200
    assert response.json()["status"] == "READY"
    assert response.json()["start"] == {"query": "서울숲"}


def test_rejects_more_than_30_messages(client):
    assert client.post(URL, json=_body(31)).status_code == 422


def test_gemma_failure_is_502(client, monkeypatch):
    def fail(messages):
        raise GemmaError("offline")

    monkeypatch.setattr(route_intent_route, "extract_route_intent", fail)
    assert client.post(URL, json=_body()).status_code == 502


def test_gemma_timeout_is_504(client, monkeypatch):
    def timeout(messages):
        raise GemmaTimeoutError("timeout")

    monkeypatch.setattr(route_intent_route, "extract_route_intent", timeout)
    assert client.post(URL, json=_body()).status_code == 504
