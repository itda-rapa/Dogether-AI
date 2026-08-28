"""오픈채팅 최근 대화 기반 경로 의도 추출 API."""

from fastapi import APIRouter, HTTPException, status

from app.logging_config import get_logger
from app.schemas.meeting import ErrorResponse
from app.schemas.route_intent import RouteIntentRequest, RouteIntentResult
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError
from app.services.route_intent_extractor import extract_route_intent

router = APIRouter(prefix="/api/v2/routes", tags=["route-intents"])
logger = get_logger(__name__)


@router.post(
    "/extract",
    response_model=RouteIntentResult,
    summary="최근 오픈채팅에서 새 경로 계획 추출",
    responses={
        502: {"model": ErrorResponse, "description": "AI 호출/응답 변환 실패"},
        504: {"model": ErrorResponse, "description": "AI 응답 시간 초과"},
        422: {"model": ErrorResponse, "description": "요청 형식/메시지 개수 오류"},
    },
)
def extract(request: RouteIntentRequest) -> RouteIntentResult:
    logger.info(
        "[경로의도 요청] room=%s messages=%d",
        request.room_id,
        len(request.messages),
    )
    try:
        return extract_route_intent(request.messages)
    except GemmaTimeoutError as exc:
        logger.warning("[경로의도 실패] AI 응답 시간 초과: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI 응답 시간이 초과됐어요.",
        ) from exc
    except (GemmaError, ExtractionError) as exc:
        logger.warning("[경로의도 실패] AI 호출/응답 변환 실패: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="대화에서 경로 계획을 분석하지 못했어요.",
        ) from exc
