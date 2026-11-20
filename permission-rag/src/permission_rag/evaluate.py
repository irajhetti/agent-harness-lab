"""Retrieval quality and the leak check. `make eval` writes eval/results.md from real runs."""

from __future__ import annotations

import json
import platform
from datetime import UTC, date, datetime
from importlib.metadata import version
from typing import Any, LiteralString, TypedDict

from .access import Level, User, permits
from .corpus import PROJECT_ROOT, Document, load_corpus
from .db import Cursor, owner_session, reader_session
from .embed import DIMENSIONS, MODEL_NAME
from .retrieve import MODES, Mode, Result, search

EVAL_DIR = PROJECT_ROOT / "eval"
RESULTS_FILE = EVAL_DIR / "results.md"

PROFILES: dict[str, User] = {
    "public": User(Level.PUBLIC),
    "internal": User(Level.INTERNAL),
    "internal+alpha": User(Level.INTERNAL, frozenset({"alpha"})),
    "restricted": User(Level.RESTRICTED),
    "restricted+alpha": User(Level.RESTRICTED, frozenset({"alpha"})),
    "restricted+bravo": User(Level.RESTRICTED, frozenset({"bravo"})),
    "restricted+alpha+bravo": User(Level.RESTRICTED, frozenset({"alpha", "bravo"})),
}
AUTHORISED = PROFILES["restricted+alpha+bravo"]

CorpusIndex = dict[tuple[str, int], Document]


class Question(TypedDict):
    question: str
    expected: list[str]  # document ids


def load_questions() -> list[Question]:
    data: list[Question] = json.loads((EVAL_DIR / "questions.json").read_text())
    return data


def load_adversarial() -> list[str]:
    data: list[str] = json.loads((EVAL_DIR / "adversarial_queries.json").read_text())
    return data


def corpus_index() -> CorpusIndex:
    return {(d.doc_id, d.version): d for d in load_corpus()}


def find_leaks(results: list[Result], user: User, index: CorpusIndex, today: date) -> list[Result]:
    """Results `user` should not have seen.

    Judged against the labels in the source files on disk, not the labels the database
    returned, so a wrong label in the database cannot hide a leak.
    """
    leaks = []
    for result in results:
        doc = index.get((result.doc_id, result.version))
        if doc is None or not permits(
            user, doc.level, doc.compartments, doc.valid_from, doc.valid_to, today
        ):
            leaks.append(result)
    return leaks


def leak_check(
    cur: Cursor, user: User, queries: list[str], mode: Mode, index: CorpusIndex
) -> tuple[int, list[Result]]:
    """Run every query as `user`. Returns (chunks returned, leaked chunks)."""
    returned = 0
    leaks: list[Result] = []
    for query in queries:
        results = search(cur, user, query, mode, k=10)
        returned += len(results)
        leaks += find_leaks(results, user, index, date.today())
    return returned, leaks


def doc_ranking(results: list[Result]) -> list[str]:
    """Chunk results to a ranked list of document ids, first appearance wins."""
    return list(dict.fromkeys(r.doc_id for r in results))


def metrics(rankings: list[list[str]], expected: list[set[str]]) -> dict[str, float]:
    """Document-level recall@1/3/5 and MRR, averaged over the questions.

    recall@k is the share of a question's expected documents found in the top k.
    MRR uses the rank of the first expected document, or 0 if none was retrieved.
    """
    totals = {"recall@1": 0.0, "recall@3": 0.0, "recall@5": 0.0, "mrr": 0.0}
    for ranking, wanted in zip(rankings, expected, strict=True):
        for k in (1, 3, 5):
            totals[f"recall@{k}"] += len(wanted & set(ranking[:k])) / len(wanted)
        first = next((i for i, doc in enumerate(ranking, start=1) if doc in wanted), None)
        totals["mrr"] += 1.0 / first if first else 0.0
    return {name: total / len(rankings) for name, total in totals.items()}


def _scalar(cur: Cursor, sql: LiteralString) -> Any:
    cur.execute(sql)
    row = cur.fetchone()
    assert row is not None
    return next(iter(row.values()))


