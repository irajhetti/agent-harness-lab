"""Pydantic models for everything a model is allowed to say.

A model reply that does not validate against one of these is never acted on.
Extra keys are forbidden, so a model cannot smuggle in a field such as a rerun command.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FileEdit(BaseModel):
    """Replace one whole file. Whether the path is allowed is decided in guardrails.py, not here."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    new_content: str = Field(max_length=200_000)


class FixProposal(BaseModel):
    """The fixer's answer. Ports FixReport in heal.ts, plus the edits themselves."""

    model_config = ConfigDict(extra="forbid")

    fixable: bool = True
    cause: str = Field(min_length=1, max_length=2_000)
    rationale: str = Field(max_length=4_000)
    edits: list[FileEdit] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def fixable_needs_edits(self) -> "FixProposal":
        if self.fixable and not self.edits:
            raise ValueError("a fixable proposal needs at least one edit")
        return self


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    CHANGES = "CHANGES"
    ESCALATE = "ESCALATE"


class Review(BaseModel):
    """The reviewer's answer. It has no edits field: the reviewer can only judge."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    notes: str = Field(max_length=4_000)
