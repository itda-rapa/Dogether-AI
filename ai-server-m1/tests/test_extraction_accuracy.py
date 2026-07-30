"""[정확도 회귀 테스트]

메시지 상한을 200으로 올린 뒤에도, 그리고 '나들이 장소별 카드' 규칙을 넣은 뒤에도
약속 추출 품질이 떨어지지 않는지(회귀하지 않는지) 확인하는 테스트입니다.

두 부분으로 나뉩니다.

1. 데이터셋 유효성 검사 (항상 실행, 오프라인)
   - 골든 데이터셋이 스키마(개수 2~200, 날짜 형식 등)에 맞는지 확인합니다.
   - 실제 AI 없이 돌아가므로 기본 `pytest` 에 포함됩니다.

2. 실제 정확도 측정 (기본 skip, 실제 Gemma 필요)
   - 라벨링된 대화를 진짜 Gemma 로 추출해 필드별 정확도를 잽니다.
   - 응답이 '카드 목록' 이라, 기대 목록과 실제 목록을 장소 기준으로 맞춰 채점합니다.
   - 네트워크/모델이 필요하므로 환경변수로 켤 때만 실행됩니다:
       RUN_ACCURACY_TESTS=1 pytest tests/test_extraction_accuracy.py -s
   - 통과 기준(필드 단위 정확도)은 ACCURACY_THRESHOLD 로 조절합니다. (기본 0.85)

이렇게 나눈 이유: README 대로 기본 테스트는 모델 서버 없이도 빠르게 돌아야 하고,
정확도 측정은 회귀를 잡고 싶을 때 의도적으로 켜서 돌리기 위함입니다.
"""

import os

import pytest

from app.schemas.meeting import ALLOWED_MEETING_TYPES, MeetingDraftRequest
from tests.accuracy_dataset import DATASET

# 판정할 필드 4종
FIELDS = ("meeting_type", "date", "time", "place")

# 실제 Gemma 를 부르는 정확도 측정을 켜는 스위치
RUN_ACCURACY = os.getenv("RUN_ACCURACY_TESTS") == "1"

# 통과 기준 (필드 단위 정확도). 환경변수로 덮어쓸 수 있습니다.
THRESHOLD = float(os.getenv("ACCURACY_THRESHOLD", "0.85"))

# '있어야 할 카드가 없음/없어야 할 카드가 있음' 을 표시하는, 무엇과도 안 맞는 표식.
_MISSING = object()


# ---------------------------------------------------------------------------
# 1. 데이터셋 유효성 검사 (항상 실행, 오프라인)
# ---------------------------------------------------------------------------

def test_dataset_not_empty():
    assert len(DATASET) >= 5, "회귀를 의미있게 잡으려면 케이스가 충분해야 합니다."


@pytest.mark.parametrize("case", DATASET, ids=[c["id"] for c in DATASET])
def test_dataset_case_is_valid(case):
    """골든 케이스가 요청 스키마(2~200개, 날짜 형식 등)에 맞는지 검증합니다."""
    # 요청 스키마 검증을 그대로 통과해야 합니다. (실패 시 스키마 위반)
    MeetingDraftRequest(
        room_id="room-1",
        reference_date=case["reference_date"],
        messages=case["messages"],
    )
    # 기대값(정답) 카드들도 규칙에 맞는지 확인합니다.
    for card in case["expected"]:
        assert set(card.keys()) == set(FIELDS)
        if card["meeting_type"] is not None:
            assert card["meeting_type"] in ALLOWED_MEETING_TYPES


def test_dataset_covers_all_meeting_types_and_empty():
    """주요 종류 코드(WALK/PLAY/HOSPITAL)와 '빈 목록' 케이스가 모두 있는지 확인합니다.

    (OTHER 는 '그 외' 포괄 코드라 전용 골든 케이스는 두지 않습니다.)
    """
    seen = set()
    has_empty = False
    for c in DATASET:
        if not c["expected"]:
            has_empty = True
        for card in c["expected"]:
            seen.add(card["meeting_type"])
    for t in ("WALK", "PLAY", "HOSPITAL"):
        assert t in seen, f"'{t}' 케이스가 데이터셋에 없습니다."
    assert has_empty, "빈 목록(약속 없음) 케이스가 데이터셋에 없습니다."


def test_dataset_has_multi_place_outing_case():
    """나들이 장소별 카드 규칙을 검증하려면 장소 여러 개 케이스가 있어야 합니다."""
    multi = [c for c in DATASET if len(c["expected"]) >= 2]
    assert multi, "장소가 2곳 이상인 나들이 케이스가 있어야 합니다."
    assert all(
        card["meeting_type"] == "PLAY"
        for c in multi
        for card in c["expected"]
    ), "여러 카드 케이스는 나들이(PLAY)여야 합니다."


