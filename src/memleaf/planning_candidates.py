"""Lexical comparison candidates for conversation input, not search commands.

No topic names or language-specific business stopwords. Shared rare terms help
recover an existing subject even when the turn also introduces a NEW identifier.
Candidates are comparison context; relevance never authorizes an update/merge.
"""
from __future__ import annotations

from collections import Counter
import math
import re
import unicodedata


def terms(text):
    value = unicodedata.normalize("NFKC", text).casefold()
    result = set(re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", value))
    result = {t for t in result if len(t) >= 3 or any(c.isdigit() for c in t)}
    for span in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", value):
        for width in (2, 3, 4):
            result.update(span[i:i + width] for i in range(len(span) - width + 1))
    return result


class ComparisonRanker:
    def __init__(self, memories):
        self.documents = {}
        self.headings = {}
        counts = Counter()
        for key, memory in memories.items():
            heading = terms("\n".join([memory.title, *memory.tags, *memory.aliases, *memory.keywords]))
            document = heading | terms(memory.body)
            self.headings[key] = heading
            self.documents[key] = document
            counts.update(document)
        size = len(memories)
        self.weights = {term: 1 + math.log((size + 1) / (count + 1)) for term, count in counts.items()}

    def rank(self, query, keys, limit):
        query_terms = terms(query)
        scored = []
        for key in keys:
            common = self.documents[key] & query_terms
            heading_common = common & self.headings[key]
            if not common or not (any(t.isascii() or len(t) >= 3 for t in heading_common)
                                  or len(heading_common) >= 2 or any(len(t) >= 4 for t in common)):
                continue
            # Favor the subject/title without requiring its complete phrasing.
            score = sum(self.weights[t] * (2 if t in self.headings[key] else 1) for t in common)
            scored.append((score, key))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [key for _, key in scored[:limit]]
