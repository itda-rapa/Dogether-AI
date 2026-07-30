
"""[로그 설정]

서버 곳곳에서 남기는 로그(진행 상황·경고·오류)를 '어떻게 보여줄지' 한곳에서 정합니다.

왜 print 대신 logging 을 쓰나요?
- print 는 항상 무조건 찍히지만, logging 은 '레벨'로 켜고 끌 수 있습니다.
  (예: 평소엔 INFO 만 보고, 문제를 파헤칠 땐 DEBUG 까지 켜기)
- 시각·레벨·어느 모듈에서 났는지가 자동으로 붙습니다.
- 나중에 파일 저장·외부 수집(모니터링)으로 확장하기 쉽습니다.

구조:
- "app" 이라는 이름의 로거 하나에 출력 담당(handler)과 서식(formatter)을 답니다.
- app.services.meeting_extractor 처럼 "app.~" 로 시작하는 하위 로거들은
  이 "app" 로거의 설정을 그대로 물려받습니다. (각자 설정할 필요 없음)
- propagate=False 로 두어, uvicorn 의 기본 로거와 겹쳐 두 번 찍히는 것을 막습니다.
"""

import logging

from app.config import get_settings

# 로거 이름의 '뿌리'. 모든 앱 로거는 이 이름 아래에 둡니다.
APP_LOGGER_NAME = "app"

# 설정이 이미 끝났는지 표시. (여러 번 불러도 handler 가 중복으로 붙지 않게)
_configured = False


def configure_logging() -> None:
    """앱 로거에 출력 담당과 서식을 붙입니다. (서버 시작 시 한 번 호출)"""

    global _configured
    if _configured:
        return

    settings = get_settings()

    logger = logging.getLogger(APP_LOGGER_NAME)
    # 문자열 레벨("INFO")을 실제 레벨 값으로 바꿔서 설정합니다.
    logger.setLevel(settings.log_level.upper())

    # 아직 출력 담당이 없다면 하나 만들어 붙입니다. (터미널로 출력)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                # 예) 2026-07-24 18:00:00 [INFO] app.services.meeting_extractor: ...
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    # uvicorn 의 최상위(root) 로거로 올려보내지 않습니다. (중복 출력 방지)
    logger.propagate = False

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """모듈용 로거를 돌려줍니다. 보통 get_logger(__name__) 으로 씁니다."""

    return logging.getLogger(name)
