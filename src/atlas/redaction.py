"""Secret redaction.

docs/security-governance.md의 Logging and Redaction 요구를 구현합니다. worker와
executor의 stdout/stderr, structured error, audit log에서 secret을 지웁니다.

git 출력용과 executor 출력용은 경계가 다릅니다.

- git은 짧은 오류 메시지 한 줄이 대부분이고 저장 대상이 event입니다.
- executor는 임의 길이 출력이고 저장 대상이 log artifact입니다. 주입한 known
  secret 값까지 지워야 합니다.

공통 pattern은 여기 두고, 호출자가 필요한 boundary를 골라 씁니다.
"""

from __future__ import annotations

import re

# URL에 박힌 credential: https://user:token@host
_CREDENTIAL_IN_URL = re.compile(r"(?i)\b((?:https?|ssh|git)://)[^/\s@]+@")

# provider token 형태.
_TOKEN_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{10,})\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    # JWT 형태
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"),
)

# Authorization 헤더와 Bearer 토큰.
# 헤더는 줄 끝까지 통째로 지웁니다. `\S+`만 지우면
# `Authorization: Bearer <token>`에서 "Bearer"만 사라지고 토큰이 그대로 남습니다.
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+"
)
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}")

PLACEHOLDER = "<redacted>"

# 이보다 짧은 값은 우연히 일반 단어와 겹칠 수 있어 known secret으로 지우지
# 않습니다. 짧은 값을 무차별 치환하면 로그가 읽을 수 없게 됩니다.
MIN_KNOWN_SECRET_LENGTH = 8


def redact_patterns(text: str) -> str:
    """알려진 secret 형태를 지웁니다."""

    if not text:
        return ""
    cleaned = _CREDENTIAL_IN_URL.sub(rf"\1{PLACEHOLDER}@", text)
    cleaned = _AUTH_HEADER.sub(rf"\1: {PLACEHOLDER}", cleaned)
    cleaned = _BEARER.sub(rf"\1 {PLACEHOLDER}", cleaned)
    for pattern in _TOKEN_PATTERNS:
        cleaned = pattern.sub(PLACEHOLDER, cleaned)
    return cleaned


def redact_values(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """환경으로 주입한 known secret의 raw 값을 지웁니다.

    긴 값부터 치환해야 짧은 값이 긴 값의 일부를 먼저 깨뜨리지 않습니다.
    """

    if not text or not secrets:
        return text or ""
    cleaned = text
    for value in sorted({s for s in secrets if s}, key=len, reverse=True):
        if len(value) < MIN_KNOWN_SECRET_LENGTH:
            continue
        cleaned = cleaned.replace(value, PLACEHOLDER)
    return cleaned


def redact(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """known secret 값을 먼저 지우고 형태 기반 pattern을 적용합니다."""

    return redact_patterns(redact_values(text, secrets))


# streaming redaction이 flush 경계를 정할 때 쓰는 pattern 여유 폭입니다.
# 개행 없이 쏟아지는 출력에서 token/JWT/Bearer가 경계를 가로지르지 않도록
# 이만큼은 다음 회차로 넘겨 다시 검사합니다.
PATTERN_OVERLAP_WINDOW = 512


def overlap_window(secrets: tuple[str, ...] | list[str] = ()) -> int:
    """flush 경계에서 되돌려 둬야 할 최소 길이입니다.

    known secret은 길이를 알 수 있으므로 그중 가장 긴 값을 기준으로 잡습니다.
    pattern은 길이가 열려 있어 고정 window를 씁니다.
    """

    longest = max(
        (len(v) for v in secrets or () if v and len(v) >= MIN_KNOWN_SECRET_LENGTH),
        default=0,
    )
    return max(PATTERN_OVERLAP_WINDOW, longest)


def redaction_spans(
    text: str, secrets: tuple[str, ...] | list[str] = ()
) -> list[tuple[int, int]]:
    """`redact`가 지울 구간을 돌려줍니다.

    streaming flush 지점이 secret 한가운데를 자르지 않게 하는 용도입니다.
    치환 결과가 아니라 원본 문자열의 index로 돌려줍니다.
    """

    if not text:
        return []
    spans: list[tuple[int, int]] = []
    for value in sorted({s for s in secrets or () if s}, key=len, reverse=True):
        if len(value) < MIN_KNOWN_SECRET_LENGTH:
            continue
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value)))
            start = text.find(value, start + 1)
    for pattern in (_CREDENTIAL_IN_URL, _AUTH_HEADER, _BEARER, *_TOKEN_PATTERNS):
        for match in pattern.finditer(text):
            spans.append(match.span())
    return spans


def safe_split_index(
    text: str, index: int, secrets: tuple[str, ...] | list[str] = ()
) -> int:
    """`index`에서 자를 때 secret을 반으로 쪼개지 않는 지점을 돌려줍니다.

    경계를 가로지르는 구간이 있으면 그 시작점까지 물러섭니다. 물러선 부분은
    다음 회차로 넘어가 온전한 상태에서 지워집니다.
    """

    if index <= 0 or index >= len(text):
        return index
    for start, end in redaction_spans(text, secrets):
        if start < index < end:
            index = start
    return max(index, 0)


def redact_line(text: str, limit: int = 200, secrets: tuple[str, ...] = ()) -> str:
    """event에 넣을 한 줄 요약. 공백을 접고 길이를 제한합니다."""

    return " ".join(redact(text, secrets).split())[:limit]


def redact_argv(argv: tuple[str, ...] | list[str], secrets: tuple[str, ...] = ()) -> list[str]:
    """command를 저장하기 전에 secret을 지웁니다.

    argument에 secret이 섞이는 경우를 대비합니다. command 전체를 그대로
    저장하지 말라는 요구를 따르되, 어떤 executor가 무엇을 했는지는 남깁니다.
    """

    return [redact_line(str(token), limit=120, secrets=secrets) for token in argv]
