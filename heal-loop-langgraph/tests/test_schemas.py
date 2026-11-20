"""Pydantic models reject malformed model output."""

from typing import Any

import pytest
from pydantic import ValidationError

from heal_loop.fixture import proposal, review
from heal_loop.schemas import FixProposal, Review, Verdict


def test_valid_output_parses() -> None:
    assert FixProposal.model_validate(proposal()).edits[0].path == "app/report.py"
    assert Review.model_validate(review()).verdict is Verdict.APPROVE


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "just text",
        {"cause": "x"},  # no rationale
        proposal() | {"edits": []},  # fixable but nothing to apply
        proposal() | {"edits": [{"path": "", "new_content": "x"}]},  # empty path
        proposal() | {"edits": [{"path": "a.py"}]},  # no content
        proposal() | {"edits": [{"path": f"app/f{i}.py", "new_content": ""} for i in range(11)]},  # too many
        proposal() | {"rerun_command": ["true"]},  # the model may not add fields
        proposal() | {"cause": ""},
    ],
)
def test_malformed_proposals_are_rejected(bad: Any) -> None:
    with pytest.raises(ValidationError):
        FixProposal.model_validate(bad)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"verdict": "LGTM", "notes": "x"},  # not one of the three verdicts
        {"verdict": "approve", "notes": "x"},  # wrong case is not guessed at
        {"verdict": "APPROVE"},  # no notes
        {"verdict": "APPROVE", "notes": "x", "merge_now": True},  # extra field
    ],
)
def test_malformed_reviews_are_rejected(bad: Any) -> None:
    with pytest.raises(ValidationError):
        Review.model_validate(bad)


def test_not_fixable_needs_no_edits() -> None:
    report = FixProposal.model_validate({"fixable": False, "cause": "needs a login", "rationale": ""})
    assert report.edits == []
