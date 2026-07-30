"""[200개 사례 실측 채점 — 현재 프롬프트 기준]

app/prompts/meeting_prompt.py 의 '현재' SYSTEM_PROMPT 로 실제 Gemma 를 호출해
dataset_200.py 의 200개 사례를 채점하고, 문서 그룹(A~T)·평가(안정/주의/취약)별
성공/실패율을 집계한다.

- 성공(pass) = 모델 카드 목록이 기대 카드 목록과 '완전히 일치' (개수·필드 전부)
- 실패(fail) = 그 외 (필드 불일치, 카드 수 불일치, 파싱 실패 포함)
- 정책 미정(POLICY) = 채점 제외, 별도 집계

실행:
    python tests/cli_debug_test/run_200.py                 # 전체
    python tests/cli_debug_test/run_200.py --limit 20       # 앞 20개만(빠른 점검)
    python tests/cli_debug_test/run_200.py --category C      # 특정 그룹만
    python tests/cli_debug_test/run_200.py --out docs/PROMPT_TEST_RESULT_200.md
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

from app.config import get_settings
from app.prompts.meeting_prompt import SYSTEM_PROMPT, build_user_prompt
from app.services import meeting_extractor as mx
from app.services.gemma_client import get_client
from tests.cli_debug_test.dataset_200 import CAT_NAMES, DATASET_200, POLICY

FIELDS = ("meeting_type", "date", "time", "place")
TIERS = ("안정", "주의", "취약")
_MISSING = object()


def _call_model(messages_pairs_dicts, reference_date):
    """현재 SYSTEM_PROMPT 로 모델을 호출하고 카드 목록(dict)으로 반환. 실패 시 None."""
    settings = get_settings()
    client = get_client()
    # dict 메시지를 그대로 'sender: content' 로 정리
    conv = "\n".join(f"{m['sender']}: {m['content']}" for m in messages_pairs_dicts)
    user_prompt = build_user_prompt(conv, reference_date)
    resp = client.chat.completions.create(
        model=settings.gemma_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=settings.gemma_temperature,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    content = resp.choices[0].message.content or ""
    try:
        cards = mx.to_cards(mx.parse_json(content))
    except mx.ExtractionError:
        return None, content
    return [c.model_dump() for c in cards], content


def _fmatch(e, a):
    return (e or None) == (a or None)


def _score(expected, actual):
    """(맞은 필드, 전체 필드, 완전일치 여부)."""
    if actual is None:  # 파싱 실패
        return 0, len(FIELDS), False
    if not expected and not actual:
        return len(FIELDS), len(FIELDS), True
    exp = sorted(expected, key=lambda c: (c.get("place") or ""))
    act = sorted(actual, key=lambda c: (c.get("place") or ""))
    n = max(len(exp), len(act))
    total, correct = n * len(FIELDS), 0
    for i in range(n):
        e = exp[i] if i < len(exp) else None
        a = act[i] if i < len(act) else None
        for f in FIELDS:
            ev = e[f] if e is not None else _MISSING
            av = a[f] if a is not None else _MISSING
            if _fmatch(ev, av):
                correct += 1
    return correct, total, (correct == total and len(exp) == len(act))


def _new_bucket():
    return {"scored": 0, "passed": 0, "failed": 0, "policy": 0, "fcorrect": 0, "ftotal": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="앞 N개만 실행(0=전체)")
    ap.add_argument("--category", default=None, help="특정 그룹(A~T)만")
    ap.add_argument("--out", default="docs/PROMPT_TEST_RESULT_200.md", help="결과 리포트 저장 경로")
    args = ap.parse_args()

    cases = DATASET_200
    if args.category:
        cases = [c for c in cases if c["cat"] == args.category.upper()]
    if args.limit:
        cases = cases[: args.limit]

    per_cat = {c: _new_bucket() for c in CAT_NAMES}
    per_tier = {t: _new_bucket() for t in TIERS}
    failures = []

    total = len(cases)
    print(f"총 {total}개 사례 채점 시작 (현재 프롬프트, enable_thinking=False)\n")
    t0 = time.perf_counter()

    for idx, case in enumerate(cases, 1):
        cat, tier, expected = case["cat"], case["tier"], case["expected"]
        cb, tb = per_cat[cat], per_tier[tier]

        if expected == POLICY:
            cb["policy"] += 1
            tb["policy"] += 1
            print(f"[{idx:3d}/{total}] {case['id']} {tier}  · 정책미정(제외) — {case.get('reason','')}")
            continue

        try:
            actual, _raw = _call_model(case["messages"], case["reference_date"])
        except Exception as exc:  # 호출 실패
            actual = None
            print(f"[{idx:3d}/{total}] {case['id']} {tier}  ! 호출오류 {type(exc).__name__}")

        correct, tot, passed = _score(expected, actual)
        for b in (cb, tb):
            b["scored"] += 1
            b["fcorrect"] += correct
            b["ftotal"] += tot
            b["passed" if passed else "failed"] += 1

        mark = "PASS" if passed else "FAIL"
        print(f"[{idx:3d}/{total}] {case['id']} {tier}  {mark}  ({correct}/{tot} 필드)")
        if not passed:
            failures.append((case["id"], tier, expected, actual))

    elapsed = time.perf_counter() - t0
    report = _render(per_cat, per_tier, failures, total, elapsed)
    print("\n" + report)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\n리포트 저장: {out}")


def _rate(b):
    return (b["passed"] / b["scored"] * 100) if b["scored"] else 0.0


def _facc(b):
    return (b["fcorrect"] / b["ftotal"] * 100) if b["ftotal"] else 0.0


def _render(per_cat, per_tier, failures, total, elapsed):
    L = []
    L.append("# 200개 사례 실측 결과 — 현재 프롬프트")
    L.append("")
    L.append(f"- 총 사례: {total}개 · 소요: {elapsed:.1f}초")
    L.append("- 성공 = 기대 카드 목록과 완전 일치 / 실패 = 그 외(파싱 실패 포함)")
    L.append("- 정책미정 = 채점 제외(별도 집계)")
    L.append("")

    tot = _new_bucket()
    for b in per_cat.values():
        for k in tot:
            tot[k] += b[k]

    L.append("## 전체 요약")
    L.append("")
    L.append(f"- 채점 대상: {tot['scored']}개 (성공 {tot['passed']} / 실패 {tot['failed']})")
    L.append(f"- 성공률: **{_rate(tot):.1f}%** · 필드 정확도: {_facc(tot):.1f}%")
    L.append(f"- 정책미정(제외): {tot['policy']}개")
    L.append("")

    L.append("## 평가 등급별")
    L.append("")
    L.append("| 등급 | 채점 | 성공 | 실패 | 성공률 | 필드정확도 | 정책미정 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for t in TIERS:
        b = per_tier[t]
        L.append(f"| {t} | {b['scored']} | {b['passed']} | {b['failed']} | {_rate(b):.1f}% | {_facc(b):.1f}% | {b['policy']} |")
    L.append("")

    L.append("## 그룹(A~T)별")
    L.append("")
    L.append("| 그룹 | 이름 | 채점 | 성공 | 실패 | 성공률 | 정책미정 |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for c in CAT_NAMES:
        b = per_cat[c]
        if b["scored"] == 0 and b["policy"] == 0:
            continue
        L.append(f"| {c} | {CAT_NAMES[c]} | {b['scored']} | {b['passed']} | {b['failed']} | {_rate(b):.1f}% | {b['policy']} |")
    L.append("")

    if failures:
        L.append(f"## 실패 사례 상세 ({len(failures)}개)")
        L.append("")
        for cid, tier, exp, act in failures:
            L.append(f"- **{cid}** ({tier})")
            L.append(f"  - 기대: `{exp}`")
            L.append(f"  - 실제: `{act}`")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
