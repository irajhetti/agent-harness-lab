# Review checklist

Checks to run yourself before you show this to anyone. Run them from this folder.
Each one says what to look for.

## It builds and passes

1. **Tests.** `uv sync && uv run pytest -q`
   Expect `107 passed` and no skips. It takes one to two minutes. Most of that is the CLI tests,
   which start real Python processes.

2. **Lint and types.** `uv run ruff check && uv run mypy`
   Expect `All checks passed!` and `Success: no issues found in 24 source files`.

3. **Versions.** `uv pip list | grep -iE "^(langgraph|pydantic) "`
   Expect LangGraph 1.2.x and pydantic 2.x. `uv.lock` pins the exact versions.

## It is offline and holds no secrets

4. **Network code.** `grep -rnE "urllib|requests|httpx|socket" src/`
   Expect hits in two files only. `src/heal_loop/models.py`, inside `OllamaModel`. And
   `src/heal_loop/jaeger.py`, which reads traces back from a local Jaeger for `make trace-demo`.
   The trace exporter is a library and is imported in one place: `from_env` in `tracing.py`.

5. **Keys and environment.** `grep -rnE "environ|API_KEY|api_key|token" src/`
   Expect two lines in `cli.py`, for `OLLAMA_BASE_URL` and `OLLAMA_MODEL`. Three lines in
   `tracing.py` that read `OTEL_` variables. Three lines about token counts, which are numbers
   reported by a model server, not credentials. Nothing else.

6. **Nothing that names a business.** Search the folder for your company, client, host and
   product names: `grep -rniE "name1|name2" --exclude-dir=.venv --exclude=uv.lock .`
   Expect no hits.

## The demo does what the README says

7. **It pauses, and nothing is merged.**
   `uv run python -m heal_loop start --dir /tmp/heal-demo`
   Expect a diff and a line starting `PAUSED:`. Then:
   `git -C /tmp/heal-demo/repo log --oneline` shows one commit, `initial`.
   `cat /tmp/heal-demo/repo/app/report.py` still has `rows[1:]`.
   `git -C /tmp/heal-demo/repo branch` shows a `heal/nightly-report-demo` branch next to `main`.

8. **Approve merges and the job passes.**
   `uv run python -m heal_loop resume --dir /tmp/heal-demo --approve`
   Expect `FIXED:`. Then `git -C /tmp/heal-demo/repo log --oneline --graph` shows a merge commit,
   `(cd /tmp/heal-demo/repo && python3 -B job.py)` prints `nightly-report ok`, and
   `cat /tmp/heal-demo/heal.jsonl` has one line with `"outcome": "fixed"` and `"model_calls": 2`.
   Run the resume again. Expect `error: run 'demo' is not waiting for approval`.

9. **Reject changes nothing.** Use a new directory. Run `start`, then `resume --reject`.
   Expect `REJECTED:`. `git -C <dir>/repo status --short` prints nothing and the log still has one
   commit. The `heal/` branch is still there, so you can read what was proposed.

10. **A bad fix is reverted.** Use a new directory. Run `start --bad-fix`, then `resume --approve`.
    Expect `REVERTED:`. The log shows a merge and then a `Revert` commit.
    `git -C <dir>/repo diff HEAD~2 HEAD` prints nothing.

## The controls are real, not decoration

11. **Break a control and watch a test fail.** Make one of these edits, run
    `uv run pytest -q tests/test_graph.py tests/test_models.py`, see failures, then undo the edit.
    - `graph.py`, in `apply_in_worktree`: change `if refused:` to `if False:`.
    - `graph.py`, in `after_review`: change `state["round"] < cfg.max_rounds` to `True`.
    - `graph.py`, in `after_review`: change `return "human_approval"` to `return "merge_and_rerun"`.
    - `graph.py`, in `revert`: delete the `git revert` line.
    - `graph.py`, in `reviewer`: change `Verdict.ESCALATE` to `Verdict.APPROVE`.
    - `models.py`, in `ask`: change `(1, 2)` to `(1,)`, then to `(1, 2, 3)`.

    If any of these leaves the tests green, the test is not doing its job.

12. **Only one road leads to the merge.** `grep -n "merge_and_rerun" src/heal_loop/graph.py`
    Expect it as a destination in one place only: the edge out of `human_approval`.
    Print the graph to confirm:
    ```bash
    uv run python -c "
    from pathlib import Path
    from heal_loop import ScriptedModel, build_graph
    from heal_loop.fixture import fixture_config
    print(build_graph(fixture_config(Path('/nonexistent')), ScriptedModel({})).get_graph().draw_mermaid())"
    ```
    Paste the output into a Mermaid viewer. Check it against the diagram in the README.

13. **The model cannot choose the command.** `grep -rn "rerun_command" src/`
    Expect `config.py`, `fixture.py` and one use in `graph.py`. `schemas.py` must not appear.
    `grep -n "extra=" src/heal_loop/schemas.py` shows `forbid` on all three models.

14. **The port matches the original.** Open `scripts/heal.ts` next to the table in the README.
    Check each cited line range. Check the round limit against the `for` loop on line 235, and the
    unreadable verdict rule against line 246.

15. **You can explain it.** Read `src/heal_loop/graph.py` top to bottom. For each node, say what it
    reads from the state, what it writes, and which edge follows. If you cannot, ask before the
    interview, not in it.

## Tracing

16. **Off means off.** `env -u OTEL_EXPORTER_OTLP_ENDPOINT uv run python -m heal_loop start --dir /tmp/heal-off`
    Expect the usual `PAUSED:` output, nothing on stderr, and no `trace-demo.json` in
    `/tmp/heal-off`.

17. **A real trace arrives.** `make jaeger-up && make trace-demo`
    Expect `FIXED:`, then two trees. The first starts `heal_loop.start`, the second
    `heal_loop.resume`. The last line is `resume root links to the start root span: yes`.
    Compare the shape with `docs/trace-example.txt`. Then `make jaeger-down`, and
    `podman ps -a | grep lab-jaeger` prints nothing.

18. **Nothing private is in a span.** `uv run pytest -q tests/test_tracing.py -k no_prompt`
    Expect `1 passed`. Now break it. In `tracing.py`, in `on_tool_call`, add
    `span.set_attribute("leak", " ".join(command))`. Run the test again and see it fail. Undo the
    edit. Then `grep -n "sum(rows" docs/trace-example.json` prints nothing: the real trace has no
    file content in it.

19. **A dead collector does not break a run.**
    `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 uv run python -m heal_loop start --dir /tmp/heal-dead; echo $?`
    Expect the usual `PAUSED:` output, one line on stderr that starts `Failed to export spans`,
    and `0`. It takes about 3 seconds longer than a run without tracing.
