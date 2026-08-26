"""[위험 신호 판단 API 창구 (M3_FLOW 5-3 ②)]

규칙 필터에 걸린 메시지를 맥락과 함께 보고, 사람이 검토할 만한지 답하는 창구입니다.
범죄 탐지에서 AI 서버가 등장하는 곳은 여기 하나뿐입니다.

    ① 규칙 필터        분석 Consumer (백엔드)
    ② AI 맥락 판단     ← 여기
    ③ 위험 점수 누적   백엔드 (여러 방·여러 상대를 합산하므로 여기서 못 함)
    ④ 운영자 검토 큐   백엔드 + 관리자 대시보드 (5-7)

M3_FLOW 5-1 의 1번(거절 후 반복 접촉)은 차단 이력과 접근 횟수 조회로 끝나서
이 창구를 쓰지 않습니다. (5-2) 여기서 다루는 것은 2·3·4 번입니다.

- 400 : 명부가 2명 미만 / 판단 대상 지정이 대화와 안 맞음
- 502 : AI 호출 실패
- 504 : AI 응답 시간 초과
- 422 : 요청 형식이 잘못됨 (메시지 0개/11개 이상 등, FastAPI 가 자동 처리)

받는 쪽 안내 (두 가지 모두 지켜야 합니다):

1. **200 이 아닌 답이 오면 검토 큐에 올리지 마세요.** 판단이 안 된 것을
   "위험하다" 로 읽으면 오탐이 생깁니다. AI 서버가 잠깐 죽었다고 해서 그 시간대
   메시지가 전부 검토 큐로 가면 5-5 의 '하루 200건' 이 그대로 재현됩니다.

2. **FLAG 하나로 사용자에게 아무 일도 일어나면 안 됩니다.** (5-6 원칙 1)
   FLAG 는 점수 누적의 입력일 뿐이고, 임계값을 넘으면 사람이 봅니다.
   경고·정지는 사람이 누릅니다.
"""

from fastapi import APIRouter, HTTPException, status

from app.logging_config import get_logger
from app.schemas.meeting import ErrorResponse
from app.schemas.risk_signal import RiskSignalRequest, RiskSignalVerdict
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.participant_mapper import ParticipantMappingError
from app.services.risk_signal_detector import RiskSignalError, analyze_risk_signal

logger = get_logger(__name__)

# 실제 주소는 "/api/v1/risk-signals/analyze" 가 됩니다.
router = APIRouter(prefix="/api/v1/risk-signals", tags=["risk-signals"])


@router.post(
    "/analyze",
    response_model=RiskSignalVerdict,
    summary="규칙에 걸린 메시지를 맥락과 함께 판단하기",
    responses={
        400: {"model": ErrorResponse, "description": "명부 오류 / 대상 지정 오류"},
        502: {"model": ErrorResponse, "description": "AI 호출 실패"},
        504: {"model": ErrorResponse, "description": "AI 응답 시간 초과"},
        422: {"model": ErrorResponse, "description": "요청 형식 오류 (메시지 개수 등)"},
    },
)
def analyze(request: RiskSignalRequest) -> RiskSignalVerdict:
    """규칙 필터에 걸린 메시지가 정말 검토할 만한지 판단합니다.

    돌려주는 것은 유형과 위험도뿐입니다. 판정 근거 문장은 돌려주지 않습니다.
    근거에 대화 원문이 실려 나가면 그것을 저장하는 순간 5-6 원칙 2 가 깨집니다.
    """

    try:
        return analyze_risk_signal(
            participants=request.participants,
            messages=request.messages,
            suspect_sender_id=request.suspect_sender_id,
        )
    except (ParticipantMappingError, RiskSignalError) as exc:
        # 보낸 쪽이 고쳐야 하는 문제라 AI 를 부르기 전에 여기서 끝냅니다.
        # 이 메시지는 우리 코드가 직접 쓴 글이라 그대로 돌려줘도 안전합니다.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"요청이 올바르지 않아요: {exc}",
        ) from exc
    except GemmaTimeoutError as exc:
        # (GemmaError 의 자식이라 반드시 GemmaError 보다 먼저 잡아야 합니다.)
        logger.warning("[위험판단] Gemma 응답 시간 초과: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI 응답 시간이 초과됐어요.",
        ) from exc
    except GemmaError as exc:
        # 오류 원문은 로그에만 남깁니다. 연결 실패 메시지에는 모델 주소 같은
        # 서버 내부 정보가 섞여 있어서 그대로 밖으로 내보내지 않습니다.
        logger.warning("[위험판단] Gemma 호출 실패: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 모델을 부르는 데 실패했어요.",
        ) from exc
