# heal-loop

A small agent loop that tries to repair a failed scheduled job, with hard limits on what it may do.

A FIXER model proposes a change. The change is applied in a throwaway git worktree. A REVIEWER
model judges the diff. A person approves or rejects. Only then is the change merged and the job
run again. If the job still fails, the merge is reverted.

It is a Python and LangGraph port of the control flow of a TypeScript script, `scripts/heal.ts`,
that runs in a private codebase. The port is generic. It contains no names, hosts or secrets from
that codebase.

Built with AI coding agents under the owner's direction as a demonstration project.

## Run it

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and git.

```bash
uv sync
uv run pytest          # 83 tests, offline, under a minute
uv run ruff check
uv run mypy            # strict
```

The demo creates a toy repository with a failing job, runs the loop with a scripted model and
stops at the approval gate. A second command gives the decision.

```bash
uv run python -m heal_loop start --dir /tmp/heal-demo
uv run python -m heal_loop resume --dir /tmp/heal-demo --approve     # or --reject
```

`start --bad-fix` scripts a fix that does not work, so you can watch the revert.
`--dir` must be new or empty. The paused run is stored in `checkpoints.sqlite` inside it, which is
why the resume can be a separate process.

No model is installed or downloaded. Every test and the demo use `ScriptedModel`, which replays
fixed replies. If `OLLAMA_BASE_URL` is set, the CLI uses `OllamaModel` instead: plain HTTP to a
local Ollama-compatible `/api/chat` endpoint (`OLLAMA_MODEL` picks the model). There is no other
provider and no API key anywhere.

## The graph

```
gather_evidence -> fixer -> apply_in_worktree -> reviewer -+-> human_approval -> merge_and_rerun -> finish
                     ^                                     |         |                |
                     +------------- CHANGES ---------------+       reject        rerun failed -> revert -> finish
                                                           |
any node that needs a person ------------------------------+-> escalate -> finish
```

| Node | What it does | Part of `scripts/heal.ts` it ports |
|---|---|---|
| `gather_evidence` | Reads the tail of the error log. Applies the pre-flight rules. Cuts a worktree and branch. | `gatherEvidence` (lines 91-103), `lastError` (41-46), `fingerprint` (49-52), `escalateReason` (55-65) and its use in `main` (207-211), worktree setup in `main` (213-219) |
| `fixer` | Asks the fixer model for a `FixProposal`. Validates it. Retries once. | `fixerPrompt` (138-164), `lastJson` (68-73), the fixer half of the loop in `main` (236-241) |
| `apply_in_worktree` | Checks every path, writes the edits into the worktree, commits, takes the diff. | In the original the fixer agent edits and commits by itself, held in by the sandbox in the agent runner `claude` (110-126) and by `FIXER_TOOLS` and `DENY` (128-133). The diff and the "changed nothing" check are `main` (242-243) |
| `reviewer` | Asks the reviewer model for a `Review`. An unreadable verdict becomes ESCALATE. | `reviewerPrompt` (166-186), `REVIEWER_TOOLS` (130), `main` (244-246) |
| edge after `reviewer` | APPROVE goes to the gate. CHANGES goes back to the fixer while rounds remain. Anything else escalates. | The `for` loop bound and `break` in `main` (235, 247), and line 251 |
| `human_approval` | `interrupt()`. The run stops until a person resumes it. | Not in the original, which merges as soon as the reviewer approves. The nearest thing is `HEAL_NO_DEPLOY` (252) |
| `merge_and_rerun` | Merges the reviewed commit with `--no-ff`. Aborts a failed merge. Runs the configured job command. | `main` (256-259) and the rerun (267-268) |
| `revert` | Reverts the merge commit. | The `revert` closure (261) |
| `escalate` | Records why a person is needed. | The `escalate` closure (228) |
| `finish` | Writes the ledger line. Removes the worktree. Deletes the branch only when the fix held. | The `finish` closure (221-227), `record` (85-88) |

Where the code lives:

