# ai-server-m1 — 약속 카드 초안 추출 서버 (M1)

같이놀개(dogether) 프로젝트의 AI 서버 M1 기능입니다.
대화(채팅) 내용을 입력받아 **약속 카드 초안**(종류·날짜·시각·장소)을 추출합니다.
OpenAI 호환 방식으로 Gemma 모델을 호출합니다.

> AI는 약속을 **직접 확정하거나 저장하지 않습니다.** 사용자가 확인·수정할
> 카드 초안만 만들어 돌려줍니다.

## M1 범위

**포함**
- Spring Boot가 전달한 최근 대화 수신 (최소 2개 ~ 최대 200개)
- 입력 형식 검증 (개수, 날짜 형식 등)
- 대화 → Gemma 프롬프트 변환 및 호출
- 약속 **종류·날짜·시각·장소** 추출
- 상대 날짜("내일", "모레") → 기준일 기반 실제 날짜 변환
- Gemma 응답에서 JSON 추출 및 카드 형식 검증
- 연결 실패 / 타임아웃 / 형식 오류 처리

**제외**
- 프론트엔드 버튼, Spring Boot의 대화 조회, 카드 저장/수정/확정, 알림
- M2 스케줄링, M3 실시간 검열, 대화 원문 저장

## 요구 사항

- Python 3.10+
- OpenAI 호환 Gemma 엔드포인트

## 설치

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt        # 실행용
pip install -r requirements-dev.txt    # 테스트 포함
```

## 환경설정

`.env.example` 을 `.env` 로 복사한 뒤 값을 채웁니다. (실제 API 키는 `.env` 에만)

```bash
cp .env.example .env
```

| 변수 | 설명 | 예시 |
| --- | --- | --- |
| `GEMMA_BASE_URL` | OpenAI 호환 엔드포인트 | `http://mtvs2026.work/v1` |
| `GEMMA_MODEL` | 모델 이름 | `balanced-q4-k-m-mtp` |
| `GEMMA_API_KEY` | API 키 | `replace-with-your-api-key` |
| `GEMMA_TIMEOUT_SECONDS` | 호출 타임아웃(초) | `60` |
| `GEMMA_TEMPERATURE` | 생성 온도 | `0.1` |
| `GEMMA_ENABLE_THINKING` | 추론(생각) 과정 사용 여부 | `false` |
| `LOG_LEVEL` | 로그 상세도 (`DEBUG`/`INFO`/`WARNING`/`ERROR`) | `INFO` |

## 실행

```bash
uvicorn app.main:app --reload --port 8000
```

- 헬스 체크: `GET http://localhost:8000/health`
- API 문서(Swagger): `http://localhost:8000/docs`

## 로그

추출 과정(대화 정리 → 프롬프트 생성 → Gemma 호출 → JSON 파싱 → 카드 변환)의
각 단계가 서버 터미널에 로그로 찍힙니다. 어느 단계에서 멈췄는지로 문제 위치를
바로 좁힐 수 있습니다.

```
2026-07-24 18:00:00 [INFO] app.services.meeting_extractor: [추출 3/5] Gemma 호출 시작
2026-07-24 18:00:00 [INFO] app.services.meeting_extractor: [추출 5/5] 카드 완성 — {...}
```

- 기본 레벨은 `INFO`(단계 흐름). 프롬프트/응답 **원문**까지 보려면 `.env` 에서
  `LOG_LEVEL=DEBUG` 로 바꾸세요.
- 문제 상황만 보고 싶으면 `LOG_LEVEL=WARNING`.

## API

### 공통 헤더 — `X-Request-Source` (선택)

약속 카드 창구(v1·v2)는 요청 출처를 헤더로 받습니다. **로그에만 쓰이며 추출
결과에는 전혀 영향을 주지 않습니다.** 값은 `manual`(사용자가 화면에서 직접
누름) 또는 `scheduler`(배치가 자동으로 돎)이고, 없거나 모르는 값이면
`unknown` 으로 기록됩니다. **모르는 값 때문에 요청이 거절되지는 않습니다.**

```
X-Request-Source: scheduler
```

