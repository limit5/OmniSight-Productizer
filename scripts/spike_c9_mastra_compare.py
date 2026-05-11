#!/usr/bin/env python3
"""C9 Mastra Observational Memory bake-off harness (OP-859).

The harness is read-only and deterministic by default. It can verify that the
Mastra npm packages are visible, ingest B9-style ``progress.txt`` JSONL plus a
compact JIRA changelog export, and compare C1/C3/Mastra on Recall@K, token
cost, and setup complexity. No live model call or production adapter is used.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("spike_c9_mastra")


class MastraInstallFailed(RuntimeError):
    """Vendor package check failed; spike can still run in modeled mode."""


class MastraIngestionTimeout(RuntimeError):
    """Modeled ingestion exceeded the allowed sample size."""


class MastraQuerySchemaMismatch(RuntimeError):
    """Modeled vendor recall response did not match the expected schema."""


PRICE_PER_M_INPUT = 1.00
PRICE_PER_M_OUTPUT = 5.00
MASTRA_OBSERVER_TOKENS = 2_620
MASTRA_QUERY_TOKENS = (1_100, 160)
MEMORY_QUERY_TOKENS = (260, 80)
COGNEE_QUERY_TOKENS = (420, 120)
DEFAULT_RECALL_K = 3
ONE_WEEK_DAYS = 7
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "the", "to", "with", "without",
}


@dataclass(frozen=True)
class MemoryRecord:
    ticket_key: str
    kind: str
    text: str
    created_at: datetime
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuerySpec:
    id: str
    query: str
    relevant_ticket: str


@dataclass
class RecallHit:
    ticket_key: str
    score: float
    evidence: str


@dataclass
class AdapterResult:
    adapter: str
    recall_at_k: float
    token_cost_usd_per_query: float
    setup_loc: int
    infra: list[str]
    ingestion_records: int
    notes: list[str] = field(default_factory=list)


@dataclass
class Corpus:
    records: list[MemoryRecord]
    queries: list[QuerySpec]
    source: str


def built_in_corpus(now: datetime | None = None) -> Corpus:
    """Five-ticket sample covering the "find relevant prior ticket" task."""

    base = now or datetime(2026, 5, 11, tzinfo=timezone.utc)
    rows = [
        ("OP-830", "progress",
         "B3 added a 3x loop detector and context reset. Same tool, same "
         "args hash, same error class triggers reset_required.",
         ("loop-detector", "context-reset", "B3"), 6),
        ("OP-843", "changelog",
         "Outcomes spike compared B3 with a grader. Recommendation was "
         "complement, not replace, because false success and tool loops are "
         "different failure classes.",
         ("outcomes", "vendor-claim", "spike"), 5),
        ("OP-847", "progress",
         "B16 integrated Outcomes on the final attempt only. Rubric Goodhart "
         "guards warn when AC text is too thin.",
         ("outcomes", "rubric", "goodhart"), 4),
        ("OP-848", "changelog",
         "Lessons learned BM25 auto-injection surfaces relevant prior "
         "operator lessons before runner pickup.",
         ("lessons", "bm25", "memory"), 3),
        ("OP-850", "progress",
         "B12 reflection loop retries after test or lint failure with a "
         "bounded reflection prompt and progress.txt accounting.",
         ("reflection-loop", "lint", "tests"), 2),
    ]
    records = [
        MemoryRecord(key, kind, text, base - timedelta(days=age), tags)
        for key, kind, text, tags, age in rows
    ]
    queries = [
        QuerySpec("Q1-loop-reset",
                  "find prior ticket about same tool args error class reset",
                  "OP-830"),
        QuerySpec("Q2-vendor-claim",
                  "find prior ticket where vendor grader claim should "
                  "complement rather than replace existing resilience",
                  "OP-843"),
        QuerySpec("Q3-goodhart",
                  "find prior ticket about thin acceptance criteria causing "
                  "rubric Goodhart risk",
                  "OP-847"),
        QuerySpec("Q4-lessons-bm25",
                  "find prior ticket about BM25 lessons learned injection",
                  "OP-848"),
        QuerySpec("Q5-reflection",
                  "find prior ticket about reflection after test lint failure",
                  "OP-850"),
    ]
    return Corpus(records=records, queries=queries, source="built-in-sample")


def _parse_dt(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return fallback
    return fallback


def _first_text(payload: dict[str, Any]) -> str:
    for key in ("summary", "message", "text", "content", "observation", "event"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def load_progress_txt(path: Path, *, now: datetime | None = None) -> list[MemoryRecord]:
    fallback = now or datetime.now(timezone.utc)
    records: list[MemoryRecord] = []
    if not path.exists():
        return records
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("progress.txt line %s is not JSON; skipped", lineno)
            continue
        key = str(
            payload.get("ticket_key") or payload.get("ticket") or payload.get("key")
            or payload.get("issue") or "UNKNOWN"
        )
        tags = tuple(str(t) for t in payload.get("tags", ()) if str(t).strip())
        records.append(MemoryRecord(
            key, "progress", _first_text(payload),
            _parse_dt(payload.get("created_at") or payload.get("ts"), fallback),
            tags,
        ))
    return records


def load_jira_changelogs(path: Path, *, now: datetime | None = None) -> list[MemoryRecord]:
    fallback = now or datetime.now(timezone.utc)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    issues = raw.get("issues", raw) if isinstance(raw, dict) else raw
    records: list[MemoryRecord] = []
    for issue in issues:
        key = str(issue.get("key") or issue.get("ticket_key") or "UNKNOWN")
        changelog = issue.get("changelog", {})
        histories = changelog.get("histories", changelog) if isinstance(changelog, dict) else changelog
        for entry in histories if isinstance(histories, list) else []:
            items = entry.get("items") if isinstance(entry.get("items"), list) else [entry]
            parts = [
                f"{item.get('field', 'field')}: "
                f"{item.get('fromString') or item.get('from') or ''} -> "
                f"{item.get('toString') or item.get('to') or ''}".strip()
                for item in items
            ]
            if parts:
                records.append(MemoryRecord(
                    key, "jira_changelog", "; ".join(parts),
                    _parse_dt(entry.get("created") or entry.get("created_at"), fallback),
                    ("jira", "changelog"),
                ))
    return records


def load_corpus(
    *,
    progress_path: Path | None,
    jira_changelogs_path: Path | None,
    sample_days: int = ONE_WEEK_DAYS,
) -> Corpus:
    base = built_in_corpus()
    records: list[MemoryRecord] = []
    if progress_path:
        records.extend(load_progress_txt(progress_path))
    if jira_changelogs_path:
        records.extend(load_jira_changelogs(jira_changelogs_path))
    if not records:
        return base
    latest = max(r.created_at for r in records)
    cutoff = latest - timedelta(days=sample_days)
    filtered = [r for r in records if r.created_at >= cutoff] or records
    return Corpus(filtered, base.queries, "artifact-sample")


def _tokens(text: str) -> list[str]:
    return [
        tok.lower()
        for tok in TOKEN_RE.findall(text)
        if tok.lower() not in STOPWORDS and len(tok) > 1
    ]


def _overlap_score(query: str, text: str, tags: Iterable[str] = ()) -> float:
    q = Counter(_tokens(query))
    doc = Counter(_tokens(text + " " + " ".join(tags)))
    if not q or not doc:
        return 0.0
    return sum(min(q[t], doc[t]) for t in q) / math.sqrt(
        sum(q.values()) * sum(doc.values())
    )


class BaseAdapter:
    name = "base"
    setup_loc = 0
    infra: list[str] = []
    query_tokens = (0, 0)

    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []

    def ingest(self, records: list[MemoryRecord]) -> None:
        self.records = list(records)

    def query(self, query: str, *, k: int) -> list[RecallHit]:
        raise NotImplementedError

    def cost_per_query_usd(self) -> float:
        in_tok, out_tok = self.query_tokens
        return (
            in_tok / 1_000_000 * PRICE_PER_M_INPUT
            + out_tok / 1_000_000 * PRICE_PER_M_OUTPUT
        )


class MemoryToolAdapter(BaseAdapter):
    name = "c1_memory_tool"
    setup_loc = 90
    infra = ["progress.txt", "filesystem"]
    query_tokens = MEMORY_QUERY_TOKENS

    def query(self, query: str, *, k: int) -> list[RecallHit]:
        hits = [
            RecallHit(r.ticket_key, _overlap_score(query, r.text, r.tags), r.text)
            for r in self.records
        ]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]


class CogneeAdapter(MemoryToolAdapter):
    name = "c3_cognee"
    setup_loc = 260
    infra = ["cognee", "code graph", "vector index"]
    query_tokens = COGNEE_QUERY_TOKENS

    def query(self, query: str, *, k: int) -> list[RecallHit]:
        q_tokens = set(_tokens(query))
        hits = []
        for r in self.records:
            tag_bonus = 0.08 * len(q_tokens.intersection(_tokens(" ".join(r.tags))))
            score = _overlap_score(query, r.text, r.tags) + tag_bonus
            hits.append(RecallHit(r.ticket_key, score, r.text))
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]


class MastraOMAdapter(MemoryToolAdapter):
    name = "mastra_observational_memory"
    setup_loc = 390
    infra = ["node sidecar", "@mastra/memory", "@mastra/libsql", "REST wrapper"]
    query_tokens = MASTRA_QUERY_TOKENS

    def __init__(self, *, timeout_record_limit: int = 500) -> None:
        super().__init__()
        self.timeout_record_limit = timeout_record_limit

    def ingest(self, records: list[MemoryRecord]) -> None:
        if len(records) > self.timeout_record_limit:
            raise MastraIngestionTimeout(
                f"record count {len(records)} exceeds limit {self.timeout_record_limit}"
            )
        self.records = [
            MemoryRecord(
                r.ticket_key, "observation",
                f"{r.created_at.date()} {r.ticket_key}: "
                f"{' '.join(r.text.split())[:220]} Tags: {', '.join(r.tags)}",
                r.created_at, r.tags + ("mastra-om",),
            )
            for r in records
        ]

    def query(self, query: str, *, k: int) -> list[RecallHit]:
        hits = super().query(query, k=k)
        if any(not isinstance(h.ticket_key, str) for h in hits):
            raise MastraQuerySchemaMismatch("recall hit missing string ticket_key")
        return hits

    def cost_per_query_usd(self) -> float:
        return super().cost_per_query_usd() + (
            MASTRA_OBSERVER_TOKENS / 1_000_000 * PRICE_PER_M_INPUT
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def verify_mastra_install(runner: Runner = subprocess.run) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("@mastra/memory", "@mastra/core", "mastra"):
        proc = runner(
            ["npm", "view", package, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise MastraInstallFailed(
                f"{package}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        versions[package] = proc.stdout.strip()
    return versions


def recall_at_k(adapter: BaseAdapter, queries: list[QuerySpec], *, k: int) -> float:
    if not queries:
        return 0.0
    return sum(
        any(hit.ticket_key == q.relevant_ticket for hit in adapter.query(q.query, k=k))
        for q in queries
    ) / len(queries)


def evaluate(corpus: Corpus, *, k: int = DEFAULT_RECALL_K) -> dict[str, Any]:
    results: list[AdapterResult] = []
    for adapter in [MemoryToolAdapter(), CogneeAdapter(), MastraOMAdapter()]:
        notes: list[str] = []
        try:
            adapter.ingest(corpus.records)
        except MastraIngestionTimeout:
            latest = max(r.created_at for r in corpus.records)
            cutoff = latest - timedelta(days=1)
            adapter.ingest([r for r in corpus.records if r.created_at >= cutoff])
            notes.append("MastraIngestionTimeout: reduced sample to 1 day")
        results.append(AdapterResult(
            adapter.name,
            recall_at_k(adapter, corpus.queries, k=k),
            adapter.cost_per_query_usd(),
            adapter.setup_loc,
            list(adapter.infra),
            len(adapter.records),
            notes,
        ))
    return {
        "source": corpus.source,
        "records": len(corpus.records),
        "queries": [asdict(q) for q in corpus.queries],
        "k": k,
        "results": [asdict(r) for r in results],
        "decision": recommend(results),
    }


def recommend(results: list[AdapterResult]) -> str:
    by_name = {r.adapter: r for r in results}
    mastra = by_name["mastra_observational_memory"]
    c1 = by_name["c1_memory_tool"]
    c3 = by_name["c3_cognee"]
    best = max(r.recall_at_k for r in results)
    if mastra.recall_at_k >= best and mastra.setup_loc <= c1.setup_loc + c3.setup_loc:
        return "complement"
    if mastra.recall_at_k > max(c1.recall_at_k, c3.recall_at_k):
        return "complement"
    if mastra.recall_at_k >= best and mastra.token_cost_usd_per_query < c3.token_cost_usd_per_query:
        return "wholesale-replace"
    return "reject"


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="spike_c9_mastra_compare")
    parser.add_argument("--progress", type=Path, default=None)
    parser.add_argument("--jira-changelogs", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("data/op-859-c9-mastra.json"))
    parser.add_argument("--recall-k", type=int, default=DEFAULT_RECALL_K)
    parser.add_argument("--verify-install", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_cli(argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    corpus = load_corpus(
        progress_path=args.progress,
        jira_changelogs_path=args.jira_changelogs,
    )
    result = evaluate(corpus, k=args.recall_k)
    if args.verify_install:
        result["mastra_install"] = verify_mastra_install()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    compact = {
        row["adapter"]: (
            f"R@{args.recall_k}={row['recall_at_k']:.0%}, "
            f"cost=${row['token_cost_usd_per_query']:.4f}, "
            f"setup={row['setup_loc']}loc"
        )
        for row in result["results"]
    }
    print(
        f"[OP-859 spike] source={result['source']} records={result['records']} "
        f"decision={result['decision']} metrics={compact} "
        f"(harness {time.monotonic() - started:.2f}s) -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
