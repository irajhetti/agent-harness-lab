# Tracing

The loop can send OpenTelemetry traces. A trace shows one run as a tree of timed steps: the run,
each graph node inside it, and each model call and tool call inside each node.

Tracing is off by default. The code is in `src/heal_loop/tracing.py`. It is an implementation of
the hooks seam in `src/heal_loop/hooks.py`. The graph does not import it.

## Turn it on

Set one standard environment variable:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

- Without it, nothing is created: no tracer provider, no exporter, no thread, no connection.
- With it, spans go out as OTLP over HTTP with protobuf, to `<endpoint>/v1/traces`.
- The service name is `heal-loop`. `OTEL_SERVICE_NAME` overrides it.
- Spans are sent in one batch when the process exits. The CLI flushes before it returns.
- The export timeout is 3 seconds. `OTEL_EXPORTER_OTLP_TIMEOUT` or
  `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` overrides it. The Python exporter reads these in seconds.
- If the collector is down, the run still finishes with its normal output and exit code. The
  exporter prints one line on stderr, `Failed to export spans batch due to timeout, max retries or
  shutdown.`, and that process's spans are lost.

## What is traced

| Span name | One per | Kind |
|---|---|---|
| `heal_loop.start` | `start` process. The root of the first trace. | internal |
| `heal_loop.resume` | `resume` process. The root of the second trace. | internal |
| `node <name>` | graph node that runs, for example `node fixer` | internal |
| `chat <model>` | model call, including a retry | client |
| `execute_tool <name>` | tool call: `git.<subcommand>`, `apply_edits`, `rerun` | internal |

The tool names are fixed in code. `apply_edits` is the step that writes the fixer's edits into the
worktree. `rerun` is the configured job command.

## Attributes

Every span has `heal.run_id`. Node, model and tool spans also have `heal.node` and `heal.round`.
The round is 0 before the first fixer pass, then 1 or 2.

| Attribute | On | Meaning |
|---|---|---|
| `heal.segment` | root | `start` or `resume` |
| `heal.paused` | root, `node human_approval` | `true` when the process stopped at the approval gate |
| `heal.outcome` | root, and the node that set it | `fixed`, `escalated`, `skipped`, `rejected` or `reverted` |
| `heal.rounds` | root | fixer and reviewer passes made |
| `heal.model_calls` | root, `node fixer`, `node reviewer` | model calls in the whole run, or in that node |
| `heal.start_trace_id` | `heal_loop.resume` | trace id of the first segment, as text |
| `heal.retried` | `node fixer`, `node reviewer` | `true` when the first reply was invalid and a second call was made |
| `heal.evidence.chars` | `node gather_evidence` | size of the error log tail |
| `heal.proposal.edits` | `node fixer` | number of file edits proposed |
| `heal.edits.count`, `heal.edits.chars` | `node apply_in_worktree` | number and total size of the edits |
| `heal.diff.chars` | `node apply_in_worktree` | size of the diff |
| `heal.guardrail.refused_edits` | `node apply_in_worktree` | number of edits the path rules refused |
| `heal.review.verdict` | `node reviewer` | `APPROVE`, `CHANGES` or `ESCALATE` |
| `heal.review.notes.chars` | `node reviewer` | size of the reviewer's notes |
| `heal.decision` | `node human_approval` | `approve` or `reject`, on the resume |
| `heal.escalation` | a node | `true` on a node that asked for a person, and on `node escalate` |
| `heal.role` | model call | `fixer` or `reviewer` |
| `heal.attempt` | model call | 1, or 2 for the retry |
| `heal.prompt.chars`, `heal.reply.chars` | model call | sizes in characters |
| `gen_ai.operation.name` | model call, tool call | `chat` or `execute_tool` |
| `gen_ai.provider.name` | model call | `scripted` or `ollama` |
| `gen_ai.request.model` | model call | the Ollama model name, or `scripted` |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | model call | only when the model server reports them |
| `gen_ai.tool.name` | tool call | for example `git.commit` |
| `process.exit.code` | tool call | exit code of the subprocess. Not set on `apply_edits`. |
| `error.type` | any span that raised | the exception class name, for example `TimeoutExpired` |

The `gen_ai.*` names follow the OpenTelemetry GenAI semantic conventions.
`gen_ai.provider.name` is the current name. It replaced `gen_ai.system`, which the installed
conventions package (0.66b2) marks as deprecated.

Span status is `ERROR` in these cases, and unset otherwise:

- A span whose code raised. The description is the exception class name.
- A tool call that exited with a code other than 0. The description is `exit code N`.
- A node that asked for a person, and `node escalate`. The description is `escalation`.
- A root span whose outcome is `escalated` or `reverted`. The description is the outcome.

A `CHANGES` verdict is not an error. A rejection by a person is not an error. A pause is not an
error.

## What is deliberately not recorded

A trace is copied to another system. It is kept longer and read by more people than the run
itself. So the rule in `tracing.py` is: names chosen by code, enum values, counts and sizes. No
text that came from a model, a file or a log.

| Left out | Why | Recorded instead |
|---|---|---|
| Prompts and model replies | A prompt holds the error log and source files. A reply holds the model's free text. | Sizes in characters |
| The error log tail | Logs can hold customer data. | `heal.evidence.chars` |
| File contents and diffs | Source can hold secrets. | Counts and sizes |
| Command lines | A commit message carries the model's own words. Arguments carry paths. | A tool name fixed in code |
| Refused paths | The model chose them. | A count |
| The escalation reason and the summary | Both are built from model text. | `heal.outcome`, `heal.escalation` |
| Exception messages and stack traces | A message can quote model output or a file. | The class name only |
| Token counts for the scripted model | It has none. Nothing is made up. | Nothing |