이 값과 `room_id` 는 요청·성공·실패 로그에 함께 남습니다.

```
[v1 요청] room=room-1 source=scheduler — 메시지 30개
[v1 완료] room=room-1 source=scheduler — 카드 1개
[v1 실패] room=room-7 source=scheduler — AI 응답 시간 초과 (504)
```

**배치에는 이 로그가 사실상 필수입니다.** 응답은 카드 배열뿐이라 `room_id` 가
되돌아가지 않아서, 방을 하나씩 도는 도중 어디서 실패했는지 알 방법이 로그밖에
없습니다. 수동 호출은 사용자가 그 방을 보고 있지만 배치는 보는 사람이 없습니다.

> 출처를 요청 **본문**이 아니라 헤더로 받는 이유는, 본문에 넣으면 v1/v2 요청
> 규격이 바뀌어 기존 문서와 회귀 테스트가 함께 움직이기 때문입니다.
> 그리고 **판단 로직은 이 값을 보지 않습니다** — 같은 대화면 누가 불렀든 같은
> 카드가 나와야 "수동으로는 되는데 배치로는 안 된다" 가 생기지 않습니다.

### `POST /api/v1/meeting-drafts/extract`

**요청**

```json
{
  "room_id": "room-1",
  "reference_date": "2026-07-24",
  "messages": [
    {
      "sender": "초코 보호자",
      "content": "내일 저녁 7시에 중앙공원에서 산책할까요?",
      "sent_at": "2026-07-24T18:00:00+09:00"
    },
    {
      "sender": "보리 보호자",
      "content": "좋아요. 내일 봬요!",
      "sent_at": "2026-07-24T18:01:00+09:00"
    }
  ]
}
```

- `room_id` (필수): 대화방 ID
- `reference_date` (필수): 상대 날짜 해석 기준일(`YYYY-MM-DD`)
- `messages` (필수): 시간 순 대화 **2~200개**. 각 항목은 `sender`, `content`, `sent_at`

**성공 응답 `200`** — 응답은 **카드 목록(배열)** 입니다.

```json
[
  {
    "meeting_type": "WALK",
    "date": "2026-07-25",
    "time": "19:00",
    "place": "중앙공원"
  }
]
```

- 응답은 항상 **카드 배열**입니다. 보통 카드 1개.
- `meeting_type` 은 코드값: `WALK`(산책) / `PLAY`(나들이) / `HOSPITAL`(병원 동행) / `OTHER`(그 외) 중 하나이거나 `null`
- `null` 이면 프론트에서 사용자가 종류를 직접 고릅니다
- 대화에서 확인되지 않는 항목은 추측하지 않고 `null`
- `date` 는 `YYYY-MM-DD`, `time` 은 `HH:MM`(24시간제)이며, **형식이 조금이라도
  어긋나면 그 문자열을 내리지 않고 `null`** 로 둡니다. 백엔드가 `LocalDate.parse` /
  `LocalTime.parse` 를 바로 걸어도 됩니다 (타임존은 붙지 않으므로 KST 로컬로 해석)
- **`date` 가 `reference_date` 보다 이전이면 `null`** 입니다. 지나간 날짜의 약속은
  카드가 될 수 없기 때문입니다. 기준일 **당일은 남습니다** — 오늘 저녁 약속이 오늘
  아침 배치에서 사라지면 안 되니까요. 날짜밖에 없던 과거 카드는 네 항목이 모두
  `null` 이 되어 목록에서 아예 빠집니다
- **나들이에서 서로 다른 장소가 여러 곳** 확인되면, **장소마다 카드가 하나씩** 담겨 여러 개가 나옵니다. (산책·병원 동행은 대표 장소 1개로 카드 1개)

**나들이 여러 장소 응답 `200`** (예)

```json
[
  { "meeting_type": "PLAY", "date": "2026-07-26", "time": "10:00", "place": "서울숲" },
  { "meeting_type": "PLAY", "date": "2026-07-26", "time": "10:00", "place": "한강공원" }
]
```

**빈 목록 응답 `200`** (약속이 없거나 모두 불명확할 때)

```json
[]
```

**오류**

