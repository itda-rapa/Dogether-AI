"""[단체 채팅방(v2) 사례 실측 채점 — 현재 v2 프롬프트 기준]

app/prompts/meeting_prompt_v2.py 의 '현재' SYSTEM_PROMPT_V2 로 실제 Gemma 를
호출해 dataset_group.py 의 사례를 채점하고, 그룹·평가 등급별 성공률을 집계한다.
(M2_FLOW A-6 의 4번 단계)

v1 의 run_200.py 와 다른 점은 채점 항목이 하나 더 있다는 것뿐이다.

- 성공(pass) = 카드 목록이 기대와 완전히 일치 (개수 · 네 필드 · **참여자**까지)
- 실패(fail) = 그 외 (참여자 불일치, 카드 수 불일치, 파싱 실패 포함)
- 참여자 비교는 순서를 따지지 않는다. (집합으로 비교)

서버 라우트가 아니라 extract_meeting_drafts_v2 를 그대로 호출하므로,
이름표 붙이기·떼기와 정족수 판정까지 운영과 똑같은 경로를 지난다.

실행:
    python tests/cli_debug_test/run_group.py                  # 전체
    python tests/cli_debug_test/run_group.py --limit 10        # 앞 10개만
    python tests/cli_debug_test/run_group.py --category Q      # 정족수만
    python tests/cli_debug_test/run_group.py --out docs/PROMPT_TEST_RESULT_GROUP.md
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.schemas.meeting_v2 import MessageV2
from app.services.meeting_extractor_v2 import extract_meeting_drafts_v2
from tests.cli_debug_test.dataset_group import CAT_NAMES, DATASET_GROUP, TIERS

# 카드에서 채점할 항목. participant_ids 가 v2 에서 새로 채점되는 항목이다.
FIELDS = ("meeting_type", "date", "time", "place", "participant_ids")
_MISSING = object()


def _call_model(case):
    """운영과 같은 경로로 카드 목록(dict)을 얻는다. 실패 시 None."""

    messages = [MessageV2(**m) for m in case["messages"]]
    cards = extract_meeting_drafts_v2(
        participants=case["participants"],
        messages=messages,
        reference_date=case["reference_date"],
    )
    return [c.model_dump() for c in cards]


def _fmatch(field, e, a):
    """한 항목이 맞는지 본다. 참여자는 순서를 무시하고 집합으로 비교한다."""

    if e is _MISSING or a is _MISSING:
        return False
    if field == "participant_ids":
        return set(e or []) == set(a or [])
    return (e or None) == (a or None)


def _sort_key(card):
    # 기대 카드와 실제 카드를 같은 기준으로 줄 세워 짝을 맞춘다.
    return (card.get("date") or "", card.get("time") or "", card.get("place") or "")


def _score(expected, actual):
    """(맞은 항목 수, 전체 항목 수, 완전일치 여부)."""

    if actual is None:  # 호출/파싱 실패
        return 0, len(FIELDS), False
    if not expected and not actual:
        return len(FIELDS), len(FIELDS), True

    exp = sorted(expected, key=_sort_key)
    act = sorted(actual, key=_sort_key)
    n = max(len(exp), len(act))
    total, correct = n * len(FIELDS), 0
    for i in range(n):
        e = exp[i] if i < len(exp) else None
        a = act[i] if i < len(act) else None
        for f in FIELDS:
            ev = e[f] if e is not None else _MISSING
            av = a.get(f, _MISSING) if a is not None else _MISSING
            if _fmatch(f, ev, av):
                correct += 1
    return correct, total, (correct == total and len(exp) == len(act))


def _new_bucket():
    return {"scored": 0, "passed": 0, "failed": 0, "fcorrect": 0, "ftotal": 0}


def _rate(b):
    return (b["passed"] / b["scored"] * 100) if b["scored"] else 0.0


def _facc(b):
    return (b["fcorrect"] / b["ftotal"] * 100) if b["ftotal"] else 0.0


def _brief(cards):
    """리포트에 넣을 짧은 카드 표기."""

    if cards is None:
        return "(호출/파싱 실패)"
    if not cards:
        return "[]"
    return " | ".join(
        f"{c.get('meeting_type')}/{c.get('date')}/{c.get('time')}/{c.get('place')}"
        f"/{sorted(c.get('participant_ids') or [])}"
        for c in cards
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="앞 N개만 실행(0=전체)")
    ap.add_argument("--category", default=None, help=f"특정 그룹만 ({''.join(CAT_NAMES)})")
    ap.add_argument(
        "--out",
        default="docs/PROMPT_TEST_RESULT_GROUP.md",
        help="결과 리포트 저장 경로",
    )
    args = ap.parse_args()

    cases = DATASET_GROUP
    if args.category:
        cases = [c for c in cases if c["cat"] == args.category.upper()]
    if args.limit:
        cases = cases[: args.limit]

    per_cat = {c: _new_bucket() for c in CAT_NAMES}
    per_tier = {t: _new_bucket() for t in TIERS}
    failures = []

    total = len(cases)
    print(f"총 {total}개 사례 채점 시작 (v2 프롬프트, 참여자까지 채점)\n")
    t0 = time.perf_counter()

    for idx, case in enumerate(cases, 1):
        cat, tier, expected = case["cat"], case["tier"], case["expected"]
        cb, tb = per_cat[cat], per_tier[tier]

        try:
            actual = _call_model(case)
        except Exception as exc:  # 호출 실패
            actual = None
            print(f"[{idx:3d}/{total}] {case['id']} {tier}  ! 호출오류 {type(exc).__name__}: {exc}")

        correct, tot, passed = _score(expected, actual)
        for b in (cb, tb):
            b["scored"] += 1
            b["fcorrect"] += correct
            b["ftotal"] += tot
            b["passed" if passed else "failed"] += 1

        mark = "PASS" if passed else "FAIL"
        print(
            f"[{idx:3d}/{total}] {case['id']} ({cat}·{tier})  {mark}  "
            f"({correct}/{tot} 항목) · {len(case['participants'])}명 방"
        )
        if not passed:
            failures.append((case["id"], cat, tier, expected, actual))

    elapsed = time.perf_counter() - t0
    report = _render(per_cat, per_tier, failures, total, elapsed)
    print("\n" + report)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\n리포트 저장: {out}")


def _render(per_cat, per_tier, failures, total, elapsed):
    L = []
    L.append("# 단체 채팅방(v2) 사례 실측 결과 — 현재 v2 프롬프트")
    L.append("")
    L.append(f"- 총 사례: {total}개 · 소요: {elapsed:.1f}초")
    L.append("- 성공 = 카드 목록이 기대와 완전 일치 (네 필드 + 참여자)")
    L.append("- 참여자는 순서를 무시하고 집합으로 비교")
    L.append("")

    tot = _new_bucket()
    for b in per_cat.values():
        for k in tot:
            tot[k] += b[k]

    L.append("## 전체 요약")
    L.append("")
    L.append(f"- 채점 대상: {tot['scored']}개 (성공 {tot['passed']} / 실패 {tot['failed']})")
    L.append(f"- 성공률: **{_rate(tot):.1f}%** · 항목 정확도: {_facc(tot):.1f}%")
    L.append("")

    L.append("## 평가 등급별")
    L.append("")
    L.append("| 등급 | 채점 | 성공 | 실패 | 성공률 | 항목정확도 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for t in TIERS:
        b = per_tier[t]
        if b["scored"] == 0:
            continue
        L.append(
            f"| {t} | {b['scored']} | {b['passed']} | {b['failed']} | "
            f"{_rate(b):.1f}% | {_facc(b):.1f}% |"
        )
    L.append("")

    L.append("## 그룹별")
    L.append("")
    L.append("| 그룹 | 이름 | 채점 | 성공 | 실패 | 성공률 |")
    L.append("|---|---|---:|---:|---:|---:|")
    for c, name in CAT_NAMES.items():
        b = per_cat[c]
        if b["scored"] == 0:
            continue
        L.append(
            f"| {c} | {name} | {b['scored']} | {b['passed']} | {b['failed']} | "
            f"{_rate(b):.1f}% |"
        )
    L.append("")

    if failures:
        L.append("## 실패 사례")
        L.append("")
        for case_id, cat, tier, expected, actual in failures:
            L.append(f"### {case_id} ({cat}·{tier})")
            L.append("")
            L.append(f"- 기대: `{_brief(expected)}`")
            L.append(f"- 실제: `{_brief(actual)}`")
            L.append("")

    return "\n".join(L)


if __name__ == "__main__":
    main()
