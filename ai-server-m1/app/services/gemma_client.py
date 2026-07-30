"""[AI와 연결하는 부분]

AI(Gemma)에게 실제로 말을 걸고 답을 받아오는 곳입니다.
Gemma 는 OpenAI 와 같은 방식으로 대화할 수 있어서,
OpenAI 도구(라이브러리)를 그대로 빌려 씁니다.
"""

from functools import lru_cache
from typing import Dict, List

from openai import APITimeoutError, OpenAI

from app.config import get_settings


class GemmaError(RuntimeError):
    """AI 를 부르다가 문제가 생겼을 때 쓰는 '전용 오류 표시' 입니다. (-> 502)"""


class GemmaTimeoutError(GemmaError):
    """AI 답이 정해진 시간 안에 오지 않았을 때 쓰는 오류 표시입니다. (-> 504)"""


# @lru_cache : 연결 통로(client)를 한 번만 만들고 계속 다시 씁니다.
@lru_cache
def get_client() -> OpenAI:
    """AI 와 이야기할 '연결 통로(client)' 를 만들어 줍니다."""

    settings = get_settings()
    return OpenAI(
        base_url=settings.gemma_base_url,        # AI 주소
        api_key=settings.gemma_api_key,          # 열쇠
        timeout=settings.gemma_timeout_seconds,  # 기다리는 시간(초)
    )


def chat_completion(messages: List[Dict[str, str]]) -> str:
    """AI 에게 대화 내용을 보내고, AI 가 답한 글을 돌려받습니다.

    messages: AI 에게 보내는 말들의 목록입니다.
        예) [{"role": "system", "content": "너는 도우미야"}, ...]

    돌려주는 값: AI 가 만들어낸 답 글자.

    문제가 생기면:
        - 시간 초과 -> GemmaTimeoutError (나중에 504 로 안내)
        - 그 밖의 실패 -> GemmaError (나중에 502 로 안내)
    """

    settings = get_settings()
    client = get_client()

    # AI 에게 실제로 질문을 보내는 부분입니다.
    # try/except : 혹시 실패하더라도 프로그램이 죽지 않게 감싸줍니다.
    try:
        response = client.chat.completions.create(
            model=settings.gemma_model,              # 어떤 모델에게 물어볼지
            messages=messages,                       # 무엇을 물어볼지
            temperature=settings.gemma_temperature,  # 얼마나 안정적으로 답할지
            # 생각(추론) 과정 켜기/끄기. 이 서버는 chat_template 의
            # enable_thinking 값으로 제어합니다. 기본은 꺼서(False) 추론을
            # 생략하고 바로 답(JSON)만 받습니다.
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": settings.gemma_enable_thinking
                }
            },
        )
    except APITimeoutError as exc:
        # 정해진 시간 안에 답이 안 온 경우 (시간 초과)
        raise GemmaTimeoutError(f"AI 응답 시간 초과: {exc}") from exc
    except Exception as exc:
        # 연결 실패, 인증 실패 등 그 밖의 문제는 하나의 오류로 묶어서 알려줍니다.
        raise GemmaError(f"AI 호출 실패: {exc}") from exc

    # AI 가 아무 답도 안 준 경우를 확인합니다.
    if not response.choices:
        raise GemmaError("AI 응답이 비어 있어요.")

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise GemmaError("AI 가 빈 답을 보냈어요.")

    return content