| 코드 | 상황 |
| --- | --- |
| `422` | 메시지 2개 미만/200개 초과, 날짜 형식 오류 등 요청 형식 문제 |
| `502` | Gemma 연결/인증 실패, 또는 응답을 카드 형식으로 변환 실패 |
| `504` | Gemma 응답 시간 초과 |

> `502`/`504` 발생 시 빈 목록(`[]`)을 대신 제공하는 처리는 Spring Boot가 담당합니다.

### 예시 (curl)

```bash
curl -X POST http://localhost:8000/api/v1/meeting-drafts/extract \
  -H "Content-Type: application/json" \
  -d '{
    "room_id": "room-1",
    "reference_date": "2026-07-24",
    "messages": [
      {"sender": "초코 보호자", "content": "내일 저녁 7시에 중앙공원에서 산책할까요?", "sent_at": "2026-07-24T18:00:00+09:00"},
      {"sender": "보리 보호자", "content": "좋아요. 내일 봬요!", "sent_at": "2026-07-24T18:01:00+09:00"}
    ]
  }'
```

### `POST /api/v2/meeting-drafts/extract` (단체 채팅방)

**3명 이상**이 있는 방을 위한 창구입니다. v1 과 하는 일은 같고, 카드마다
**그 약속에 속한 사람들**이 함께 나옵니다. 백엔드는 여기 담긴 사람에게만
알림을 보내면 됩니다.

> 어느 창구로 보낼지는 **백엔드가 방 인원으로 정합니다.** 2명이면 v1,
> 3명 이상이면 v2 입니다. 명부가 3명 미만인 v2 요청은 `400` 으로 거절됩니다.

**요청** — v1 과 달라지는 곳은 두 군데입니다. `participants`(명부)가 추가되고,
발신자를 이름(`sender`)이 아니라 ID(`sender_id`)로 보냅니다.

```json
{
  "room_id": "room-1",
  "reference_date": "2026-07-24",
  "participants": ["u-101", "u-102", "u-103", "u-104"],
  "messages": [
    {"sender_id": "u-101", "content": "토요일 3시 중앙공원 어때요?", "sent_at": "2026-07-24T18:00:00+09:00"},
    {"sender_id": "u-102", "content": "좋아요 갈게요", "sent_at": "2026-07-24T18:01:00+09:00"},
    {"sender_id": "u-103", "content": "저는 그날 병원이라 어려워요", "sent_at": "2026-07-24T18:02:00+09:00"}
  ]
}
```

- `participants` (필수): 방에 있는 사람들의 사용자 ID 목록. **3명 이상**, 중복 불가
- `messages` (필수): 시간 순 대화 **2~200개**. 각 항목은 `sender_id`, `content`, `sent_at`
- 명부에 없는 `sender_id` 의 메시지는 **버려집니다** (누구인지 되돌릴 수 없으므로)

**성공 응답 `200`**

```json
[
  {
    "meeting_type": "WALK",
    "date": "2026-07-25",
    "time": "15:00",
    "place": "중앙공원",
    "participant_ids": ["u-101", "u-102"]
  }
]
```

- `participant_ids` = **제안한 사람 + 동의한 사람**
- **침묵은 동의가 아닙니다.** 아무 말 없던 `u-104` 와 거절한 `u-103` 은 빠집니다
- 약속이 성립하려면 **제안자 + 최소 1명 = 2명**이 필요합니다. 방 인원과 무관하게 고정이며,
  이 조건에 못 미치는 카드는 응답에 담기지 않습니다
- 약속이 여러 건이면 **카드마다 참여자를 따로** 판단합니다

**오류** — v1 과 같고 `400` 이 하나 더 있습니다.

| 코드 | 상황 |
| --- | --- |
| `400` | 명부가 3명 미만이거나(→ v1 을 쓰세요), 명부에 빈 ID·중복 ID 가 있음 |
| `422` | 메시지 2개 미만/200개 초과, 날짜 형식 오류 등 요청 형식 문제 |
| `502` | Gemma 연결/인증 실패, 또는 응답을 카드 형식으로 변환 실패 |
| `504` | Gemma 응답 시간 초과 |