def test_dataset_has_long_conversation_case():
    """상한 상향(200)을 실제로 검증하려면 긴 대화 케이스가 있어야 합니다."""
    assert any(len(c["messages"]) >= 40 for c in DATASET), (
        "40개 이상 대화 케이스가 있어야 상한 상향의 영향을 잡을 수 있습니다."
    )


def test_dataset_has_near_cap_case():
    """상한(200)에 근접한 대용량 케이스가 있어야 대용량 문맥 회귀를 잡습니다."""
    assert any(len(c["messages"]) >= 150 for c in DATASET), (
        "150개 이상 대화 케이스가 있어야 상한 근처의 대용량 상황을 검증할 수 있습니다."
    )


# ---------------------------------------------------------------------------
# 2. 실제 정확도 측정 (기본 skip, 실제 Gemma 필요)
# ---------------------------------------------------------------------------

def _field_match(expected, actual) -> bool:
    """한 필드가 정답과 맞는지 비교합니다. (둘 다 None 이면 맞음)"""
    return (expected or None) == (actual or None)


def _score_case(exp_list, act_list):
    """기대 카드 목록과 실제 카드 목록을 비교해 (맞은 필드, 전체 필드, 실패목록) 반환.

    - 둘 다 비어 있으면(약속 없음을 정확히 맞힘) 만점 처리.
    - 카드는 place 기준으로 정렬해 짝을 맞춥니다.
    - 카드 수가 다르면, 빠지거나 남는 카드의 필드는 전부 오답으로 셉니다.
    """
    if not exp_list and not act_list:
        return len(FIELDS), len(FIELDS), []

    exp = sorted(exp_list, key=lambda c: (c.get("place") or ""))
    act = sorted(act_list, key=lambda c: (c.get("place") or ""))
    n = max(len(exp), len(act))
    total = n * len(FIELDS)
    correct = 0
    fails = []
    for i in range(n):
        e = exp[i] if i < len(exp) else None
        a = act[i] if i < len(act) else None
        for field in FIELDS:
            ev = e[field] if e is not None else _MISSING
            av = a[field] if a is not None else _MISSING
            if _field_match(ev, av):
                correct += 1
            else:
                fails.append(f"카드{i} {field}: 기대={ev!r} 실제={av!r}")
    return correct, total, fails


@pytest.mark.skipif(
    not RUN_ACCURACY,
    reason="RUN_ACCURACY_TESTS=1 로 켤 때만 실제 Gemma 정확도를 측정합니다.",
)
def test_extraction_accuracy_regression():
    """골든 데이터셋을 실제 Gemma 로 추출해 필드 단위 정확도를 측정합니다."""
    # 실제 모델을 부르는 함수는 이 테스트를 켤 때만 import 합니다.
    from app.services.meeting_extractor import extract_meeting_drafts

    total_fields = 0
    correct_fields = 0
    per_case = []
    failures = []

    for case in DATASET:
        messages = list(
            MeetingDraftRequest(
                room_id="room-1",
                reference_date=case["reference_date"],
                messages=case["messages"],
            ).messages
        )

        cards = extract_meeting_drafts(
            messages=messages, reference_date=case["reference_date"]
        )
        actual = [c.model_dump() for c in cards]
        exp = case["expected"]

        c_correct, c_total, c_fails = _score_case(exp, actual)
        total_fields += c_total
        correct_fields += c_correct
        per_case.append((case["id"], c_correct, c_total))
        for f in c_fails:
            failures.append(f"[{case['id']}] {f}")

    accuracy = correct_fields / total_fields if total_fields else 0.0

    # 사람이 읽을 수 있는 리포트를 출력합니다. (pytest -s 로 보임)
    print("\n===== 정확도 회귀 리포트 =====")
    for cid, c, n in per_case:
        print(f"  {cid:32s} {c}/{n}")
    print("  ----------------------------")
    print(f"  전체 필드 정확도: {correct_fields}/{total_fields} = {accuracy:.1%}")
    print(f"  통과 기준(THRESHOLD): {THRESHOLD:.1%}")
    if failures:
        print("  틀린 필드:")
        for f in failures:
            print(f"    - {f}")
    print("=============================")

    assert accuracy >= THRESHOLD, (
        f"정확도 {accuracy:.1%} 가 기준 {THRESHOLD:.1%} 아래입니다. "
        f"틀린 필드 {len(failures)}개 — 위 리포트를 확인하세요."
    )
