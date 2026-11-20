"""Prompts for the two roles. They explain the rules to the model, but they do not enforce them.

Enforcement lives in schemas.py (shape), guardrails.py (paths) and graph.py (rounds, approval).
"""

from .config import HealConfig


def fixer_prompt(cfg: HealConfig, evidence: str, files: str, previous: str, notes: str) -> str:
    revision = ""
    if notes:
        # heal.ts resumes the fixer's own session here. This port has no sessions, so it restates.
        revision = (
            "\n## Your previous proposal\n" + previous + "\n\nThe reviewer did not approve it. "
            "Address these notes and report again in the same JSON format:\n" + notes + "\n"
        )
    return f"""You are the on-call fixer for a scheduled job. The job `{cfg.job}` failed.
Propose the smallest change that fixes the ROOT CAUSE so the next run succeeds.

Rules:
- You may only replace whole files matching: {", ".join(cfg.allow)}
- Never touch: {", ".join(cfg.deny)}
- If it cannot be fixed in code (it needs a login, a credential or a human decision), set
  "fixable" to false, give the reason in "cause" and send no edits.
- A reviewer will read your diff. A person will read your cause. Be exact.

## Last error log lines
{evidence or "(empty)"}

## Files you may edit
{files or "(none)"}
{revision}
End your reply with exactly one JSON block:
```json
{{"fixable": true, "cause": "one sentence", "rationale": "what you changed and why",
  "edits": [{{"path": "relative/path.py", "new_content": "the whole new file"}}]}}
```"""


def reviewer_prompt(cfg: HealConfig, evidence: str, proposal: str, diff: str) -> str:
    return f"""You are reviewing a fix another agent proposed for the scheduled job `{cfg.job}`.
You cannot change anything. Decide whether this diff may go forward to a person for approval.

APPROVE only if it addresses the cause shown in the evidence, is minimal, and touches nothing
it should not. CHANGES with concrete notes if it is on the right track but wrong in a specific
way. ESCALATE if a person must decide, or the evidence does not support the cause.

## Evidence
{evidence or "(empty)"}

## Fixer's report
{proposal}

## Diff
{diff}

End with exactly one JSON block:
```json
{{"verdict": "APPROVE", "notes": "one or two sentences"}}
```"""
