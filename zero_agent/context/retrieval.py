"""Deterministic lexical matching with Chinese substring and n-gram fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


@dataclass(frozen=True)
class LexicalMatch:
    score: float
    terms: tuple[str, ...]
    positions: tuple[tuple[int, int], ...]


def match_text(query: str, text: str) -> LexicalMatch | None:
    """Score exact words, Chinese n-grams, and substring matches."""

    normalized = query.casefold().strip()
    folded_text = text.casefold()
    terms = _query_terms(normalized)
    if not terms:
        return None
    matches = [
        (term, folded_text.find(term))
        for term in terms
    ]
    matches = [(term, start) for term, start in matches if start >= 0]
    if not matches:
        return None
    coverage = len(matches) / len(terms)
    all_chinese = all(_CJK.fullmatch(term) for term in terms)
    if coverage < 1 and (not all_chinese or coverage < 0.5):
        return None
    positions = tuple((start, start + len(term)) for term, start in matches)
    density = sum(end - start for start, end in positions) / max(1, len(text))
    return LexicalMatch(
        score=round(coverage * 10 + density, 6),
        terms=tuple(term for term, _ in matches),
        positions=positions,
    )


def _query_terms(query: str) -> tuple[str, ...]:
    terms = list(_WORD.findall(query))
    for run in _CJK.findall(query):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(dict.fromkeys(terms))