**AI 에게 실명은 가지 않습니다.** 서버가 명부 순서대로 `P1`, `P2` … 이름표를
붙여 보내고, 답을 받으면 다시 원래 ID 로 되돌립니다
(`app/services/participant_mapper.py`). 덕분에 같은 이름을 쓰는 두 사람이
구분되고, 메시지 안에 줄바꿈을 넣어 남의 발언을 지어내는 위조도 막힙니다.

### `POST /api/v1/place-intent/decide` (지도 팝업 판단)

"병원을 찾으시나요?" **팝업을 띄울지만** 정하는 창구입니다. 지도 기능(M2 B)에서
AI 서버가 등장하는 곳은 여기 하나뿐이고, 좌표로 실제 병원을 찾는 일은 백엔드가
지도 API 로 직접 합니다.

> **좌표는 이 서버에 보내지 마세요.** Gemma 는 외부 주소에 있어서, 좌표가 여기로
> 오면 그대로 외부로 나가는 경로가 됩니다. 위치는 사용자가 팝업을 누른 뒤에
> 프론트와 백엔드 사이에서만 오갑니다. (`M2_FLOW.md` B-5)

**요청** — 키워드가 들어있는 메시지가 **맨 뒤**에 와야 합니다.

```json
{
  "room_id": "room-1",
  "participants": ["u-101", "u-102", "u-103", "u-104"],
  "trigger_sender_id": "u-103",
  "messages": [
    {"sender_id": "u-101", "content": "강아지가 밤새 설사를 했어요", "sent_at": "2026-07-24T18:00:00+09:00"},
    {"sender_id": "u-102", "content": "괜찮으려나요", "sent_at": "2026-07-24T18:01:00+09:00"},
    {"sender_id": "u-103", "content": "근처에 지금 문 연 동물병원 어디 있어요?", "sent_at": "2026-07-24T18:02:00+09:00"}
  ]
}
```

- `participants` (필수): 방 명부. **2명 이상** — 1:1 방에서도 팝업은 떠야 하므로
  약속 추출 v2 의 3명과 기준이 다릅니다
- `trigger_sender_id` (필수): 키워드를 말한 사람. 팝업은 **이 사람에게만** 뜹니다.
  방 전체에 띄우면 나머지는 자기가 꺼내지도 않은 팝업을 보게 됩니다
- `messages` (필수): **1~3개**. 키워드 메시지 + 바로 앞 2줄이며, **마지막 항목이
  키워드 메시지**여야 합니다. "최근 3줄"로 자르면 여러 명이 동시에 떠드는 방에서
  정작 키워드 메시지가 빠질 수 있습니다

**성공 응답 `200`**

```json
{
  "decision": "SHOW",
  "place_type": "HOSPITAL",
  "target_user_id": "u-103"
}
```

- `decision`: `"SHOW"` 일 때만 팝업을 띄웁니다. 위치 권한은 그때 처음 묻습니다
- `place_type`: `"HOSPITAL"` / `"PHARMACY"`, `SUPPRESS` 면 `null`
- `target_user_id`: 팝업을 띄울 사람 (= `trigger_sender_id`)

**판단 기준** — 지금 갈 곳을 찾고 있어야 `SHOW` 입니다. 다녀온 이야기
("어제 병원 다녀왔어요"), 상태 서술("우리 개가 병원을 무서워해요"), 이미 예약된
경우는 모두 `SUPPRESS` 입니다. **애매하면 `SUPPRESS`** 로 갑니다 — 필요 없는
팝업이 한 번 뜨면 사용자가 기능을 꺼버립니다.

**오류**

| 코드 | 상황 |
| --- | --- |
| `400` | 명부가 2명 미만·빈 ID·중복 ID / `trigger_sender_id` 가 명부에 없거나 마지막 메시지의 발신자가 아님 |
| `422` | 메시지 0개/4개 이상, `sender_id` 누락 등 요청 형식 문제 |
| `502` | Gemma 연결/인증 실패 |
| `504` | Gemma 응답 시간 초과 |

