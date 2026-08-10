"""[서버의 시작점]

이 파일이 서버의 '대문' 입니다.
서버를 켜면 여기서부터 시작해서, 약속을 찾아주는 기능(라우터)을 연결합니다.
"""

from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import configure_logging
from app.routes import meeting, meeting_v2


def create_app() -> FastAPI:
    """서버(FastAPI 앱)를 만들어서 돌려줍니다."""

    # 로그 설정을 가장 먼저 켭니다. (이후 남기는 로그가 서식에 맞춰 보이도록)
    configure_logging()

    # 설정 값(주소, 모델 이름 등)을 불러옵니다.
    settings = get_settings()

    # FastAPI 로 서버를 하나 만듭니다. (title/version 은 문서에 보이는 이름표)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "대화에서 약속 카드 초안을 뽑아내는 AI 서버 "
            "(v1: 1:1 대화 · v2: 단체 채팅방)"
        ),
    )

    # 약속 찾기 기능(주소들)을 서버에 붙입니다.
    # v1 = 1:1 대화용, v2 = 단체 채팅방(3명 이상)용. 어느 쪽을 부를지는 백엔드가 정합니다.
    app.include_router(meeting.router)
    app.include_router(meeting_v2.router)

    # 서버가 잘 살아있는지 확인하는 간단한 주소입니다.
    # 브라우저에서 /health 로 들어가면 "ok" 라고 답해 줍니다.
    @app.get("/health", tags=["system"])
    def health() -> dict:
        """서버가 살아있는지 확인하는 곳."""

        return {"status": "ok", "version": settings.app_version}

    return app


# 서버를 실제로 하나 만들어 둡니다. (uvicorn 이 이 app 을 찾아서 실행합니다.)
app = create_app()