No span has an event. There is no switch that turns content capture on.

`tests/test_tracing.py` has a redaction test. It runs seven kinds of run with a marker string in
the error log, a source file, every reply and a path. It then checks every name, attribute, event,
link and status of every span.

These values are recorded and you choose them, so keep private text out of them: the run id, the
service name, the Ollama model name.

## Pause and resume

`start` and `resume` are two processes. They make two traces, not one.

- Both traces carry the same `heal.run_id`.
- The root of the resume trace has a span link to the root of the start trace. It also has the
  start trace id as plain text in `heal.start_trace_id`.
- `start` writes the two ids to `trace-<run_id>.json`, next to the checkpoint file. `resume`
  reads them from there. If the file is missing or damaged, the resume runs with no link.

Why not one trace: the first root span has already ended when the person answers. That can be
minutes or days later. Putting the second half under a finished parent would make one trace with
a long silent gap, and its duration would be the person's waiting time. A link says what is true:
two pieces of work, and the second follows from the first.

The cost: you see one half at a time. To find the other half, follow the link or search for the
run id.

## Run it locally

Needs rootless podman, `make` and `curl`.

```bash
make jaeger-up      # Jaeger all-in-one in a container called lab-jaeger
make trace-demo     # start, then resume --approve, with tracing on. Then prints the tree from Jaeger.
make jaeger-down    # removes the container. The traces go with it.
```

- The image is `docker.io/jaegertracing/jaeger:2.23.0`. Traces are kept in memory only.
- Two ports are published, both on loopback: `127.0.0.1:16686` for the UI and API, and
  `127.0.0.1:4318` for OTLP over HTTP.
- The container is capped at 256 MB and one CPU.
- `make trace-demo` reads the traces back from Jaeger's HTTP API. It exits with an error if either
  trace is missing or the link is not there.
- `make trace-demo SAVE=1` also rewrites the two example files in `docs/`.
- While Jaeger is up you can open `http://127.0.0.1:16686` and pick the service `heal-loop`.

`docs/trace-example.json` and `docs/trace-example.txt` are from a real run of `make trace-demo
SAVE=1`. There is no screenshot. This was built from a terminal with no browser, so the Jaeger web
page itself has not been looked at.

## How to read the tree

This is the second half of the real run in `docs/trace-example.txt`:

```
trace 18b8c12bf26d51dfb7731286e113fe3a  service=heal-loop  spans=9
heal_loop.resume  56.3 ms  heal.model_calls=2  heal.outcome=fixed  heal.rounds=1  heal.run_id=demo  heal.segment=resume  heal.start_trace_id=492a4de72d9e232aac156faf1103e715
  link -> trace 492a4de72d9e232aac156faf1103e715 span fcb9e1d5d3f6f899
  node human_approval  0.0 ms  heal.decision=approve  heal.node=human_approval  heal.round=1
  node merge_and_rerun  44.2 ms  heal.node=merge_and_rerun  heal.outcome=fixed  heal.round=1
    execute_tool git.merge  12.2 ms  process.exit.code=0
    execute_tool git.rev-parse  2.5 ms  process.exit.code=0
    execute_tool rerun  29.1 ms  process.exit.code=0
  node finish  7.6 ms  heal.node=finish  heal.round=1
    execute_tool git.worktree  3.1 ms  process.exit.code=0
    execute_tool git.branch  3.3 ms  process.exit.code=0
```

- Indentation is parent and child. The root is the process. Under it are the nodes, in the order
  they ran. Under each node are its model calls and tool calls.
- The number is the span's duration. A parent's time includes its children.
- `ERROR(...)` appears after the duration when the status is an error.
- The text tree leaves out `heal.run_id`, `heal.node` and `heal.round` when the parent has the
  same value. The JSON file has every attribute.
- `link ->` under the resume root points at the start root.
- `chat scripted` takes about 0 ms because the scripted model replays fixed text.

Questions the tree answers at a glance: where the time went, which round a call belongs to,
whether a reply needed a retry, what the reviewer said, which command failed, and how the run
ended.

## Limits

- This is a demonstration. It has been run against the toy fixture and a local Jaeger only.
- The Ollama path, including token counts, is tested against a stub HTTP server. It has not been
  run against a real model server.
- Parents are tracked on a simple stack. That is correct because this graph runs one node at a
  time. A graph with parallel nodes would need OpenTelemetry context propagation.
- Spans leave at process exit. A process that is killed hard loses its spans.
- A dead collector costs up to 3 seconds at exit, in each process.
- There are no metrics and no log export. Traces only.
- The tool spans use the GenAI name `execute_tool`. In this loop the model never picks a tool.
  Code does. The name is used so that viewers which know the conventions group these spans.
- The GenAI conventions are still marked as in development. Attribute names may change again.
- Every CLI demo run uses the run id `demo`. A search by run id finds all of them.
- LangSmith is a dependency of `langchain-core`. It imports the OpenTelemetry SDK classes when the
  SDK is installed, even with tracing off. In the traced runs here, every span that reached Jaeger
  came from this project's tracer.
- Jaeger 2.23.0 does not serve the older `/api/traces` path. The reader in
  `src/heal_loop/jaeger.py` uses `/api/v3/traces`, which answers in OTLP JSON.