**`200` 이 아니면 팝업을 띄우지 마세요.** 판단이 안 된 것을 "띄워도 된다"로
읽으면 오탐이 생깁니다. 반대로 모델이 알아들을 수 없는 답을 준 경우는 오류가
아니라 `200` + `SUPPRESS` 로 옵니다 (판단은 끝났고 결과가 '띄우지 않음').

**쿨다운은 서버가 아니라 앱이 기억합니다.** 사용자가 팝업을 한 번 닫으면 그
방에서 한동안 다시 묻지 않습니다. 기억의 단위는 **(방, 사용자)** 이고, 판정은
프론트의 키워드 단계 **맨 앞**에 두어야 합니다. 뒤에 두면 이미 모델을 부른
뒤라 비용이 발생합니다. (`M2_FLOW.md` B-6)

## 테스트

```bash
pytest
```

테스트는 실제 Gemma 호출 없이 목(mock)으로 대체하므로 모델 서버가 없어도 실행됩니다.

### 정확도 회귀 테스트 (선택)

메시지 상한 상향(200) 이후에도 추출 품질이 유지되는지 확인하는 테스트입니다.
라벨링된 골든 데이터셋(`tests/accuracy_dataset.py`)을 **실제 Gemma** 로 추출해
필드 단위 정확도를 측정합니다. 네트워크/모델이 필요하므로 **기본 `pytest` 에서는
skip** 되고, 아래처럼 켤 때만 실행됩니다.

```bash
# .env 의 Gemma 엔드포인트가 실제로 응답해야 합니다.
RUN_ACCURACY_TESTS=1 pytest tests/test_extraction_accuracy.py -s

# 통과 기준(필드 정확도)을 조절하려면:
RUN_ACCURACY_TESTS=1 ACCURACY_THRESHOLD=0.9 pytest tests/test_extraction_accuracy.py -s
```

- 데이터셋 유효성 검사(스키마 적합·종류 커버리지·긴 대화 케이스 존재)는
  모델 없이 항상 실행되어, 골든 데이터가 상한/형식 규칙을 벗어나면 바로 잡아냅니다.
- 정확도가 기준(`ACCURACY_THRESHOLD`, 기본 0.85) 아래로 떨어지면 실패하고,
  어떤 케이스의 어떤 필드가 틀렸는지 리포트로 출력합니다.

## 프로젝트 구조

```
app/
  main.py                           # FastAPI 진입점
  config.py                         # 환경설정
  routes/meeting.py                 # v1 API — 1:1 (요청 접수 + 오류 응답)
  routes/meeting_v2.py              # v2 API — 단체 채팅방
  routes/place_intent.py            # 지도 팝업 판단 API
  routes/request_log.py             # 로그 꼬리표 (room_id + 요청 출처 헤더)
  schemas/meeting.py                # v1 입력/출력 규격
  schemas/meeting_v2.py             # v2 규격 (명부 + 카드별 참여자)
  schemas/place_intent.py           # 팝업 판단 규격 (좌표 없음)
  services/gemma_client.py          # Gemma(OpenAI 호환) 연결
  services/meeting_extractor.py     # 대화 변환 → 호출 → JSON 추출 → 카드 검증
  services/meeting_extractor_v2.py  # 위 흐름 + 이름표 붙이기/떼기 + 정족수
  services/place_intent_detector.py # 이름표 → 호출 → SHOW/SUPPRESS 해석
  services/participant_mapper.py    # 사용자 ID ↔ 이름표(P1…) 변환 (약속·지도 공용)
  prompts/meeting_prompt.py         # v1 AI 지시문
  prompts/meeting_prompt_v2.py      # v2 AI 지시문 (동의 규칙 포함)
  prompts/place_intent_prompt.py    # 팝업 판단 지시문 (단어 하나만 출력)
tests/                              # API / 추출 / 이름표 / 팝업 판단 테스트
```

v1 파일은 v2 를 붙이면서 **하나도 고치지 않았습니다.** 기존 회귀 테스트가
그대로 유효합니다. 지도 팝업 판단을 붙이면서 `participant_mapper` 에
`min_size` 인자 하나만 추가했습니다 — 이름표를 붙이는 방법은 같고 최소 인원
기준만 기능마다 다르기 때문입니다 (약속 3명 · 지도 2명).