def run_eval() -> str:
    """Run the quality evaluation and the leak check, write results.md, return its text."""
    questions = load_questions()
    expected = [set(q["expected"]) for q in questions]
    adversarial = load_adversarial()
    index = corpus_index()

    quality: dict[Mode, dict[str, float]] = {}
    hybrid_misses: list[str] = []
    with reader_session(AUTHORISED) as cur:
        for mode in MODES:
            rankings = [
                doc_ranking(search(cur, AUTHORISED, q["question"], mode, k=20)) for q in questions
            ]
            quality[mode] = metrics(rankings, expected)
            if mode == "hybrid":
                hybrid_misses = [
                    f"{q['question']} (expected {', '.join(sorted(want))};"
                    f" got {', '.join(got[:5])})"
                    for q, want, got in zip(questions, expected, rankings, strict=True)
                    if not want <= set(got[:5])
                ]
        pg_version = _scalar(cur, "SHOW server_version")
        pgvector_version = _scalar(
            cur, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )

    leak_rows: list[str] = []
    searches = total_returned = total_leaks = 0
    for name, user in PROFILES.items():
        with reader_session(user) as cur:
            for mode in MODES:
                returned, leaks = leak_check(cur, user, adversarial, mode, index)
                leak_rows.append(
                    f"| {name} | {mode} | {len(adversarial)} | {returned} | {len(leaks)} |"
                )
                searches += len(adversarial)
                total_returned += returned
                total_leaks += len(leaks)

    with owner_session() as cur:
        chunk_count = _scalar(cur, "SELECT count(*) FROM chunks")

    lines = [
        "# Evaluation results",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `make eval`."
        " These are the numbers from that run. Do not edit by hand.",
        "",
        "## Setup",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Embedding model | {MODEL_NAME} ({DIMENSIONS} dimensions, ONNX, CPU) |",
        f"| fastembed | {version('fastembed')} |",
        f"| onnxruntime | {version('onnxruntime')} |",
        f"| PostgreSQL | {pg_version} |",
        f"| pgvector | {pgvector_version} |",
        f"| psycopg | {version('psycopg')} |",
        f"| Python | {platform.python_version()} |",
        f"| Corpus | {len(index)} document versions, {chunk_count} chunks (synthetic) |",
        "",
        "## Retrieval quality",
        "",
        f"{len(questions)} questions from `eval/questions.json`, asked as a fully authorised"
        " user (RESTRICTED, compartments alpha and bravo). Scored per document: the chunk"
        " ranking is reduced to document ids, first appearance wins. Each method fetches 20"
        " candidate chunks.",
        "",
        "| Mode | recall@1 | recall@3 | recall@5 | MRR |",
        "|---|---|---|---|---|",
    ]
    for mode in MODES:
        m = quality[mode]
        lines.append(
            f"| {mode} | {m['recall@1']:.3f} | {m['recall@3']:.3f} | {m['recall@5']:.3f}"
            f" | {m['mrr']:.3f} |"
        )
    lines += [
        "",
        f"Questions where hybrid did not find every expected document in the top 5:"
        f" {len(hybrid_misses)}.",
    ]
    if hybrid_misses:
        lines += ["", *(f"- {miss}" for miss in hybrid_misses)]
    lines += [
        "",
        "## Leak check",
        "",
        f"{len(adversarial)} queries from `eval/adversarial_queries.json`, run for"
        f" {len(PROFILES)} user profiles in {len(MODES)} modes, top 10 chunks each. A leak is a"
        " returned chunk whose source file says the profile may not see it: level too high,"
        " a compartment the user lacks, or outside its validity dates.",
        "",
        "| Profile | Mode | Queries | Chunks returned | Leaks |",
        "|---|---|---|---|---|",
        *leak_rows,
        "",
        f"Total: {searches} searches, {total_returned} chunks returned, {total_leaks} leaks.",
        "",
        "## Read this before quoting the numbers",
        "",
        "- The questions and the corpus were written by the same author. That flatters every"
        " method.",
        f"- {len(questions)} questions is a small set. One question moves recall by about"
        f" {100 / len(questions):.1f} points, so small gaps between modes mean nothing.",
        "- The corpus is synthetic and tiny. At this size PostgreSQL can scan every row, so"
        " these numbers say nothing about the HNSW index at scale.",
        "- The questions were written once and not tuned after seeing results.",
        "",
    ]
    text = "\n".join(lines)
    RESULTS_FILE.write_text(text)
    return text
