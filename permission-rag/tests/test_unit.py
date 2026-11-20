"""Unit tests. No database needed."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from permission_rag.access import Level, User, parse_compartments, permits
from permission_rag.chunking import chunk_text
from permission_rag.corpus import load_corpus, parse_document
from permission_rag.embed import to_pgvector
from permission_rag.evaluate import load_adversarial, load_questions, metrics
from permission_rag.retrieve import rrf_merge

TODAY = date(2026, 6, 1)
START = date(2025, 1, 1)


# --- access rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_level", "doc_level", "allowed"),
    [
        (Level.PUBLIC, Level.PUBLIC, True),
        (Level.PUBLIC, Level.INTERNAL, False),
        (Level.PUBLIC, Level.RESTRICTED, False),
        (Level.INTERNAL, Level.PUBLIC, True),
        (Level.INTERNAL, Level.INTERNAL, True),
        (Level.INTERNAL, Level.RESTRICTED, False),
        (Level.RESTRICTED, Level.RESTRICTED, True),
    ],
)
def test_level_is_a_ceiling(user_level: Level, doc_level: Level, allowed: bool) -> None:
    assert permits(User(user_level), doc_level, [], START, None, TODAY) is allowed


def test_user_must_hold_every_compartment() -> None:
    alpha = User(Level.RESTRICTED, frozenset({"alpha"}))
    both = User(Level.RESTRICTED, frozenset({"alpha", "bravo"}))
    assert permits(alpha, Level.INTERNAL, ["alpha"], START, None, TODAY)
    assert not permits(alpha, Level.INTERNAL, ["bravo"], START, None, TODAY)
    assert not permits(alpha, Level.INTERNAL, ["alpha", "bravo"], START, None, TODAY)
    assert permits(both, Level.INTERNAL, ["alpha", "bravo"], START, None, TODAY)


def test_a_compartment_does_not_raise_the_level() -> None:
    user = User(Level.INTERNAL, frozenset({"alpha"}))
    assert not permits(user, Level.RESTRICTED, ["alpha"], START, None, TODAY)


def test_validity_window_is_inclusive_at_both_ends() -> None:
    user = User(Level.PUBLIC)
    assert permits(user, Level.PUBLIC, [], TODAY, TODAY, TODAY)
    assert not permits(user, Level.PUBLIC, [], date(2026, 6, 2), None, TODAY)  # not yet valid
    assert not permits(user, Level.PUBLIC, [], START, date(2026, 5, 31), TODAY)  # expired


def test_user_parse() -> None:
    user = User.parse("internal", " Alpha, bravo ,")
    assert user == User(Level.INTERNAL, frozenset({"alpha", "bravo"}))
    assert User.parse("PUBLIC").compartments == frozenset()
    with pytest.raises(ValueError, match="unknown access level"):
        User.parse("ADMIN")


@pytest.mark.parametrize("bad", ["alpha;bravo", "al pha", "x'--", "{alpha}"])
def test_odd_compartment_names_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid compartment"):
        parse_compartments(bad)


# --- chunking ------------------------------------------------------------------------------


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("One short paragraph.") == ["One short paragraph."]
    assert chunk_text("") == []


def test_paragraphs_are_packed_up_to_the_limit() -> None:
    body = "a b c\n\nd e f\n\ng h i"
    assert chunk_text(body, max_words=6) == ["a b c\n\nd e f", "g h i"]


def test_long_paragraph_is_cut_on_word_boundaries() -> None:
    body = " ".join(str(n) for n in range(25))
    chunks = chunk_text(body, max_words=10)
    assert [len(c.split()) for c in chunks] == [10, 10, 5]
    assert " ".join(chunks) == body  # nothing lost, nothing repeated


def test_no_chunk_exceeds_the_limit_and_no_word_is_lost() -> None:
    for doc in load_corpus():
        chunks = chunk_text(doc.body, max_words=80)
        assert chunks
        assert all(len(c.split()) <= 80 for c in chunks)
        assert " ".join(chunks).split() == doc.body.split()


# --- reciprocal rank fusion ----------------------------------------------------------------


def test_rrf_scores_are_sums_of_reciprocal_ranks() -> None:
    merged = dict(rrf_merge([[1, 2, 3], [3, 1]], k=60))
    assert merged[1] == pytest.approx(1 / 61 + 1 / 62)
    assert merged[3] == pytest.approx(1 / 63 + 1 / 61)
    assert merged[2] == pytest.approx(1 / 62)


def test_rrf_prefers_items_both_lists_agree_on() -> None:
    order = [item for item, _ in rrf_merge([[1, 2, 3], [4, 2, 5]])]
    assert order[0] == 2  # second in both beats first in only one


def test_rrf_single_list_keeps_its_order_and_ties_break_by_id() -> None:
    assert [item for item, _ in rrf_merge([[7, 3, 9], []])] == [7, 3, 9]
    assert [item for item, _ in rrf_merge([[5], [2]])] == [2, 5]
    assert rrf_merge([[], []]) == []


# --- corpus, evaluation helpers ------------------------------------------------------------

HEADER = "---\nid: X-1\ntitle: T\nversion: 1\n{access}compartments:\nvalid_from: 2025-01-01\n"


def test_document_without_a_valid_label_is_refused(tmp_path: Path) -> None:
    for access_line in ("", "access: SECRET\n", "access:\n"):
        path = tmp_path / "doc.md"
        path.write_text(HEADER.format(access=access_line) + "valid_to:\n---\nBody.\n")
        with pytest.raises(ValueError, match="bad or missing metadata"):
            parse_document(path, root=tmp_path)


def test_corpus_loads_with_expected_shape() -> None:
    docs = load_corpus()
    assert 30 <= len(docs) <= 45
    assert {d.level for d in docs} == set(Level)
    assert {"alpha", "bravo"} <= {c for d in docs for c in d.compartments}
    assert all(len(d.sha256) == 64 and d.version >= 1 for d in docs)
    assert sum(d.doc_id.startswith("POISON") for d in docs) >= 3


def test_eval_files_are_big_enough_and_point_at_real_documents() -> None:
    doc_ids = {d.doc_id for d in load_corpus()}
    questions = load_questions()
    assert len(questions) >= 40
    for q in questions:
        assert set(q["expected"]) <= doc_ids
    assert len(load_adversarial()) >= 30


def test_metrics() -> None:
    result = metrics([["a", "b"], ["x", "y", "b"], ["z"]], [{"a"}, {"b"}, {"q"}])
    assert result["recall@1"] == pytest.approx(1 / 3)
    assert result["recall@3"] == pytest.approx(2 / 3)
    assert result["mrr"] == pytest.approx((1 + 1 / 3 + 0) / 3)


def test_pgvector_text_form() -> None:
    assert to_pgvector([0.5, -1.0]) == "[0.5000000,-1.0000000]"
