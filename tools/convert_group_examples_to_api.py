import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "GROUP_CHAT_PROMPT_V2_EXAMPLES_64.md"
TARGET = ROOT / "GROUP_CHAT_API_REQUEST_EXAMPLES_64.md"

CASE_PATTERN = re.compile(
    r"### (?P<number>\d{2})\. (?P<title>[^\n]+)\n\n"
    r"참여자: (?P<participants>[^\n]+)\n\n"
    r"```text\n(?P<dialogue>.*?)\n```\n\n"
    r"```json\n(?P<expected>.*?)\n```",
    re.DOTALL,
)


def user_id(label: str) -> str:
    number = int(label.removeprefix("P"))
    return f"u-{100 + number}"


def replace_expected_participants(value):
    if isinstance(value, list):
        return [replace_expected_participants(item) for item in value]
    if isinstance(value, dict):
        converted = {}
        for key, item in value.items():
            if key == "participants":
                converted[key] = [user_id(label) for label in item]
            else:
                converted[key] = replace_expected_participants(item)
        return converted
    return value


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    cases = list(CASE_PATTERN.finditer(source))
    if len(cases) != 64:
        raise RuntimeError(f"64개 사례가 필요하지만 {len(cases)}개를 찾았습니다.")

    output = [
        "# SYSTEM_PROMPT_V2 API 요청 예시 64개",
        "",
        "## 공통 기준",
        "",
        "- 기준일: `2026-08-12` 수요일",
        "- 참여자 변환: `P1~P6` → `u-101~u-106`",
        "- 메시지 시각: 제공 예시와 같은 UTC ISO 8601 형식",
        "- 각 사례는 API 요청 JSON과 예상 응답 JSON으로 구성",
        "- 제공 예시의 `2026-07-24`는 메시지 날짜 및 기존 정답과 불일치하여 사용하지 않음",
        "",
        "---",
        "",
    ]

    base_time = datetime(2026, 8, 12, 4, 40, 8, 300000, tzinfo=timezone.utc)

    for match in cases:
        number = match.group("number")
        title = match.group("title")
        participant_labels = re.findall(r"P\d+", match.group("participants"))
        participants = [user_id(label) for label in participant_labels]

        messages = []
        for index, line in enumerate(match.group("dialogue").splitlines()):
            message_match = re.match(r"^(P\d+):\s?(.*)$", line)
            if not message_match:
                raise RuntimeError(f"사례 {number}에서 메시지를 해석하지 못했습니다: {line}")
            sent_at = base_time + timedelta(minutes=index)
            messages.append(
                {
                    "sender_id": user_id(message_match.group(1)),
                    "content": message_match.group(2),
                    "sent_at": sent_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                }
            )

        request = {
            "room_id": f"room-{number}",
            "reference_date": "2026-08-12",
            "messages": messages,
            "participants": participants,
        }
        expected = replace_expected_participants(json.loads(match.group("expected")))

        output.extend(
            [
                f"## {number}. {title}",
                "",
                "### 요청 JSON",
                "",
                "```json",
                json.dumps(request, ensure_ascii=False, indent=2),
                "```",
                "",
                "### 예상 응답 JSON",
                "",
                "```json",
                json.dumps(expected, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )

    TARGET.write_text("\n".join(output), encoding="utf-8")


if __name__ == "__main__":
    main()
