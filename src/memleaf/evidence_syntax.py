"""Source-neutral syntax recognizers used by evidence admission."""
from __future__ import annotations

import re
from typing import Iterable

_POLITE = re.compile(r"^(?:(?:麻烦你|麻烦|请问|请|帮我|替我|劳驾)\s*)+")

_QUERY_START = re.compile(
    r"^(?:查询|查一下|查下|看看|看下|查看|阅读|读取|检查|汇总|列出|罗列|告诉我|梳理|盘点|总结|给我|"
    r"把.+(?:列出|发我|告诉我|整理|汇总|梳理|总结)|(?:please\s+)?(?:list|show|tell|summari[sz]e|"
    r"recap|check|find|what|which|who|when|where|why|how)\b)", re.I)

_QUERY_WORD = re.compile(r"有没有|有什么|有哪些|是什么|是谁|多少|哪个|哪些|什么时候|何时|"
                         r"如何|怎么|为什么|是否|能否|可否|\b(?:what|which|who|when|where|why|how)\b", re.I)

_READ_ONLY_CONTROL = re.compile(
    r"^(?:(?:不要|不|请勿|勿)\s*(?:修改|更新|写入|保存|删除)\s*记忆|"
    r"(?:please\s+)?(?:do\s+not|don['’]t)\s+(?:modify|update|write|save|delete)\s+memor(?:y|ies))$",
    re.IGNORECASE,
)

_EXAMPLE = re.compile(r"(?:仅供.{0,8}(?:参考示例|示例|测试)|举(?:一个|个).{0,16}(?:例子|示例)|"
                      r"(?:只是|以下是|这是|作为).{0,12}(?:示例|样例|模板|测试数据)|"
                      r"假设|例如|测试数据|不要.{0,16}(?:记住|记录|当成真实))|"
                      r"\b(?:example|hypothetical|suppose|fictional|test fixture)\b", re.I)

_HEADING = re.compile(r"^\s*(?:#{1,6}\s+.+|\d+[.)、]\s*[^。;；\n]{1,100}[:：]\s*.*)$")

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s+")

_NEGATIVE_TASK = re.compile(r"无需|不需要|不用|不必|无须|毋须|(?:没有|不存在).{0,12}(?:需要|待办|问题)|"
                            r"\b(?:no need|need not|not required|does not need|do not need)\b", re.I)

_CLOSED_TASK = re.compile(r"(?:已|已经).{0,4}(?:全部|均)?(?:完成|取消|解决|关闭)|"
                         r"\b(?:already (?:done|completed|cancelled)|all .{0,20}(?:resolved|completed))\b", re.I)

_EXTERNAL_OWNER = re.compile(r"(?:客户|供应商|第三方)(?:自行|自己)?(?:需要|需|负责|必须|应当|要(?!求))|"
                            r"\b(?:customer|vendor|supplier|third party)\s+(?:must|needs? to|is responsible)\b", re.I)

def _query(text: str) -> bool:
    text = _POLITE.sub("", text.strip())
    control = text.rstrip("。！？!?；;.! ")
    if _READ_ONLY_CONTROL.fullmatch(control):
        return True
    return bool(_QUERY_START.search(text) or _QUERY_WORD.search(text)
                or re.search(r"[?？]|(?:吗|么|呢)[。！!\s]*$", text))

def _clauses(text: str) -> Iterable[tuple[str, tuple[str, ...], bool]]:
    """Separate syntax while retaining headings as context, never as ownership."""
    section: tuple[str, ...] = ()
    in_code = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_code = not in_code
            continue
        if not line:
            continue
        if _HEADING.match(line):
            # Every heading resets context, including unregistered names.
            section = (re.sub(r"^(?:#{1,6}|\d+[.)、])\s*", "", line).split(":", 1)[0].split("：", 1)[0],)
        quoted = in_code or line.startswith(">")
        line = _BULLET.sub("", line)
        # Independent assertion/query clauses must not suppress one another.
        # Do not split numeric thousands separators.
        line = re.sub(r"(?<![0-9])[,，]\s*|[,，](?![0-9])\s*", "\n", line)
        for clause in re.split(r"(?<=[。!?！？;；])\s*|\n+|(?<=[A-Za-z0-9]\.)\s+", line):
            clause = clause.strip()
            if clause:
                yield clause, section, quoted
