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


def redact_line(text: str, limit: int = 200, secrets: tuple[str, ...] = ()) -> str:
    """event에 넣을 한 줄 요약. 공백을 접고 길이를 제한합니다."""

    return " ".join(redact(text, secrets).split())[:limit]


def redact_argv(argv: tuple[str, ...] | list[str], secrets: tuple[str, ...] = ()) -> list[str]:
    """command를 저장하기 전에 secret을 지웁니다.

    argument에 secret이 섞이는 경우를 대비합니다. command 전체를 그대로
    저장하지 말라는 요구를 따르되, 어떤 executor가 무엇을 했는지는 남깁니다.
    """

    return [redact_line(str(token), limit=120, secrets=secrets) for token in argv]
