# cli_debug_test — 파이프라인 디버그 트레이서

FastAPI·Swagger 를 **띄우지 않고**, 약속 추출 파이프라인을 로컬에서 **한 노드씩**
직접 태워보며 전 과정을 날것으로 보여주는 디버깅 도구입니다.

- "예쁜 결과"가 목적이 아니라, **각 파이프라인 노드를 어떻게 통과하는지 관찰**이 목적입니다.
- LLM 내부 기술은 확정 판별할 수 없으므로, 대신 아래를 보여줍니다.
  1. 우리 **아키텍처/파이프라인 노드** 통과 과정
  2. 모델이 내놓은 **추론과정(reasoning_content)**
  3. API 가 노출하는 **모델/토큰/서빙 단서** (정직하게, "확정 아님" 명시)

> pytest 대상이 아닙니다. (파일명이 `test_` 로 시작하지 않아 자동 수집되지 않음)
> 실제 Gemma 엔드포인트를 호출하므로 `.env` 가 응답해야 합니다.

## 실행 (PowerShell)

프로젝트 루트(`ai-server-m1`)에서:

```powershell
# 내장 시나리오 (walk / outing / hospital / empty / partial / long / big / injection)
.\tests\cli_debug_test\run.ps1 -Scenario outing

# 추론과정 끄고 (운영 기본과 동일하게)
.\tests\cli_debug_test\run.ps1 -Scenario walk -NoThink

# 프롬프트·응답·추론 전체를 자르지 않고 전부 출력
.\tests\cli_debug_test\run.ps1 -Scenario big -Full

# 내 대화 JSON 파일로
.\tests\cli_debug_test\run.ps1 -File .\my_convo.json

# 시나리오 목록만
.\tests\cli_debug_test\run.ps1 -List
```

`run.ps1` 이 venv 파이썬으로 `pipeline_trace.py` 를 실행합니다. (직접 실행도 가능:
`.\.venv\Scripts\python.exe tests\cli_debug_test\pipeline_trace.py --scenario outing`)

## 옵션

| PowerShell | python | 설명 |
| --- | --- | --- |
| `-Scenario <이름>` | `--scenario` | 별칭 또는 골든 데이터셋 id |
| `-File <경로>` | `--file` | 사용자 대화 JSON |
| `-NoThink` | `--no-think` | 추론과정 끄고 호출 (기본은 켬) |
| `-Full` | `--full` | 프롬프트/응답/추론 전체 출력 (기본은 미리보기 길이로 자름) |
| `-List` | `--list` | 시나리오 목록만 출력 (네트워크 불필요) |

## 사용자 대화 JSON 형식

```json
{
  "reference_date": "2026-07-24",
  "messages": [
    {"sender": "초코 보호자", "content": "내일 7시 중앙공원 산책", "sent_at": "2026-07-24T18:00:00+09:00"},
    {"sender": "보리 보호자", "content": "좋아요", "sent_at": "2026-07-24T18:01:00+09:00"}
  ]
}
```

## 보여주는 노드

```
[아키텍처 맵]        노드 ↔ 실제 모듈/함수 매핑
NODE 0 INPUT        요청 대화 + 스키마 검증 통과
NODE 1 대화 정리     format_conversation 입력→출력
NODE 2 프롬프트 생성  SYSTEM_PROMPT 전문 + user 프롬프트 전문
NODE 3 Gemma 호출    요청 파라미터 / 지연 / 추론과정 / content
NODE 4 JSON 파싱     어느 분기(1차 통짜 / 2차 [ ] 또는 { })로 파싱했는지 + 후보 카드
NODE 5 카드 변환     카드별 필드 정규화 전→후, null 로 떨어진 이유
NODE 6 OUTPUT        list[MeetingDraft] (API 응답과 동일)
[모델/기술 단서]     model id / 토큰 / 캐싱 / 추론 노출 / system_fingerprint 등
```

## 참고

- 이 도구는 NODE 3 에서 풍부한 응답 객체(추론·토큰)를 보려고 **OpenAI 클라이언트를
  직접 호출**합니다. 이후 받은 `content` 는 실제 `parse_json` / `to_cards` 에 그대로
  흘려 **운영과 동일 경로**를 검증합니다. 운영 코드(`app/`)는 변경하지 않습니다.
- 추론과정을 보려고 기본적으로 `enable_thinking=True` 로 호출합니다. 운영 서버의
  기본값은 `false` 입니다. ([../../app/config.py](../../app/config.py) 의 `GEMMA_ENABLE_THINKING`)
