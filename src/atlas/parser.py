"""Atlas Task Issue Form body parser.

docs/specs/task-schema.md의 "GitHub Issue Mapping"을 구현합니다. Issue body는
신뢰되지 않은 입력이므로 parser는 heading label에만 의존하고 값 해석은
validation 단계로 넘깁니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# GitHub Issue Form은 label을 항상 `###`으로 렌더링합니다. 다른 heading level을
# 인식하지 않으면 사용자가 값 안에 쓴 Markdown이 section을 쪼개지 않습니다.
_HEADING = re.compile(r"^### +(.+?) *$")
_OPENING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CLOSING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_BULLET = re.compile(r"^ *[-*+] +(.*)$")
_CHECKBOX = re.compile(r"^ *[-*+] +\[([ xX])\] *(.*)$")

# GitHub가 빈 optional 입력을 렌더링할 때 쓰는 marker.
_NO_RESPONSE = "_No response_"

# Issue Form label -> Task 필드 식별자.
FIELD_LABELS: dict[str, str] = {
    "project": "project",
    "objective": "objective",
    "constraints": "constraints",
    "acceptance criteria": "acceptance_criteria",
    "allowed scope": "allowed_scope",
    "forbidden scope": "forbidden_scope",
    "risk level": "risk_level",
    "priority": "priority",
    "validation": "validation",
    "context and references": "context",
    "risk / notes": "notes",
    "safety confirmations": "safety_confirmations",
}


@dataclass(frozen=True)
class Checkbox:
    checked: bool
    label: str


@dataclass(frozen=True)
class ParsedBody:
    sections: dict[str, str]
    duplicate_labels: tuple[str, ...]
    unknown_labels: tuple[str, ...]

    def text(self, field: str) -> str:
        return self.sections.get(field, "")


def _normalize_label(raw: str) -> str:
    return " ".join(raw.strip().casefold().split())


def parse_issue_body(body: str) -> ParsedBody:
    """Issue body를 field 식별자별 원문 텍스트로 나눕니다."""

    sections: dict[str, str] = {}
    duplicates: list[str] = []
    unknown: list[str] = []

    current: str | None = None
    buffer: list[str] = []
    in_fence = False
    fence_char = ""
    fence_length = 0

    def flush() -> None:
        if current is None:
            return
        value = "\n".join(buffer).strip()
        if value == _NO_RESPONSE:
            value = ""
        sections[current] = value

    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if in_fence:
            closing = _CLOSING_FENCE.match(line)
            if closing:
                marker = closing.group(1)
                if marker[0] == fence_char and len(marker) >= fence_length:
                    in_fence, fence_char, fence_length = False, "", 0
            if current is not None:
                buffer.append(line)
            continue

        opening = _OPENING_FENCE.match(line)
        if opening:
            marker, info = opening.groups()
            # CommonMark에서 backtick fence의 info string에는 backtick을 쓸 수 없습니다.
            if marker[0] != "`" or "`" not in info:
                in_fence, fence_char, fence_length = True, marker[0], len(marker)
                if current is not None:
                    buffer.append(line)
                continue

        heading = None if in_fence else _HEADING.match(line)
        if heading is None:
            if current is not None:
                buffer.append(line)
            continue

        label = _normalize_label(heading.group(1))
        field = FIELD_LABELS.get(label)
        if field is None:
            # 알려진 label만 section 경계입니다. 사용자가 textarea에 쓴 `###`
            # 제목이 뒤따르는 내용을 잘라내지 않도록 값의 일부로 보존합니다.
            unknown.append(heading.group(1).strip())
            if current is not None:
                buffer.append(line)
            continue

        flush()
        if field in sections:
            duplicates.append(heading.group(1).strip())
        current, buffer = field, []

    flush()
    return ParsedBody(
        sections=sections,
        duplicate_labels=tuple(dict.fromkeys(duplicates)),
        unknown_labels=tuple(dict.fromkeys(unknown)),
    )


def split_items(text: str) -> tuple[str, ...]:
    """텍스트를 항목 목록으로 나눕니다.

    bullet은 marker를 제거하고, bullet이 아닌 줄은 한 줄을 한 항목으로 봅니다.
    """

    items: list[str] = []
    for line in text.split("\n"):
        bullet = _BULLET.match(line)
        value = (bullet.group(1) if bullet else line).strip()
        if value:
            items.append(value)
    return tuple(items)


def split_checklist(text: str) -> tuple[str, ...]:
    """`- [ ]` 접두사를 제거한 항목 목록을 돌려줍니다."""

    items: list[str] = []
    for line in text.split("\n"):
        checkbox = _CHECKBOX.match(line)
        if checkbox:
            value = checkbox.group(2).strip()
        else:
            bullet = _BULLET.match(line)
            value = (bullet.group(1) if bullet else line).strip()
        if value:
            items.append(value)
    return tuple(items)


def parse_checkboxes(text: str) -> tuple[Checkbox, ...]:
    boxes: list[Checkbox] = []
    for line in text.split("\n"):
        checkbox = _CHECKBOX.match(line)
        if checkbox:
            boxes.append(
                Checkbox(checked=checkbox.group(1).lower() == "x", label=checkbox.group(2).strip())
            )
    return tuple(boxes)
