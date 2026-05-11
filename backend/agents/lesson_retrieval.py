"""OP-848 BM25 retrieval for prior runner lessons."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

LESSON_PROMPT_HEADER = "## Relevant prior lessons"
BM25_INDEX_STALE = "bm25_index_stale"
LESSONS_DIR_UNREADABLE = "lessons_dir_unreadable"
DEFAULT_TOP_K = 3
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class LessonSearchResult:
    path: Path
    text: str
    score: float


@dataclass(frozen=True)
class _Doc:
    path: Path
    text: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class LessonBM25Index:
    lessons_dir: Path
    built_at_mtime: float
    documents: tuple[_Doc, ...]
    document_frequency: dict[str, int]
    average_document_length: float

    @classmethod
    def build(cls, lessons_dir: Path) -> "LessonBM25Index | None":
        try:
            if not lessons_dir.is_dir():
                log.warning(
                    "%s: %s is not a directory",
                    LESSONS_DIR_UNREADABLE,
                    lessons_dir,
                )
                return None
            documents, newest_mtime = [], 0.0
            for path in sorted(lessons_dir.glob("L-*.md")):
                stat = path.stat()
                newest_mtime = max(newest_mtime, stat.st_mtime)
                text = path.read_text(encoding="utf-8")
                documents.append(_Doc(path, text, tuple(_tokenize(text))))
        except OSError as exc:
            log.warning("%s: %s", LESSONS_DIR_UNREADABLE, exc)
            return None

        document_frequency: dict[str, int] = {}
        for document in documents:
            for term in set(document.terms):
                document_frequency[term] = document_frequency.get(term, 0) + 1
        total_terms = sum(len(document.terms) for document in documents)
        average_document_length = total_terms / len(documents) if documents else 0.0
        log.info("lesson BM25 index built: documents=%s", len(documents))
        return cls(
            lessons_dir,
            newest_mtime,
            tuple(documents),
            document_frequency,
            average_document_length,
        )

    def is_stale(self) -> bool:
        try:
            return any(
                path.stat().st_mtime > self.built_at_mtime
                for path in self.lessons_dir.glob("L-*.md")
            )
        except OSError as exc:
            log.warning("%s: %s", LESSONS_DIR_UNREADABLE, exc)
            return False

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
    ) -> tuple[LessonSearchResult, ...]:
        terms = tuple(_tokenize(query))
        if not terms or not self.documents:
            log.info(
                "lesson BM25 retrieval empty: query_terms=%s documents=%s",
                len(terms),
                len(self.documents),
            )
            return ()
        scored = (
            LessonSearchResult(doc.path, doc.text, self._score(doc, terms))
            for doc in self.documents
        )
        matches = tuple(
            result
            for result in sorted(
                scored,
                key=lambda item: (-item.score, str(item.path)),
            )[:top_k]
            if result.score > 0
        )
        if not matches:
            log.info("lesson BM25 retrieval empty: top_k=%s", top_k)
        return matches

    def _score(self, document: _Doc, query_terms: tuple[str, ...]) -> float:
        k1, b = 1.5, 0.75
        term_frequency: dict[str, int] = {}
        for term in document.terms:
            term_frequency[term] = term_frequency.get(term, 0) + 1

        score, doc_count = 0.0, len(self.documents)
        avg_len = self.average_document_length or 1.0
        for term in query_terms:
            frequency = term_frequency.get(term, 0)
            if frequency == 0:
                continue
            containing_docs = self.document_frequency.get(term, 0)
            idf = math.log(
                1 + (doc_count - containing_docs + 0.5) / (containing_docs + 0.5)
            )
            numerator = frequency * (k1 + 1)
            denominator = frequency + k1 * (
                1 - b + b * (len(document.terms) / avg_len)
            )
            score += idf * (numerator / denominator)
        return score


_INDEX: LessonBM25Index | None = None


def build_index(lessons_dir: Path) -> LessonBM25Index | None:
    global _INDEX
    _INDEX = LessonBM25Index.build(lessons_dir)
    return _INDEX


def retrieve_lessons(
    lessons_dir: Path,
    *,
    ticket_title: str,
    acceptance_criteria: str,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[LessonSearchResult, ...]:
    global _INDEX
    try:
        if _INDEX is None or _INDEX.lessons_dir != lessons_dir:
            _INDEX = LessonBM25Index.build(lessons_dir)
        elif _INDEX.is_stale():
            log.info("%s: rebuilding lesson BM25 index", BM25_INDEX_STALE)
            _INDEX = LessonBM25Index.build(lessons_dir)
        if _INDEX is None:
            return ()
        return _INDEX.search(
            f"{ticket_title}\n\n{acceptance_criteria}",
            top_k=top_k,
        )
    except Exception as exc:  # pragma: no cover - defensive degradation path
        log.warning("lesson BM25 index unavailable: %s", exc)
        return ()


def build_lessons_system_message(results: tuple[LessonSearchResult, ...]) -> str:
    if not results:
        return ""
    blocks = [LESSON_PROMPT_HEADER]
    blocks.extend(
        f"### {result.path.name}\n\n{result.text.strip()}"
        for result in results
    )
    return "\n\n".join(blocks)


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]
