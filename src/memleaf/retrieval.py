"""Deterministic local retrieval and scope filtering."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .index import normalize_term
from .models import Memory


_ASCII_TERM = re.compile(r"[a-z0-9]+(?:[ ._-][a-z0-9]+)*")


def _is_cjk_char(value: str) -> bool:
    """Return whether a character belongs to the common CJK ideographs."""

    if not value:
        return False
    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _query_parts(query: str | Iterable[str]) -> tuple[str, list[str], set[str]]:
    """Return the normalized query and its lexical components.

    ``query_terms`` also exposes a complete normalized phrase for callers
    that need it.  Candidate filtering needs the components separately so it
    can distinguish a meaningful topic fragment from a short local suffix.
    """

    query_value = query if isinstance(query, str) else " ".join(str(item) for item in query)
    normalized = normalize_term(query_value)
    if not normalized:
        return "", [], set()
    raw_parts = re.findall(r"[^\W_]+", query_value, re.UNICODE)
    normalized_raw_parts = [normalize_term(item) for item in raw_parts]
    parts = list(dict.fromkeys(part for part in normalized_raw_parts if part))
    explicit: set[str] = set()
    for raw_part, part in zip(raw_parts, normalized_raw_parts):
        if any(_is_cjk_char(char) for char in part):
            continue
        if any(char.isdigit() for char in raw_part) or raw_part.isalpha() and raw_part.isupper():
            explicit.add(part)
    # A trailing hyphen component is a common identifier form even when a
    # caller lowercases it (for example, ``topic-xyz``).  Keep the rule
    # structural rather than naming any particular identifier.
    hyphen_tail = re.search(r"-([a-z0-9]+)\s*$", normalized)
    if hyphen_tail and len(hyphen_tail.group(1)) >= 3:
        explicit.add(hyphen_tail.group(1))
    return normalized, parts, explicit


def _longest_common_substring(left: str, right: str) -> int:
    """Return the longest contiguous shared span without allocating slices."""

    if not left or not right:
        return 0
    # The query component is normally much shorter than a memory body.  Keep
    # the dynamic row on the shorter value to bound temporary memory.
    if len(left) < len(right):
        short, longer = left, right
    else:
        short, longer = right, left
    previous = [0] * (len(short) + 1)
    longest = 0
    for long_char in longer:
        current = [0]
        for index, short_char in enumerate(short, start=1):
            if long_char == short_char:
                span = previous[index - 1] + 1
                current.append(span)
                if span > longest:
                    longest = span
            else:
                current.append(0)
        previous = current
    return longest


def _candidate_values(memory: Memory) -> list[str]:
    """Return searchable fields, including metadata not present in fulltext."""

    values = [memory.title, memory.body, *memory.tags, *memory.aliases, *memory.keywords]
    return [normalize_term(value) for value in values if isinstance(value, str) and value]


def candidate_matches_query(memory: Memory, query: str | Iterable[str]) -> bool:
    """Apply strict candidate relevance rules for the public directory API.

    The legacy search path intentionally keeps its broad indexed/full-text
    behavior.  This predicate is used only by ``search_candidates``: exact
    identifiers and complete phrases are always trusted; a single-term query
    retains its old intent; a multi-component query must contain either a
    complete component or a sufficiently long contiguous topic span.  Thus a
    short local substring such as ``项目`` cannot stand in for a larger topic
    such as ``火星仓库项目`` without maintaining a list of domain stopwords.
    """

    normalized_query, parts, explicit_parts = _query_parts(query)
    if not normalized_query:
        return False
    if normalize_term(memory.memory_id) == normalized_query:
        return True
    values = _candidate_values(memory)
    if not values:
        return False
    if any(normalized_query in value for value in values):
        return True
    if not parts:
        return False

    # A query made of one lexical term keeps the established candidate API
    # behavior, including ordinary short/generic terms such as ``项目``.
    if len(parts) == 1 and parts[0] == normalized_query:
        part = parts[0]
        if any(part in value for value in values):
            return True
        if not any(_is_cjk_char(char) for char in part):
            return False
        minimum = max(3, (len(part) * 35 + 99) // 100)
        return max((_longest_common_substring(part, value) for value in values), default=0) >= minimum

    has_cjk_part = any(any(_is_cjk_char(char) for char in part) for part in parts)
    explicit_ascii_required = bool(explicit_parts) or (
        has_cjk_part
        and any(
            not any(_is_cjk_char(char) for char in part)
            and (len(part) >= 3 or any(char.isdigit() for char in part))
            for part in parts
        )
    )
    strong_match = False
    strong_cjk_match = False
    explicit_ascii_match = False
    weak_matches = 0
    for part in parts:
        if any(_is_cjk_char(char) for char in part):
            longest = max((_longest_common_substring(part, value) for value in values), default=0)
            minimum = max(3, (len(part) * 35 + 99) // 100)
            if longest >= minimum:
                strong_match = True
                strong_cjk_match = True
            elif longest:
                weak_matches += 1
            continue

        # For a component in a compound query, require a complete ASCII token
        # (or a numeric identifier) rather than an incidental word fragment.
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(part) + r"(?![a-z0-9])")
        if any(pattern.search(value) for value in values):
            if len(part) >= 3 or any(char.isdigit() for char in part):
                strong_match = True
                if part in explicit_parts or (explicit_ascii_required and not explicit_parts):
                    explicit_ascii_match = True
            else:
                weak_matches += 1

    if explicit_ascii_required and not explicit_ascii_match:
        return False
    if explicit_ascii_required and has_cjk_part and not strong_cjk_match:
        return False
    if strong_match:
        return True
    # Two independently matching short components provide more signal than a
    # single generic local word, without depending on a hard-coded word list.
    return weak_matches >= 2


class RetrievalError(ValueError):
    """A safe, typed failure from a retrieval protocol operation.

    The stable ``code`` is intended for adapters.  The message is kept short
    and contains no query, memory body, or filesystem details.
    """

    def __init__(self, code: str, message: str):
        if not isinstance(code, str) or not code:
            raise ValueError("retrieval error code is required")
        if not isinstance(message, str) or not message:
            raise ValueError("retrieval error message is required")
        self.code = code
        super().__init__(message)


def _matches_index_term(
    index_name: str,
    indexed_term: str,
    normalized_query: str,
    terms: Sequence[str],
) -> bool:
    candidates = [indexed_term]
    if index_name == "wikilinks" and any(separator in indexed_term for separator in (":", "/", "#")):
        candidates.extend(
            part for part in re.split(r"[:/#]", indexed_term) if part
        )
    for candidate in candidates:
        if _ASCII_TERM.fullmatch(candidate):
            boundary_pattern = re.compile(
                r"(?<![a-z0-9])" + re.escape(candidate) + r"(?![a-z0-9])"
            )
            if boundary_pattern.search(normalized_query):
                return True
        elif candidate in normalized_query or any(candidate in query_term for query_term in terms):
            return True
    return False


def query_terms(query: str | Iterable[str]) -> list[str]:
    if isinstance(query, str):
        raw = query
    else:
        raw = " ".join(str(item) for item in query)
    normalized = normalize_term(raw)
    if not normalized:
        return []
    terms = [normalize_term(item) for item in re.findall(r"[^\W_]+", normalized, re.UNICODE)]
    terms = [term for term in terms if term]
    if normalized not in terms:
        terms.append(normalized)
    return list(dict.fromkeys(terms))


def matching_index_terms(index: Mapping[str, Any], query: str | Iterable[str]) -> dict[str, int]:
    query_value = query if isinstance(query, str) else list(query)
    normalized_query = normalize_term(" ".join(query_value) if not isinstance(query_value, str) else query_value)
    terms = query_terms(query_value)
    scores: dict[str, int] = defaultdict(int)
    for index_name in ("tags", "aliases", "keywords", "wikilinks"):
        mapping = index.get(index_name, {})
        if not isinstance(mapping, Mapping):
            continue
        for indexed_term, memory_ids in mapping.items():
            term = normalize_term(indexed_term)
            if not term:
                continue
            matched = _matches_index_term(index_name, term, normalized_query, terms)
            if not matched:
                continue
            if isinstance(memory_ids, list):
                for memory_id in memory_ids:
                    if isinstance(memory_id, str):
                        scores[memory_id] += 1
    return dict(scores)


def fulltext_score(memory: Memory, query: str | Iterable[str]) -> int:
    query_value = (
        query if isinstance(query, str) else " ".join(str(item) for item in query)
    )
    normalized_query = normalize_term(query_value)
    terms = query_terms(normalized_query)
    if not terms:
        return 0
    haystack = normalize_term("\n".join((memory.title, memory.body, *memory.tags)))
    score = sum(1 for term in terms if term in haystack)
    # A complete multi-term query identifies a much narrower topic than any
    # one of its component terms.  Keep this deterministic and local while
    # making an exact title/body match outrank common index terms.
    if len(terms) > 1 and normalized_query in haystack:
        score += len(terms)
    return score


def _scope_values(scope: str | Iterable[str] | None) -> list[str]:
    if scope is None:
        return []
    if isinstance(scope, str):
        values = [scope]
    else:
        values = list(scope)
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise ValueError("invalid scope")
        result.append(value)
    return list(dict.fromkeys(result))


def scope_parents(config: Mapping[str, Any]) -> dict[str, str]:
    """Build child -> parent links from the restricted config scope tree."""

    parents: dict[str, str] = {}
    scopes = config.get("scopes", {}) if isinstance(config, Mapping) else {}
    if not isinstance(scopes, Mapping):
        return parents
    for parent, value in scopes.items():
        if not isinstance(parent, str) or not isinstance(value, Mapping):
            continue
        children = value.get("children", [])
        if isinstance(children, str):
            children = [children]
        if isinstance(children, list):
            for child in children:
                if isinstance(child, str) and child not in parents:
                    parents[child] = parent
        declared_parent = value.get("parent")
        if isinstance(declared_parent, str):
            parents[parent] = declared_parent
    return parents


def inherited_scopes(requested: str | Iterable[str], config: Mapping[str, Any]) -> dict[str, int]:
    """Return allowed scopes and specificity ranks for a query scope."""

    requested_values = _scope_values(requested)
    parents = scope_parents(config)
    allowed: dict[str, int] = {}
    for value in requested_values:
        current = value
        rank = 4 if value.startswith("project:") else 3 if value.startswith("portfolio:") else 2 if value.startswith("domain:") else 1
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            allowed[current] = max(rank, allowed.get(current, 0))
            if current == "global":
                break
            current = parents.get(current)
            if current is None:
                if value != "global":
                    allowed["global"] = max(1, allowed.get("global", 0))
                break
            rank = max(1, rank - 1)
    return allowed


def memory_scope_rank(memory: Memory, allowed: Mapping[str, int]) -> int:
    return max((allowed.get(scope, -1) for scope in memory.scopes), default=-1)


def filter_by_scope(
    memories: Sequence[Memory],
    scope: str | Iterable[str] | None,
    config: Mapping[str, Any],
) -> list[tuple[Memory, int]]:
    if scope is None:
        return [(memory, 0) for memory in memories]
    allowed = inherited_scopes(scope, config)
    matched = [(memory, memory_scope_rank(memory, allowed)) for memory in memories]
    matched = [(memory, rank) for memory, rank in matched if rank >= 0]
    if not matched:
        return []

    # A more specific memory supersedes a same-titled inherited memory.  The
    # Markdown files remain untouched; this is only a retrieval-time overlay.
    grouped: dict[str, list[tuple[Memory, int]]] = defaultdict(list)
    for memory, rank in matched:
        key = normalize_term(memory.title)
        if key:
            grouped[key].append((memory, rank))
    hidden: set[str] = set()
    for values in grouped.values():
        if len(values) < 2:
            continue
        highest = max(rank for _, rank in values)
        for memory, rank in values:
            if rank < highest:
                hidden.add(memory.memory_id)
    return [(memory, rank) for memory, rank in matched if memory.memory_id not in hidden]


__all__ = [
    "RetrievalError",
    "candidate_matches_query",
    "filter_by_scope",
    "fulltext_score",
    "inherited_scopes",
    "matching_index_terms",
    "memory_scope_rank",
    "query_terms",
    "scope_parents",
]
