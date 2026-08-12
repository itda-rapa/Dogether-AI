"""[지도 팝업 판단 API 창구 (M2_FLOW B-2)]

"병원을 찾으시나요?" 팝업을 띄울지 정하는 창구입니다.
지도 기능에서 AI 서버가 등장하는 곳은 여기 하나뿐입니다. 좌표로 실제 장소를
찾는 2단계는 백엔드가 지도 API 로 직접 하며, 이 서버를 거치지 않습니다. (B-5)

주소가 /api/v1 인 이유: 이 창구는 이번에 처음 생겼습니다.
약속 추출의 v1/v2 는 '1:1 이냐 단톡방이냐' 를 가르는 그 창구의 판(version)이지
M1/M2 라는 마일스톤 번호가 아닙니다.

- 400 : 명부가 2명 미만 / 키워드를 말한 사람 지정이 대화와 안 맞음
- 502 : AI 호출 실패
- 504 : AI 응답 시간 초과
- 422 : 요청 형식이 잘못됨 (메시지 0개/4개 이상 등, FastAPI 가 자동 처리)

받는 쪽 안내: 200 이 아닌 답이 오면 **팝업을 띄우지 마세요.**
판단이 안 된 것을 "띄워도 된다" 로 읽으면 오탐이 생깁니다.
"""

from fastapi import APIRouter, HTTPException, status

from app.logging_config import get_logger
from app.schemas.meeting import ErrorResponse
from app.schemas.place_intent import PlaceIntentDecision, PlaceIntentRequest
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.participant_mapper import ParticipantMappingError
from app.services.place_intent_detector import PlaceIntentError, decide_place_intent

logger = get_logger(__name__)

# 실제 주소는 "/api/v1/place-intent/decide" 가 됩니다.
router = APIRouter(prefix="/api/v1/place-intent", tags=["place-intent"])


@router.post(
    "/decide",
    response_model=PlaceIntentDecision,
    summary="병원/약국 팝업을 띄울지 판단하기",
    responses={
        400: {"model": ErrorResponse, "description": "명부 오류 / 대상 지정 오류"},
        502: {"model": ErrorResponse, "description": "AI 호출 실패"},
        504: {"model": ErrorResponse, "description": "AI 응답 시간 초과"},
        422: {"model": ErrorResponse, "description": "요청 형식 오류 (메시지 개수 등)"},
    },
)
def decide(request: PlaceIntentRequest) -> PlaceIntentDecision:
    """대화 몇 줄을 보고 "병원을 찾으시나요?" 팝업을 띄울지 정합니다.

    좌표는 받지 않습니다. 위치 권한은 사용자가 팝업을 누른 뒤에 프론트가 묻습니다.
    """

    try:
        return decide_place_intent(
            participants=request.participants,
            messages=request.messages,
            trigger_sender_id=request.trigger_sender_id,
        )
    except (ParticipantMappingError, PlaceIntentError) as exc:
        # 보낸 쪽이 고쳐야 하는 문제라 AI 를 부르기 전에 여기서 끝냅니다.
        # 이 메시지는 우리 코드가 직접 쓴 글이라 그대로 돌려줘도 안전합니다.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"요청이 올바르지 않아요: {exc}",
        ) from exc
    except GemmaTimeoutError as exc:
        # (GemmaError 의 자식이라 반드시 GemmaError 보다 먼저 잡아야 합니다.)
        logger.warning("[지도판단] Gemma 응답 시간 초과: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI 응답 시간이 초과됐어요.",
        ) from exc
    except GemmaError as exc:
        # 오류 원문은 로그에만 남깁니다. 연결 실패 메시지에는 모델 주소 같은
        # 서버 내부 정보가 섞여 있어서 그대로 밖으로 내보내지 않습니다.
        logger.warning("[지도판단] Gemma 호출 실패: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 모델을 부르는 데 실패했어요.",
        ) from exc