| File | Holds |
|---|---|
| `src/heal_loop/graph.py` | State, the nine nodes, the edges, `start` and `resume` |
| `src/heal_loop/schemas.py` | Pydantic models for the two model outputs |
| `src/heal_loop/guardrails.py` | Path rules for edits |
| `src/heal_loop/preflight.py` | Rules that skip the agents, and the ledger |
| `src/heal_loop/models.py` | `Model` protocol, `ScriptedModel`, `OllamaModel`, JSON parsing, validate and retry once |
| `src/heal_loop/config.py` | `HealConfig`: workspace, rerun command, allow and deny lists, limits |
| `src/heal_loop/hooks.py` | No-op tracing seam |
| `src/heal_loop/fixture.py` | The toy broken job |
| `src/heal_loop/cli.py` | The demo CLI |

## The controls, and where each one is enforced

- **Model output is data.** A reply must parse into `FixProposal` or `Review`. Extra fields are
  refused. A bad reply gets one retry with the validation error. A second bad reply escalates.
- **An unreadable verdict is never approval.** It becomes ESCALATE (`reviewer` in `graph.py`).
- **Edits are fenced in.** Relative paths only, no `..`, nothing in `.git`, nothing that is or
  passes through a symlink, nothing on the deny-list, and only paths on the allow-list
  (`guardrails.py`). One refused edit refuses the whole proposal.
- **The job that defines success is protected.** The fixture puts `job.py` on the deny-list, so the
  fixer cannot make the job pass by rewriting the job.
- **The rerun command comes from `HealConfig`.** The schema has no field for a command.
- **The loop is bounded.** `max_rounds` is 2 fixer and reviewer passes in total, as in the original.
- **A person approves before any merge.** Anything but an exact `approve` is a rejection.
- **What was reviewed is what is merged.** The merge takes the commit id recorded at review time,
  not the branch name.
- **A failed rerun is undone.** The merge commit is reverted.
- **Some failures never reach a model.** A log that looks like a login problem, the same error
  already worked on in the last 24 hours, three fixes for one job in a week, or the daily call
  budget spent.

## What is deliberately NOT done

- **There is no sandbox here.** The original runs each agent under bubblewrap (`bwrap`): the file
  system is read-only except the worktree, secret directories are masked, the environment is
  scrubbed, and the agent has a tool allow-list and deny rules. This port does not need one for the
  models, because the models here have no tools. They only return text. But the rerun executes the
  repository's own code with your user's full rights. Treat that as untrusted code.
- **Not for production use.** It is a demonstration of the control flow. It has been run only
  against the toy fixture.
- **Local or scripted model only.** `OllamaModel` is tested against a stub HTTP server in the test
  suite. It has not been run against a real Ollama server, so real-model behaviour is unknown.
- **No agent session.** The original sends the reviewer's notes back into the fixer's own session.
  Here the second prompt restates the previous proposal and the notes.
- **No real tools for the fixer.** It sees the allow-listed files pasted into the prompt (capped at
  20,000 characters) and replaces whole files.
- **Budget is a call count, not money.** The original caps dollars per run and per day.
- **No lock.** The original keeps one healer per job with a lock file (lines 194-199).
- **No notifications, no deploy steps, no incident log.** The original reports to a chat channel,
  ships files to a second machine, rebuilds a server and appends to an incidents file.
- **No tracing yet.** `hooks.py` is the seam for it.

## Differences from the original worth knowing

- The human approval gate is new. The original is fully automatic after the reviewer's APPROVE.
- The original escalates at once on an unreadable reviewer verdict. This port retries once first,
  then escalates.
- The original lets the agent edit files and commit. Here the model proposes and code applies.
- A model transport error (for example Ollama is not running) raises an exception and stops the
  run. The worktree is left behind for inspection, and a new run needs a new run id. The original
  also stops, and alerts a person.

See `WALKTHROUGH.md` for the spoken version and `REVIEW-CHECKLIST.md` for how to check the claims.
