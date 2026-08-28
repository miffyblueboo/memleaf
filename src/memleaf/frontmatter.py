"""A deliberately small YAML subset used by config and Markdown frontmatter.

The project is standard-library-only.  This parser intentionally supports only
the predictable subset memleaf writes: mappings, block lists, inline scalar
lists, strings, numbers, booleans, and null.  It rejects YAML features whose
semantics would be surprising without a full YAML implementation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence, Tuple


class FrontmatterError(ValueError):
    """Raised when the restricted YAML/Markdown format is invalid."""


@dataclass(frozen=True)
class _Line:
    number: int
    indent: int
    content: str


def _error(number: int, message: str) -> FrontmatterError:
    return FrontmatterError(f"invalid restricted YAML at line {number}: {message}")


def _strip_comment(value: str) -> str:
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ('"', "'"):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value.rstrip()


def _prepare_lines(text: str) -> List[_Line]:
    result: List[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        leading = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:leading]:
            raise _error(number, "tabs are not allowed for indentation")
        if leading % 2:
            raise _error(number, "indentation must use two-space levels")
        content = _strip_comment(raw[leading:]).strip()
        if not content:
            continue
        if content in ("---", "..."):
            raise _error(number, "document markers are not allowed inside a value")
        result.append(_Line(number, leading, content))
    return result


def _find_colon(value: str) -> int:
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ('"', "'"):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == ":" and quote is None and (
            index + 1 == len(value) or value[index + 1].isspace()
        ):
            return index
    return -1


def _split_flow_items(value: str, number: int) -> List[str]:
    if not (value.startswith("[") and value.endswith("]")):
        raise _error(number, "invalid inline list")
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: List[str] = []
    start = 0
    quote = None
    escaped = False
    for index, character in enumerate(inner):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ('"', "'"):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "," and quote is None:
            item = inner[start:index].strip()
            if not item:
                raise _error(number, "empty inline-list item")
            items.append(item)
            start = index + 1
    if quote is not None:
        raise _error(number, "unterminated quoted value")
    item = inner[start:].strip()
    if not item:
        raise _error(number, "empty inline-list item")
    items.append(item)
    return items


def _parse_scalar(value: str, number: int) -> Any:
    value = value.strip()
    if not value:
        return None
    if value.startswith("["):
        if not value.endswith("]"):
            raise _error(number, "unterminated inline list")
        return [_parse_scalar(item, number) for item in _split_flow_items(value, number)]
    if value.startswith("{") or value in ("|", ">") or value.startswith(("&", "*", "!")):
        raise _error(number, "unsupported YAML feature")
    if value.startswith('"'):
        if not value.endswith('"'):
            raise _error(number, "unterminated double-quoted string")
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise _error(number, "invalid double-quoted string")
    if value.startswith("'"):
        if not value.endswith("'"):
            raise _error(number, "unterminated single-quoted string")
        return value[1:-1].replace("''", "'")
    lowered = value.casefold()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            raise _error(number, "invalid integer")
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", value) or re.fullmatch(
        r"[-+]?\d+[eE][-+]?\d+", value
    ):
        try:
            return float(value)
        except ValueError:
            raise _error(number, "invalid number")
    if "\n" in value or "\r" in value:
        raise _error(number, "multiline values are not allowed")
    return value


class _Parser:
    def __init__(self, lines: Sequence[_Line]):
        self.lines = lines

    def parse(self) -> Any:
        if not self.lines:
            return {}
        if self.lines[0].indent != 0:
            raise _error(self.lines[0].number, "top-level indentation is not allowed")
        value, index = self._block(0, 0)
        if index != len(self.lines):
            line = self.lines[index]
            raise _error(line.number, "unexpected indentation")
        return value

    def _block(self, index: int, indent: int) -> Tuple[Any, int]:
        if index >= len(self.lines):
            return {}, index
        line = self.lines[index]
        if line.indent != indent:
            raise _error(line.number, "unexpected indentation")
        if line.content == "-" or line.content.startswith("- "):
            return self._list(index, indent)
        return self._mapping(index, indent)

    def _mapping(self, index: int, indent: int) -> Tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(self.lines):
            line = self.lines[index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise _error(line.number, "unexpected indentation")
            if line.content == "-" or line.content.startswith("- "):
                break
            key, raw_value = self._mapping_entry(line)
            if key in result:
                raise _error(line.number, "duplicate key")
            index += 1
            if raw_value == "":
                if index < len(self.lines) and self.lines[index].indent > indent:
                    child_indent = self.lines[index].indent
                    value, index = self._block(index, child_indent)
                else:
                    value = {}
            else:
                value = _parse_scalar(raw_value, line.number)
                if index < len(self.lines) and self.lines[index].indent > indent:
                    raise _error(self.lines[index].number, "nested value needs a mapping key")
            result[key] = value
        return result, index

    def _list(self, index: int, indent: int) -> Tuple[list[Any], int]:
        result: list[Any] = []
        while index < len(self.lines):
            line = self.lines[index]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise _error(line.number, "unexpected indentation")
            if not (line.content == "-" or line.content.startswith("- ")):
                break
            rest = line.content[1:].strip()
            index += 1
            if not rest:
                if index < len(self.lines) and self.lines[index].indent > indent:
                    child_indent = self.lines[index].indent
                    value, index = self._block(index, child_indent)
                else:
                    value = None
                result.append(value)
                continue
            colon = _find_colon(rest)
            if colon < 1:
                value = _parse_scalar(rest, line.number)
                if index < len(self.lines) and self.lines[index].indent > indent:
                    raise _error(self.lines[index].number, "unexpected nested value")
                result.append(value)
                continue
            key = rest[:colon].strip()
            raw_value = rest[colon + 1 :].strip()
            if not key or any(character in key for character in "\n\r"):
                raise _error(line.number, "invalid mapping key")
            item: dict[str, Any] = {}
            if raw_value == "":
                if index < len(self.lines) and self.lines[index].indent > indent:
                    child_indent = self.lines[index].indent
                    value, index = self._block(index, child_indent)
                else:
                    value = {}
            else:
                value = _parse_scalar(raw_value, line.number)
            item[key] = value
            if index < len(self.lines) and self.lines[index].indent > indent:
                continuation_indent = self.lines[index].indent
                continuation, index = self._mapping(index, continuation_indent)
                for continuation_key, continuation_value in continuation.items():
                    if continuation_key in item:
                        raise _error(line.number, "duplicate key in list item")
                    item[continuation_key] = continuation_value
            result.append(item)
        return result, index

    @staticmethod
    def _mapping_entry(line: _Line) -> Tuple[str, str]:
        colon = _find_colon(line.content)
        if colon < 1:
            raise _error(line.number, "expected key: value")
        key = line.content[:colon].strip()
        if not key or key[0] in "-?!" or any(character in key for character in "\n\r"):
            raise _error(line.number, "invalid mapping key")
        if key.startswith(("'", '"')):
            parsed_key = _parse_scalar(key, line.number)
            if not isinstance(parsed_key, str):
                raise _error(line.number, "mapping key must be a string")
            key = parsed_key
        return key, line.content[colon + 1 :].strip()


def load_yaml(text: str) -> Any:
    return _Parser(_prepare_lines(text)).parse()


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise FrontmatterError("multiline strings are not supported")
        return json.dumps(value, ensure_ascii=False)
    raise FrontmatterError("unsupported value type")


def _validate_key(key: Any) -> str:
    if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
        raise FrontmatterError("mapping keys must be non-empty single-line strings")
    if any(character in key for character in "{}[]"):
        raise FrontmatterError("mapping key uses unsupported syntax")
    return key


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _dump_mapping(mapping: Mapping[str, Any], indent: int) -> List[str]:
    lines: List[str] = []
    prefix = " " * indent
    for raw_key, value in mapping.items():
        key = _validate_key(raw_key)
        if _is_scalar(value):
            lines.append(f"{prefix}{key}: {_dump_scalar(value)}")
        elif isinstance(value, list) and all(_is_scalar(item) for item in value):
            items = ", ".join(_dump_scalar(item) for item in value)
            lines.append(f"{prefix}{key}: [{items}]")
        else:
            lines.append(f"{prefix}{key}:")
            lines.extend(_dump_value(value, indent + 2))
    return lines


def _dump_value(value: Any, indent: int) -> List[str]:
    prefix = " " * indent
    if isinstance(value, Mapping):
        return _dump_mapping(value, indent)
    if isinstance(value, list):
        lines: List[str] = []
        for item in value:
            if _is_scalar(item):
                lines.append(f"{prefix}- {_dump_scalar(item)}")
            elif isinstance(item, Mapping):
                entries = list(item.items())
                if not entries:
                    lines.append(f"{prefix}- {{}}")
                    continue
                first_key, first_value = entries[0]
                key = _validate_key(first_key)
                if _is_scalar(first_value):
                    lines.append(f"{prefix}- {key}: {_dump_scalar(first_value)}")
                else:
                    lines.append(f"{prefix}- {key}:")
                    lines.extend(_dump_value(first_value, indent + 4))
                if len(entries) > 1:
                    lines.extend(_dump_mapping(dict(entries[1:]), indent + 2))
            else:
                raise FrontmatterError("unsupported list item type")
        return lines
    raise FrontmatterError("unsupported nested value type")


def dump_yaml(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise FrontmatterError("top-level YAML value must be a mapping")
    return "\n".join(_dump_mapping(value, 0)) + "\n"


def parse_frontmatter(text: str) -> Tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("Markdown memory must start with frontmatter")
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise FrontmatterError("frontmatter closing marker is missing")
    metadata = load_yaml("\n".join(lines[1:closing]))
    if not isinstance(metadata, dict):
        raise FrontmatterError("frontmatter must be a mapping")
    body = "\n".join(lines[closing + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    return metadata, body.rstrip("\n")


def dump_frontmatter(metadata: Mapping[str, Any], body: str) -> str:
    if not isinstance(body, str):
        raise FrontmatterError("memory body must be text")
    rendered = dump_yaml(metadata)
    return f"---\n{rendered}---\n\n{body.rstrip(chr(10))}\n"
