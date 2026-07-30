"""[파이프라인 디버그 트레이서 — CLI]

FastAPI/Swagger 를 띄우지 않고, 약속 추출 파이프라인을 로컬에서 '한 노드씩'
직접 태워보며 모든 과정을 날것으로 보여주는 디버깅 도구입니다.

- 예쁜 결과가 목적이 아니라, 각 노드를 어떻게 통과하는지 관찰이 목적입니다.
- LLM 내부 기술은 확정 판별할 수 없으므로, 대신
  (1) 우리 아키텍처/파이프라인 노드 통과 과정과
  (2) 모델이 내놓은 추론과정(reasoning_content),
  (3) API 가 노출하는 모델/토큰 단서를 정직하게 덤프합니다.

운영 코드(app/)는 건드리지 않고, 기존 함수를 '관찰'만 합니다.
단, NODE 3(Gemma 호출)만은 풍부한 응답 객체(추론·토큰)를 보기 위해
OpenAI 클라이언트를 직접 호출하고, 받은 content 는 실제 parse_json/to_cards
에 그대로 흘려 동일 경로를 검증합니다.

실행 예:
    python tests/cli_debug_test/pipeline_trace.py --scenario outing
    python tests/cli_debug_test/pipeline_trace.py --file my_convo.json --no-think
    python tests/cli_debug_test/pipeline_trace.py --list
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

# 프로젝트 루트(ai-server-m1)를 import 경로에 넣어, app/tests 를 모듈로 불러옵니다.
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 콘솔에 한글이 깨지지 않도록 UTF-8 로 맞춥니다.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.config import get_settings  # noqa: E402
from app.prompts.meeting_prompt import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from app.schemas.meeting import ALLOWED_MEETING_TYPES, MeetingDraftRequest  # noqa: E402
from app.services import meeting_extractor as mx  # noqa: E402
from app.services.gemma_client import get_client  # noqa: E402
from tests.accuracy_dataset import DATASET  # noqa: E402

# 친숙한 별칭 -> 골든 데이터셋 id (골든 데이터를 재활용해 중복을 없앰)
SCENARIO_ALIASES = {
    "walk": "simple_walk_tomorrow",
    "outing": "outing_multiple_places",
    "outing1": "outing_day_after_tomorrow",
    "hospital": "hospital_visit",
    "empty": "no_agreement_empty_card",
    "partial": "partial_no_time",
    "long": "long_chat_scattered_agreement",
    "big": "near_cap_198_messages",
    "injection": "prompt_injection_resistance",
}

# 미리보기에서 너무 길면 잘라내는 기본 길이
PREVIEW_CHARS = 1200
PREVIEW_MSGS = 12


# ---------------------------------------------------------------------------
# 출력 도우미
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "═" * 78)
    print(f"  {title}")
    print("═" * 78)


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 70 - len(title)))


def clip(text: str, limit: int, full: bool) -> str:
    if full or text is None or len(text) <= limit:
        return text if text is not None else ""
    return text[:limit] + f"\n  …(생략: 전체 {len(text)}자, --full 로 전체 보기)"


# ---------------------------------------------------------------------------
# 시나리오 로딩
# ---------------------------------------------------------------------------

def _dataset_by_id(case_id: str):
    for c in DATASET:
        if c["id"] == case_id:
            return c
    return None


def load_scenario(scenario: str):
    """별칭 또는 데이터셋 id 로 시나리오를 찾습니다."""
    case_id = SCENARIO_ALIASES.get(scenario, scenario)
    case = _dataset_by_id(case_id)
    if case is None:
        raise SystemExit(
            f"[오류] 시나리오 '{scenario}' 를 찾지 못했습니다. --list 로 목록을 확인하세요."
        )
    return case["reference_date"], case["messages"], case_id


def load_file(path: str):
    """사용자 JSON 파일에서 대화를 읽습니다.

    형식: {"reference_date": "YYYY-MM-DD", "messages": [ {sender,content,sent_at}, ... ]}
    reference_date 가 없으면 오늘 날짜 대신 데이터셋 기준일을 씁니다.
    """
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    messages = data.get("messages", data if isinstance(data, list) else [])
    reference_date = data.get("reference_date", "2026-07-24")
    return reference_date, messages, f"file:{path}"


def print_scenarios() -> None:
    banner("사용 가능한 시나리오")
    print("  별칭          →  데이터셋 id            (메시지 수, 기대 카드 수)")
    print("  " + "-" * 68)
    for alias, cid in SCENARIO_ALIASES.items():
        case = _dataset_by_id(cid)
        n_msg = len(case["messages"]) if case else "?"
        n_exp = len(case["expected"]) if case else "?"
        print(f"  {alias:12s}  →  {cid:24s} ({n_msg}개 메시지, 카드 {n_exp}개)")


# ---------------------------------------------------------------------------
# 아키텍처 맵
# ---------------------------------------------------------------------------

def print_architecture_map() -> None:
    banner("아키텍처 맵 — 파이프라인 노드 ↔ 실제 모듈/함수")
    rows = [
        ("NODE 0", "INPUT",        "요청 대화 messages + reference_date",       "schemas.meeting.MeetingDraftRequest"),
        ("NODE 1", "대화 정리",     "대화를 '이름: 말' 한 덩어리로",             "meeting_extractor.format_conversation"),
        ("NODE 2", "프롬프트 생성", "SYSTEM_PROMPT + build_user_prompt",         "prompts.meeting_prompt"),
        ("NODE 3", "Gemma 호출",    "OpenAI 호환 chat.completions",              "services.gemma_client (+OpenAI SDK)"),
        ("NODE 4", "JSON 파싱",     "응답에서 카드 목록 추출",                   "meeting_extractor.parse_json"),
        ("NODE 5", "카드 변환",     "값 검증·정규화·빈 카드 제거",               "meeting_extractor.to_cards / to_card"),
        ("NODE 6", "OUTPUT",        "list[MeetingDraft]",                        "route: POST /meeting-drafts/extract"),
    ]
    for tag, name, desc, mod in rows:
        print(f"  {tag}  {name:12s} {desc}")
        print(f"          └─ {mod}")
    print("\n  흐름:  INPUT → 정리 → 프롬프트 → [LLM] → 파싱 → 변환 → OUTPUT")


# ---------------------------------------------------------------------------
# NODE 4 보조: 어느 파싱 분기를 탔는지 알아냄 (표시용, 결과는 실제 parse_json 사용)
# ---------------------------------------------------------------------------

def diagnose_parse_branch(raw: str) -> str:
    candidate = mx._strip_code_fence(raw)
    try:
        json.loads(candidate)
        return "1차: 통짜 파싱 성공 (```/설명 없이 순수 JSON)"
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in mx._outer_brackets_first(candidate):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            snippet = candidate[start : end + 1]
            try:
                json.loads(snippet)
                return f"2차: '{open_ch}…{close_ch}' 범위 잘라서 파싱 성공"
            except json.JSONDecodeError:
                continue
    return "실패: JSON 을 찾지 못함 (ExtractionError 예상)"


# ---------------------------------------------------------------------------
# NODE 5 보조: 어떤 필드가 왜 null 로 떨어졌는지
# ---------------------------------------------------------------------------

def why_null(field: str, raw_value, norm_value) -> str:
    if norm_value is not None:
        return "OK"
    if raw_value in (None, "", "null"):
        return "원래 비어있음/null"
    if field == "meeting_type":
        return f"허용 3종({'/'.join(ALLOWED_MEETING_TYPES)}) 아님 → null"
    if field == "date":
        return "YYYY-MM-DD 형식 아님 → null"
    if field == "time":
        return "HH:MM 형식 아님 → null"
    return "정규화로 제거"


# ---------------------------------------------------------------------------
# 메인 트레이스
# ---------------------------------------------------------------------------

def trace(reference_date: str, messages: list, label: str, think: bool, full: bool) -> None:
    print_architecture_map()

    settings = get_settings()

    # ── NODE 0: INPUT ──────────────────────────────────────────────────
    banner(f"NODE 0 · INPUT  (시나리오: {label})")
    print(f"  reference_date : {reference_date}")
    print(f"  메시지 수       : {len(messages)}개")
    # 스키마 검증도 실제로 통과시키며, Message 객체로 변환
    req = MeetingDraftRequest(
        room_id="debug-room", reference_date=reference_date, messages=messages
    )
    msg_objs = list(req.messages)
    print("  (MeetingDraftRequest 스키마 검증 통과 ✓)")
    shown = msg_objs if (full or len(msg_objs) <= PREVIEW_MSGS) else (
        msg_objs[:4] + msg_objs[-4:]
    )
    sub("메시지 미리보기")
    if not (full or len(msg_objs) <= PREVIEW_MSGS):
        print(f"  (처음 4개 + 마지막 4개만 표시 / 전체 {len(msg_objs)}개, --full 로 전체)")
    for m in shown:
        print(f"    {m.sender}: {m.content}")

    # ── NODE 1: 대화 정리 ──────────────────────────────────────────────
    banner("NODE 1 · 대화 정리  →  meeting_extractor.format_conversation")
    conversation_text = mx.format_conversation(msg_objs)
    print(f"  출력 길이 : {len(conversation_text)}자")
    sub("format_conversation 결과")
    print(clip(conversation_text, PREVIEW_CHARS, full))

    # ── NODE 2: 프롬프트 생성 ─────────────────────────────────────────
    banner("NODE 2 · 프롬프트 생성  →  prompts.meeting_prompt")
    user_prompt = build_user_prompt(conversation_text, reference_date)
    sub("SYSTEM_PROMPT (역할·규칙, 항상 붙음)")
    print(clip(SYSTEM_PROMPT, PREVIEW_CHARS, full))
    sub("USER 프롬프트 (실제 질문)")
    print(clip(user_prompt, PREVIEW_CHARS, full))

    # ── NODE 3: Gemma 호출 ────────────────────────────────────────────
    banner("NODE 3 · Gemma 호출  →  services.gemma_client (+ OpenAI SDK)")
    chat_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    sub("요청 파라미터")
    print(f"  base_url            : {settings.gemma_base_url}")
    print(f"  model               : {settings.gemma_model}")
    print(f"  temperature         : {settings.gemma_temperature}")
    print(f"  timeout(초)         : {settings.gemma_timeout_seconds}")
    print(f"  enable_thinking     : {think}  (이 디버그 도구 전용 설정)")
    print("  extra_body          : "
          f'{{"chat_template_kwargs": {{"enable_thinking": {str(think).lower()}}}}}')

    client = get_client()
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=settings.gemma_model,
        messages=chat_messages,
        temperature=settings.gemma_temperature,
        extra_body={"chat_template_kwargs": {"enable_thinking": think}},
    )
    latency = time.perf_counter() - t0

    choice = resp.choices[0]
    message = choice.message
    content = message.content or ""
    reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)

    print(f"\n  ⏱  호출 지연        : {latency:.2f}초")
    print(f"  finish_reason       : {choice.finish_reason}")
    print(f"  응답 model          : {getattr(resp, 'model', 'N/A')}")

    sub("추론과정 (reasoning_content) — LLM 이 생각한 흔적")
    if reasoning:
        print(f"  [추론 {len(reasoning)}자]")
        print(clip(reasoning, PREVIEW_CHARS, full))
    else:
        print("  (없음 — enable_thinking=False 이거나 모델이 추론을 노출하지 않음)")

    sub("최종 응답 content (파싱 대상 원문)")
    print(clip(content, PREVIEW_CHARS, full))

    # ── NODE 4: JSON 파싱 ─────────────────────────────────────────────
    banner("NODE 4 · JSON 파싱  →  meeting_extractor.parse_json")
    print(f"  파싱 분기 : {diagnose_parse_branch(content)}")
    try:
        parsed = mx.parse_json(content)
    except mx.ExtractionError as exc:
        print(f"\n  ❌ ExtractionError: {exc}")
        print("  → 실제 API 였다면 502(응답 변환 실패)로 응답됩니다.")
        return
    print(f"  추출된 후보 카드 : {len(parsed)}개")
    sub("parse_json 결과(정규화 전 원자료)")
    for i, item in enumerate(parsed):
        print(f"  [{i}] {item}")

    # ── NODE 5: 카드 변환·검증 ────────────────────────────────────────
    banner("NODE 5 · 카드 변환·검증  →  meeting_extractor.to_cards / to_card")
    for i, item in enumerate(parsed):
        card = mx.to_card(item)
        d = card.model_dump()
        print(f"\n  [{i}] 정규화 전 → 후")
        for field in ("meeting_type", "date", "time", "place"):
            raw_v = item.get(field)
            norm_v = d[field]
            note = why_null(field, raw_v, norm_v)
            flag = "" if note == "OK" else f"   ⟵ {note}"
            print(f"       {field:13s}: {raw_v!r:>20}  →  {norm_v!r}{flag}")
    cards = mx.to_cards(parsed)
    dropped = len(parsed) - len(cards)
    print(f"\n  빈 카드 제거 : {dropped}개 제거  →  최종 {len(cards)}개")

    # ── NODE 6: OUTPUT ────────────────────────────────────────────────
    banner("NODE 6 · OUTPUT  →  list[MeetingDraft]  (API 응답과 동일)")
    print(json.dumps([c.model_dump() for c in cards], ensure_ascii=False, indent=2))

    # ── 모델/기술 단서 (정직한 덤프) ─────────────────────────────────
    banner("모델/기술 단서  (API 가 노출하는 것만 — 내부 아키텍처 '확정 아님')")
    usage = getattr(resp, "usage", None)
    print(f"  model id            : {getattr(resp, 'model', 'N/A')}")
    if usage is not None:
        print(f"  prompt_tokens       : {getattr(usage, 'prompt_tokens', 'N/A')}")
        print(f"  completion_tokens   : {getattr(usage, 'completion_tokens', 'N/A')}")
        print(f"  total_tokens        : {getattr(usage, 'total_tokens', 'N/A')}")
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            print(f"  prompt_tokens_details: {details}  (cached_tokens=캐싱 단서)")
    print(f"  reasoning 노출       : {'예' if reasoning else '아니오'}  "
          "(예=추론형 동작 단서)")
    print(f"  system_fingerprint  : {getattr(resp, 'system_fingerprint', 'N/A')}")
    # 응답 객체에 우리가 안 본 필드가 더 있는지 열쇠만 덤프
    try:
        keys = sorted(resp.model_dump().keys())
        print(f"  응답 최상위 필드     : {keys}")
    except Exception:
        pass
    print("\n  ※ 위 값들은 API 가 준 '단서'일 뿐, LLM 내부 아키텍처/기법을 "
          "확정하지는 못합니다.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="약속 추출 파이프라인 디버그 트레이서 (FastAPI 없이 로컬 실행)"
    )
    parser.add_argument("--scenario", default="walk", help="시나리오 별칭 또는 데이터셋 id")
    parser.add_argument("--file", help="사용자 대화 JSON 파일 경로")
    parser.add_argument("--no-think", action="store_true", help="추론과정 끄고 호출")
    parser.add_argument("--full", action="store_true", help="프롬프트/응답 전체 출력(자르지 않음)")
    parser.add_argument("--list", action="store_true", help="시나리오 목록만 출력")
    args = parser.parse_args()

    if args.list:
        print_scenarios()
        return

    if args.file:
        reference_date, messages, label = load_file(args.file)
    else:
        reference_date, messages, label = load_scenario(args.scenario)

    trace(
        reference_date=reference_date,
        messages=messages,
        label=label,
        think=not args.no_think,  # 기본 ON (추론과정 관찰 목적)
        full=args.full,
    )


if __name__ == "__main__":
    main()
