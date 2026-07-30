"""[환경설정 파일]

서버가 켜질 때 필요한 값들을 한곳에 모아둔 곳입니다.
- AI(Gemma)가 어디에 있는지(주소)
- 어떤 AI 모델을 쓸지(이름)
- 비밀번호 같은 열쇠(API 키)
- 얼마나 오래 기다릴지(타임아웃)

이 값들은 `.env` 라는 파일이나 컴퓨터의 환경변수에서 읽어옵니다.
프로그램 여기저기서 `get_settings()` 를 부르면 항상 같은 설정을 꺼내 씁니다.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버가 사용할 설정 값들을 담는 상자입니다."""

    # AI(Gemma)에게 말을 거는 인터넷 주소입니다.
    gemma_base_url: str = "http://mtvs2026.work/v1"

    # 사용할 AI 모델의 이름입니다.
    gemma_model: str = "balanced-q4-k-m-mtp"

    # AI에 들어갈 때 필요한 열쇠(비밀번호)입니다.
    gemma_api_key: str = "replace-with-your-api-key"

    # AI의 답을 몇 초까지 기다릴지 정합니다. (초 단위)
    # 이 시간을 넘으면 "너무 오래 걸린다"며 포기하고 504 오류를 냅니다.
    gemma_timeout_seconds: float = 60.0

    # AI의 "상상력" 정도입니다. 숫자가 낮을수록 늘 비슷하고 안정적으로 답합니다.
    # 약속 찾기는 정확해야 하므로 낮게 둡니다.
    gemma_temperature: float = 0.1

    # AI의 '생각(추론) 과정' 사용 여부입니다.
    # 약속 추출은 단순 작업이라 추론이 필요 없으므로 기본은 끔(False).
    # 끄면 불필요한 생각을 생략해 응답이 빠르고 토큰도 절약됩니다.
    # 필요하면 .env 의 GEMMA_ENABLE_THINKING=true 로 켤 수 있습니다.
    gemma_enable_thinking: bool = False

    # 서비스 이름과 버전 (설명용)
    app_name: str = "dogether-ai-server-m1"
    app_version: str = "0.1.0"

    # 로그 상세도입니다. (DEBUG < INFO < WARNING < ERROR)
    # INFO 면 각 단계 진행 로그가 보이고, WARNING 이면 문제 상황만 보입니다.
    # .env 의 LOG_LEVEL 로 바꿀 수 있습니다.
    log_level: str = "INFO"

    # 설정을 어디서 읽어올지 알려줍니다.
    # env_file=".env" -> ".env" 파일에서 값을 읽습니다.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 모르는 값이 있어도 무시하고 넘어갑니다.
    )


# @lru_cache : 한 번 만든 설정을 기억해 두고 다음부터는 그대로 다시 씁니다.
# (매번 새로 만들지 않아서 더 빠릅니다.)
@lru_cache
def get_settings() -> Settings:
    """설정 상자를 만들어서 돌려줍니다. (한 번만 만들고 계속 재사용)"""

    return Settings()
