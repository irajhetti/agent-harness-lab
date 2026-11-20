# Proposal: tracing for the production agent platform

**Status: a proposal only. Nothing in production has been changed. Nothing here should be done
without the owner's approval.**

Written while adding tracing to this demonstration project. The production platform (TypeScript,
SQLite) was not read for this. The proposal relies on the description below, so check each
assumption against the real schema first.

## What exists today

One row per agent run: `id, agent_id, batch_id, work_item_id, lead_id, kind, label, log_id,
status, started_at, finished_at, cost_usd, turns, tool_calls, error, role_key, task_id, work_id,
attempt, model, duration_ms`. Plus a step log of tool calls.

The rows answer the summary questions: what ran, for whom, how long, the cost, whether it failed.
They should stay the source of truth.

## What the rows cannot show

1. **A run as a tree of timed steps.** A row says 4 minutes and 31 tool calls. It does not say
   that 3 of those minutes were one test command.
2. **Which model turn caused a tool call.** `turns` and `tool_calls` are two totals. The link
   between a turn and its calls is lost unless the step log keeps a turn number.
3. **Work that crosses processes.** A batch that plans, builds, reviews and merges is several rows
   joined by ids. Seeing it as one timeline takes a new query each time.

## The two options

| | OpenTelemetry | Langfuse |
|---|---|---|
| What it is | A vendor-neutral standard and SDK for traces. You choose the store and viewer. | A product for LLM applications: traces, prompt and reply capture, cost views, scoring, prompt versions. |
| Tree of timed steps | Yes | Yes |
| Tool call linked to its turn | Yes, parent and child spans | Yes, nested observations |
| Cross-process context | Yes, by design. Standard `traceparent`. | Yes, by passing its trace id or by receiving OpenTelemetry data |
| Reading and scoring prompts and replies | No. A trace viewer shows attributes, not conversations. | Yes. This is its main purpose. |
| What you run | One container. Jaeger showed 13 MB to 95 MB of memory in this project, with a handful of traces. | Self-hosted: several services, including two databases. Cloud: run data goes to a third party. |

The Langfuse claims about services and about receiving OpenTelemetry data are from general
knowledge, not from a test here. Check its current documentation.

The two are not rivals. OpenTelemetry is the instrumentation. Langfuse is one place to send it.

## Mapping the existing columns to spans

One root span per run. Names follow the OpenTelemetry GenAI conventions where one exists, and a
`crm.` prefix where none does.

| Column | Becomes |
|---|---|
| `id` | `crm.run.id`. Also the seed of the trace id, so exporting a row twice gives the same trace. |
| `started_at`, `finished_at` | Span start and end |
| `duration_ms` | Not copied. It is the span's duration. Use it to check the timestamps. |
| `kind`, `role_key` | Span name `invoke_agent <role_key>`, `gen_ai.operation.name = invoke_agent`, `gen_ai.agent.name`, `crm.run.kind` |
| `agent_id` | `gen_ai.agent.id` |
| `model` | `gen_ai.request.model`, plus `gen_ai.provider.name` |
| `status` | `crm.run.status`, and span status `ERROR` for a failed run |
| `error` | Span status `ERROR` and a short `error.type`. The text is not copied by default. |
| `attempt` | `crm.run.attempt`. A retry is its own span with a link to the attempt before. |
| `cost_usd` | `crm.cost.usd`. The conventions have no settled cost attribute. |
| `turns` | `crm.turns` at first. Later, one child `chat <model>` span per turn. |
| `tool_calls` and the step log | `crm.tool_calls`, and one child `execute_tool <tool>` span per step log entry |
| `batch_id`, `work_item_id`, `work_id`, `task_id` | `crm.batch.id` and so on. These are what you search by. |
| `lead_id` | `crm.lead.id`. The number only. No name, email or message text. |
| `label` | `crm.run.label`, only if labels are fixed strings and never client text |
| `log_id` | `crm.log.id`. The pointer from a span back to the full log. |

Assumption to check: the step log needs a timestamp per entry for real durations, and a turn
number to place each tool call under its turn.

## A low-risk rollout

- **Step 0. Export from the existing rows. No change to production.** A script reads a copy of
  the database, read-only, and sends one span per row and per step log entry to a Jaeger on
  loopback. Run it by hand over a week of history. It shows whether a tree answers questions the
  rows do not. To undo it, delete the script and the container.
- **Step 1. The same export on a timer,** for new rows. The agents and the runner are untouched,
  so tracing can fail without a run noticing.
- **Step 2. Live instrumentation in one place,** where runs are started, behind a variable that
  is off by default. Turns and tool calls become spans with true timings. Child processes get the
  trace context in `TRACEPARENT`. A sandboxed agent may receive that id but must not reach the
  collector. The parent process exports. Keep the rules this project tests: a short timeout, no
  failure when the collector is down, sizes instead of content, a redaction test.
- **Step 3. Only if needed, a second destination.** Send the same spans to Langfuse if people
  want to read and score prompts. That means storing content, so it needs its own approval.

Each step is useful without the next.

## Costs and risks

- **Sensitive data.** The main risk. Prompts and tool output hold client email text and code, and
  error text can quote them. A trace store is usually less protected than the main database, and
  Jaeger's page has no login. Record ids, enums, counts and sizes. Keep the text behind `log_id`.
- **Overhead.** Small next to a run of minutes. In this project, sending two dozen spans to a
  local Jaeger took about 10 ms at exit. Start-up cost of the SDK was not measured on a quiet
  machine. These are toy numbers.
- **Another service.** One more container to patch, with memory, disk if traces are kept, and a
  port that must stay on loopback or the private network.
- **Two records of one thing.** Rows and traces can disagree. Rows win. Traces can be deleted and
  rebuilt, which is why step 0 comes first.
- **A tracing fault stopping a run.** The exporter must never block or raise into a run.
- **Changing names.** The GenAI conventions are still in development. `gen_ai.system` has already
  become `gen_ai.provider.name`.

## Recommendation

Use OpenTelemetry, and start with step 0 only.

- It adds the three things the rows lack, for the price of one local container.
- Step 0 touches no production code or data, so little is lost if the trees are not useful.
- Do not adopt Langfuse now. Its value is reviewing prompt content, and storing content is the
  largest risk on this page. It can be added later as a second destination.

**Decision needed from the owner:** approve step 0, or decline. Steps 1 to 3 each need their own
approval.
