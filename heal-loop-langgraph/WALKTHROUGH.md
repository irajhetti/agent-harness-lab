# Walkthrough

Written to be said out loud.

## What it is

This is a repair loop for a scheduled job that has failed. Two models take part, a fixer and a
reviewer. Neither can act. They return data, and code decides what happens with it. I ported the
control flow from a TypeScript script I run in a private system. The port is Python on LangGraph.

## What happens, step by step

**1. Gather evidence.** The loop reads the end of the job's error log. Four rules can stop the run
before any model is called. The error looks like a login problem, which a model cannot fix. The
same error was worked on in the last day. The job was fixed three times this week, so something
deeper is wrong. The daily budget of model calls is spent. Otherwise it cuts a git worktree on a
new branch. All changes happen there, never in the live workspace.

**2. Fixer.** The fixer gets the evidence and the files it may edit. It must answer with JSON: a
cause, a rationale and a list of whole-file edits. Pydantic validates it. An invalid reply gets one
retry with the validation error. A second invalid reply escalates. I never repair a reply.

**3. Apply in the worktree.** Code checks each path before writing. No absolute paths. No `..`.
Nothing in `.git`. Nothing that goes through a symlink. Nothing on the deny-list, such as `.env`
files, keys, CI files and the job script. Only paths on the allow-list. One bad path refuses the
whole proposal. Then the edits are committed and the diff is taken.

**4. Reviewer.** A second model reads the diff and answers APPROVE, CHANGES or ESCALATE. Its schema
has no edits field, so it cannot change anything. If its answer cannot be read after one retry, the
verdict is ESCALATE. An unclear answer is never a yes.

**5. The loop.** CHANGES sends the notes back to the fixer. There are two rounds at most. A second
CHANGES escalates.

**6. Human approval.** On APPROVE the graph calls `interrupt()`. The checkpointer saves the state
and the run stops. A person sees the cause, the reviewer's notes and the diff. Only the exact word
`approve` continues. Anything else is a rejection.

**7. Merge and rerun.** Code merges the commit id that was reviewed, not the branch name, so
nothing added later can ride along. Then it runs the job with the command from configuration.

**8. Revert.** If the job still fails, the merge commit is reverted.

**9. Finish.** Every path writes one line to a ledger and removes the worktree. The branch is kept
unless the fix held, so a person can read what was tried.

## Why each control exists

- Schemas: a model's reply is untrusted input, like a web form.
- Path rules in code: a prompt is a request, not a control. The model may ignore it.
- Protected job file: otherwise the cheapest "fix" is to delete the check.
- Fixed rerun command: the model must never choose what gets executed.
- Round limit and call budget: two models can disagree for ever.
- Human gate: the reviewer is also a model. It can be wrong in the same way as the fixer.
- Revert: approval is a prediction. The rerun is the evidence.

## What could go wrong

- The rerun executes repository code with my user's rights. This port has no sandbox. The original
  uses bubblewrap. A real deployment needs one.
- A fix can pass the job and still be wrong. The job is the only test.
- Protecting the job file does not protect the helpers it imports. The allow-list must be drawn
  with care.
- There is no lock. Two runs for one job could race.
- A person can approve without reading. The gate only helps if the diff is small.
- I have only run it with a scripted model. A real local model will fail validation more often,
  and that will show up as escalations.
