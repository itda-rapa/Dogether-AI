"""[약속 카드 초안 추출 API - 단체 채팅방(v2) 창구]

v1(1:1) 창구는 그대로 두고, 단체 채팅방용 창구를 옆에 새로 만듭니다.
어느 창구로 보낼지는 백엔드가 방 인원으로 정합니다. (2명이면 v1, 3명 이상이면 v2)

- 400 : 명부가 3명 미만이거나 명부 자체가 잘못됨 (v2 로 올 요청이 아님)
- 502 : AI 호출 실패 / 답을 카드로 못 바꿈
- 504 : AI 응답 시간 초과
- 422 : 요청 형식이 잘못됨 (메시지 2개 미만/200개 초과 등, FastAPI 가 자동 처리)
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.meeting import ErrorResponse
from app.schemas.meeting_v2 import MeetingDraftV2, MeetingDraftV2Request
from app.services.gemma_client import GemmaError, GemmaTimeoutError
from app.services.meeting_extractor import ExtractionError
from app.services.meeting_extractor_v2 import extract_meeting_drafts_v2
from app.services.participant_mapper import ParticipantMappingError

# 실제 주소는 "/api/v2/meeting-drafts/extract" 가 됩니다.
router = APIRouter(prefix="/api/v2/meeting-drafts", tags=["meeting-drafts-v2"])


@router.post(
    "/extract",
    response_model=list[MeetingDraftV2],
    summary="단체 채팅방 대화에서 약속 카드 초안 뽑기",
    responses={
        400: {"model": ErrorResponse, "description": "명부 오류 (3명 미만 등)"},
        502: {"model": ErrorResponse, "description": "AI 호출 실패 / 응답 변환 실패"},
        504: {"model": ErrorResponse, "description": "AI 응답 시간 초과"},
        422: {"model": ErrorResponse, "description": "요청 형식 오류 (메시지 개수 등)"},
    },
)
def extract(request: MeetingDraftV2Request) -> list[MeetingDraftV2]:
    """단체 채팅방 대화를 받아서 약속 카드 초안(들)을 만들어 돌려줍니다.

    카드마다 그 약속에 속한 사람들의 ID(participant_ids)가 함께 담깁니다.
    백엔드는 여기 담긴 사람에게만 알림을 보내면 됩니다.
    """

    try:
        drafts = extract_meeting_drafts_v2(
            participants=request.participants,
            messages=request.messages,
            reference_date=request.reference_date,
        )
    except ParticipantMappingError as exc:
        # 명부가 규칙에 안 맞음 (3명 미만 / 빈 ID / ID 중복) -> 400
        # 보낸 쪽이 고쳐야 하는 문제라 AI 를 부르기 전에 여기서 끝냅니다.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"참여자 명부가 올바르지 않아요: {exc}",
        ) from exc
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

    return drafts
