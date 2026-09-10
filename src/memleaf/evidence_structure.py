"""Deterministic structural segmentation for evidence records."""
from __future__ import annotations

import json
import re
from typing import Iterable

MAX_EXTERNAL_UNIT_BYTES = 32 * 1024

def _external_blocks(text: str) -> Iterable[tuple[int, int, str, str, tuple[str, ...]]]:
    """Yield deterministic, exact source blocks for one external record.

    JSON documents remain whole records.  Plain text that contains explicit
    structure is divided at paragraphs, headings, numbered items and bullets
    so coverage can account for each actionable item.  Ordinary prose and
    line oriented logs remain whole records; punctuation never creates a
    fragment.  The oversized fallback is byte bounded and always returns
    Python character offsets.
    """

    stripped = text.lstrip()
    is_json = False
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            is_json = True

    # Explicit record dividers delimit complete observations in a batched
    # text result. Keep each record's header and paragraphs together so they
    # cannot drift into unrelated model batches. This recognizes layout only;
    # it assigns no business meaning, owner, scope or source authority.
    dividers = list(re.finditer(r"(?m)^[ \t]*(?:={8,}|-{8,}|\*{8,})[ \t]*\r?$", text)) if not is_json else []
    if dividers:
        boundaries = sorted({0, *(match.start() for match in dividers), len(text)})
        for left, right in zip(boundaries, boundaries[1:]):
            block = text[left:right]
            if not block.strip():
                continue
            if re.fullmatch(r"[ \t]*(?:={8,}|-{8,}|\*{8,})[ \t\r\n]*", block):
                continue
            # A divider is structural context, not an independent assertion.
            if len(block.encode("utf-8")) <= MAX_EXTERNAL_UNIT_BYTES:
                yield left, right, block, "external_record", ()
            else:
                # Avoid recursively recognizing the same leading divider.
                cursor = left
                while cursor < right:
                    end = cursor
                    size = 0
                    while end < right:
                        width = len(text[end].encode("utf-8"))
                        if end > cursor and size + width > MAX_EXTERNAL_UNIT_BYTES:
                            break
                        size += width
                        end += 1
                    yield cursor, end, text[cursor:end], "external_block", ()
                    cursor = end
        return

    if len(text.encode("utf-8")) <= MAX_EXTERNAL_UNIT_BYTES and (
        is_json or not _has_external_structure(text)
    ):
        yield 0, len(text), text, "external_record", ()
        return

    if len(text.encode("utf-8")) <= MAX_EXTERNAL_UNIT_BYTES:
        yield from _structured_external_blocks(text)
        return

    start = 0
    while start < len(text):
        end = start
        encoded = 0
        while end < len(text):
            width = len(text[end].encode("utf-8"))
            if end > start and encoded + width > MAX_EXTERNAL_UNIT_BYTES:
                break
            encoded += width
            end += 1
        if end <= start:
            # A single code point larger than the budget is impossible for a
            # normal Unicode scalar, but make progress defensively.
            end = min(start + 1, len(text))
        yield start, end, text[start:end], "external_block", ()
        start = end

_EXTERNAL_MARKER = re.compile(r"^\s*(?:#{1,6}\s+|[-*+•]\s+|\d+[.)、]\s+)")

def _has_external_structure(text: str) -> bool:
    """Recognize structural boundaries without treating every line as one."""

    if "\n\n" in text or "\r\n\r\n" in text:
        return True
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if _EXTERNAL_MARKER.match(line) or value.endswith((":", "：")):
            return True
    return False

def _structured_external_blocks(
    text: str,
) -> Iterable[tuple[int, int, str, str, tuple[str, ...]]]:
    """Split explicit text structure while retaining parent section context."""

    # ``splitlines(True)`` keeps offsets exact while allowing us to discard
    # only structural whitespace at each emitted boundary.
    lines: list[tuple[int, int, str, str]] = []
    cursor = 0
    for raw in text.splitlines(True):
        line_end = cursor + len(raw)
        body = raw[:-1] if raw.endswith("\n") else raw
        if body.endswith("\r"):
            body = body[:-1]
        lines.append((cursor, line_end, body, raw))
        cursor = line_end
    if cursor < len(text):
        lines.append((cursor, len(text), text[cursor:], text[cursor:]))
    if not lines:
        return

    # Stack entries are ``(indent, label, kind)``. Headings remain in scope for
    # sibling numbered items; prior items only remain in scope for indented
    # children such as the two Morgan bullets in the regression digest.
    contexts: list[tuple[int, str, str]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_section: tuple[str, ...] = ()
    current_syntax = "external_paragraph"

    def emit_current() -> tuple[int, int, str, str, tuple[str, ...]] | None:
        if current_start is None or current_end is None or current_start >= current_end:
            return None
        return (
            current_start,
            current_end,
            text[current_start:current_end],
            current_syntax,
            current_section,
        )

    for line_start, line_end, body, raw in lines:
        left = len(body) - len(body.lstrip())
        right = len(body.rstrip())
        value = body.strip()
        if not value:
            emitted = emit_current()
            if emitted is not None:
                yield emitted
            current_start = current_end = None
            current_section = ()
            current_syntax = "external_paragraph"
            continue

        indent = left
        marker = _EXTERNAL_MARKER.match(body)
        heading = bool(re.match(r"^\s*#{1,6}\s+", body)) or (
            not marker and value.endswith((":", "："))
        )
        structural = bool(marker) or heading
        if structural:
            emitted = emit_current()
            if emitted is not None:
                yield emitted
            current_start = line_start + left
            current_end = line_start + right
            current_syntax = "external_section"

            if heading:
                contexts = [
                    (level, label, kind)
                    for level, label, kind in contexts
                    if level < indent
                ]
                current_section = tuple(label for _, label, _ in contexts)
                contexts.append((indent, value, "heading"))
            else:
                # Same-level numbered/bullet siblings replace the previous
                # item, while a heading at that level remains their context.
                contexts = [
                    (level, label, kind)
                    for level, label, kind in contexts
                    if level < indent or (level == indent and kind == "heading")
                ]
                current_section = tuple(label for _, label, _ in contexts)
                contexts.append((indent, value, "item"))
            continue

        # Non-structural lines continue the current item/paragraph. This keeps
        # wrapped prose together and avoids turning line-oriented logs into one
        # evidence unit per line.
        line_content_start = line_start + left
        line_content_end = line_start + right
        if current_start is None:
            current_start = line_content_start
            current_section = tuple(label for _, label, _ in contexts)
            current_syntax = "external_paragraph"
        current_end = line_content_end

    emitted = emit_current()
    if emitted is not None:
        yield emitted
