from datetime import datetime

from app.schemas.route_intent import RouteIntentMessage
from app.services.route_intent_extractor import extract_route_intent, to_route_intent


def _messages(*contents: str):
    return [
        RouteIntentMessage(sender_id=f"u-{index}", content=content, sent_at=datetime(2026, 8, 28, 9, index))
        for index, content in enumerate(contents, 1)
    ]


def test_ready_route_with_multiple_waypoints():
    messages = _messages(
        "서울숲에서 출발해서 성수대교를 지나자",
        "응, 한강공원도 들렀다가 뚝섬유원지까지 러닝하자",
    )
    result = to_route_intent(
        {
            "route_mode": "POINTS",
            "activity_type": "RUN",
            "start": {"query": "서울숲"},
            "waypoints": [{"query": "성수대교"}, {"query": "한강공원"}],
            "destination": {"query": "뚝섬유원지"},
            "message": None,
        },
        messages,
    )
    assert result.status == "READY"
    assert result.activity_type == "RUN"
    assert result.start.query == "서울숲"
    assert [point.query for point in result.waypoints] == ["성수대교", "한강공원"]


def test_ready_round_trip_with_start_activity_and_distance():
    result = to_route_intent(
        {
            "route_mode": "ROUND_TRIP",
            "activity_type": "RUN",
            "start": {"query": "판교역"},
            "target_distance_km": 6,
            "destination": None,
        },
        _messages("이틀 후에 판교역에서 출발해서 6km 정도 달릴래요?"),
    )
    assert result.status == "READY"
    assert result.route_mode == "ROUND_TRIP"
    assert result.target_distance_km == 6
    assert result.destination is None


def test_explicit_round_trip_does_not_call_external_model(monkeypatch):
    monkeypatch.setattr(
        "app.services.route_intent_extractor.chat_completion",
        lambda _: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    result = extract_route_intent(
        _messages("이틀 후에 판교역에서 출발해서 6km 정도 달릴래요?")
    )
    assert result.status == "READY"
    assert result.route_mode == "ROUND_TRIP"
    assert result.start.query == "판교역"
    assert result.activity_type == "RUN"
    assert result.target_distance_km == 6


def test_explicit_point_route_with_optional_waypoint(monkeypatch):
    monkeypatch.setattr(
        "app.services.route_intent_extractor.chat_completion",
        lambda _: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    result = extract_route_intent(
        _messages("서울숲에서 출발해서 성수대교를 경유해서 뚝섬유원지까지 러닝하자")
    )
    assert result.status == "READY"
    assert result.route_mode == "POINTS"
    assert result.start.query == "서울숲"
    assert result.destination.query == "뚝섬유원지"
    assert [point.query for point in result.waypoints] == ["성수대교"]


def test_explicit_point_route_preserves_place_starting_with_particle_character(monkeypatch):
    monkeypatch.setattr(
        "app.services.route_intent_extractor.chat_completion",
        lambda _: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    result = extract_route_intent(
        _messages("오늘 오후 6시에 판교역에서 출발해서 서현역을 경유해서 이매역까지 달릴래요?")
    )
    assert result.status == "READY"
    assert result.start.query == "판교역"
    assert [point.query for point in result.waypoints] == ["서현역"]
    assert result.destination.query == "이매역"
    assert result.activity_type == "RUN"


def test_insufficient_context_lists_missing_information():
    result = to_route_intent(
        {"activity_type": "산책", "start": {"query": "서울숲"}},
        _messages("서울숲에서 산책하자"),
    )
    assert result.status == "INSUFFICIENT_CONTEXT"
    assert result.destination is None
    assert "목적지" in result.message


def test_discards_place_not_present_in_chat():
    result = to_route_intent(
        {
            "activity_type": "CYCLE",
            "start": {"query": "서울숲"},
            "destination": {"query": "AI가 지어낸 공원"},
        },
        _messages("서울숲에서 자전거 타자"),
    )
    assert result.status == "INSUFFICIENT_CONTEXT"
    assert result.destination is None


def test_unknown_activity_is_not_guessed():
    result = to_route_intent(
        {
            "activity_type": "DRIVE",
            "start": {"query": "서울숲"},
            "destination": {"query": "한강공원"},
        },
        _messages("서울숲에서 한강공원까지 가자"),
    )
    assert result.status == "INSUFFICIENT_CONTEXT"
    assert result.activity_type is None
