"""[약속 카드 초안 추출 API - 손님을 맞는 창구]

밖에서 들어오는 요청을 받아서 약속 카드 초안을 만들어 돌려주는 창구입니다.
문제가 생기면 알맞은 오류 번호로 안내해 줍니다.
- 502 : AI 호출 실패 / 답을 카드로 못 바꿈
- 504 : AI 응답 시간 초과
- 422 : 요청 형식이 잘못됨 (메시지 2개 미만/200개 초과 등, FastAPI 가 자동 처리)
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.meeting import ErrorResponse, MeetingDraft, MeetingDraftRequest
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError, extract_meeting_drafts

# 이 창구들의 공통 주소 앞부분을 정합니다.
# 그래서 실제 주소는 "/api/v1/meeting-drafts/extract" 가 됩니다.
router = APIRouter(prefix="/api/v1/meeting-drafts", tags=["meeting-drafts"])


@router.post(
    "/extract",
    response_model=list[MeetingDraft],
    summary="대화에서 약속 카드 초안 뽑기",
    responses={
        502: {"model": ErrorResponse, "description": "AI 호출 실패 / 응답 변환 실패"},
        504: {"model": ErrorResponse, "description": "AI 응답 시간 초과"},
        422: {"model": ErrorResponse, "description": "요청 형식 오류 (메시지 개수 등)"},
    },
)
def extract(request: MeetingDraftRequest) -> list[MeetingDraft]:
    """대화를 받아서 약속 카드 초안(들)을 만들어 돌려줍니다.

    보통 0개(약속 없음) 또는 1개이며, '나들이'에 장소가 여러 곳이면
    장소마다 카드가 하나씩 담긴 목록이 나옵니다.
    """

    # try/except : 중간에 문제가 생겨도 서버가 죽지 않고 안내하도록 감쌉니다.
    try:
        drafts = extract_meeting_drafts(
            messages=request.messages,
            reference_date=request.reference_date,
        )
    except GemmaTimeoutError as exc:
        # AI 응답 시간 초과 -> 504
        # (GemmaError 의 자식이라 반드시 GemmaError 보다 먼저 잡아야 합니다.)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"AI 응답 시간이 초과됐어요: {exc}",
        ) from exc
    except GemmaError as exc:
        # AI 쪽 문제(연결 안 됨, 인증 실패 등) -> 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI 모델을 부르는 데 실패했어요: {exc}",
        ) from exc
    except ExtractionError as exc:
        # AI 답을 카드 형식으로 바꾸지 못한 경우 -> 502 (응답 변환 실패)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI 응답을 카드로 바꾸지 못했어요: {exc}",
        ) from exc

    # 잘 만들어진 카드 초안 목록을 응답으로 돌려줍니다.
    return drafts
