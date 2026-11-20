"""OpenTelemetry tracing for the loop: an implementation of the Hooks seam. See TRACING.md.

One root span per run segment (`heal_loop.start`, `heal_loop.resume`), one span per node, per
model call and per tool call. Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (`from_env`).

Privacy rule for this file: no prompt, reply, file content, diff, command line, path or error
message is ever put on a span. Only names chosen by code, enum values, counts and sizes.
"""

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanContext, SpanKind, Status, StatusCode, TraceFlags, Tracer

from .hooks import Hooks
from .models import Model

log = logging.getLogger(__name__)

SERVICE_NAME = "heal-loop"
EXPORT_TIMEOUT_S = 3.0  # the CLI is short-lived: a dead collector may cost this much at exit, no more
FAILED_OUTCOMES = ("escalated", "reverted")  # the loop did not fix the job, so the root span is an error
PROVIDERS = {"OllamaModel": "ollama", "ScriptedModel": "scripted"}

type Attribute = str | int | bool


def _never_raise[**P](method: Callable[P, None]) -> Callable[P, None]:
    """A tracing bug must never stop a run, least of all between a merge and its rerun."""

    @wraps(method)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            method(*args, **kwargs)
        except Exception:
            log.exception("tracing failed in %s; the run carries on", method.__name__)

    return guarded


def _fail(span: Span, reason: str) -> None:
    span.set_attribute("error.type", reason)
    span.set_status(Status(StatusCode.ERROR, reason))
    span.end()


class TracingHooks(Hooks):
    """Emits spans to `tracer`. Parents are tracked on an explicit stack: root, node, model or tool call.

    `link_dir` is where the first segment's trace and span ids are kept (`trace-<run_id>.json`), so
    that the resume, which is another process and another trace, can link back to it.
    """

    def __init__(self, tracer: Tracer, model: Model, link_dir: Path | None = None) -> None:
        self.tracer, self.model, self.link_dir = tracer, model, link_dir
        kind = type(model).__name__
        self.provider = PROVIDERS.get(kind, kind)
        self.model_name = str(getattr(model, "model", self.provider))  # ScriptedModel has no model name
        self._stack: list[Span] = []
        self._common: dict[str, Attribute] = {}  # run id, and inside a node: node name and round
        self._calls = 0  # model calls made by the current node

    def _start(
        self,
        name: str,
        more: Mapping[str, Attribute] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        links: Sequence[Link] = (),
    ) -> Span:
        parent = trace.set_span_in_context(self._stack[-1]) if self._stack else None
        span = self.tracer.start_span(name, parent, kind, attributes={**self._common, **(more or {})}, links=links)
        self._stack.append(span)
        return span

    def _start_segment(self, segment: str, run_id: str) -> None:
        self._common = {"heal.run_id": run_id}
        link_file = self.link_dir / f"trace-{run_id}.json" if self.link_dir else None
        links: list[Link] = []
        more: dict[str, Attribute] = {"heal.segment": segment}
        if segment == "resume" and link_file:
            try:
                first = json.loads(link_file.read_text())
                trace_id, span_id = int(first["trace_id"], 16), int(first["span_id"], 16)
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("no usable %s: the resume trace has no link to the start trace", link_file)
            else:
                start_root = SpanContext(trace_id, span_id, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
                links.append(Link(start_root, {"heal.segment": "start"}))
                more["heal.start_trace_id"] = first["trace_id"]  # also as plain text, to paste into a viewer
        root = self._start(f"heal_loop.{segment}", more, links=links).get_span_context()
        if segment == "start" and link_file and root.is_valid:
            try:
                ids = {"trace_id": format(root.trace_id, "032x"), "span_id": format(root.span_id, "016x")}
                link_file.write_text(json.dumps(ids) + "\n")
            except OSError:
                log.warning("could not write %s: the resume trace will have no link", link_file)

    # ---- nodes ---------------------------------------------------------------------------

    @_never_raise
    def on_node_start(self, node: str, state: Mapping[str, Any]) -> None:
        if not self._stack:  # the first node of a process: gather_evidence on start, the gate again on resume
            self._start_segment("start" if node == "gather_evidence" else "resume", str(state.get("run_id", "")))
        # The fixer opens the next round, the other nodes belong to the round already counted in the state.
        self._common |= {"heal.node": node, "heal.round": int(state.get("round", 0)) + (node == "fixer")}
        self._calls = 0
        span = self._start(f"node {node}")
        if node == "apply_in_worktree":
            edits = state["proposal"]["edits"]
            span.set_attribute("heal.edits.count", len(edits))
            span.set_attribute("heal.edits.chars", sum(len(edit["new_content"]) for edit in edits))
        if node == "finish":  # the only node that sees the whole finished state, so it describes the root
            root, outcome = self._stack[0], str(state.get("outcome", ""))
            root.set_attribute("heal.outcome", outcome)
            root.set_attribute("heal.rounds", int(state.get("round", 0)))
            root.set_attribute("heal.model_calls", int(state.get("model_calls", 0)))
            if outcome in FAILED_OUTCOMES:
                root.set_status(Status(StatusCode.ERROR, outcome))

    @_never_raise
    def on_node_end(self, node: str, update: Mapping[str, Any]) -> None:
        span = self._stack.pop()
        for key in ("evidence", "diff"):
            if key in update:
                span.set_attribute(f"heal.{key}.chars", len(update[key]))
        for key in ("outcome", "decision"):  # both are words chosen by code, never model text
            if key in update:
                span.set_attribute(f"heal.{key}", str(update[key]))
        if "proposal" in update:
            span.set_attribute("heal.proposal.edits", len(update["proposal"]["edits"]))
        if "review" in update:
            span.set_attribute("heal.review.verdict", str(update["review"]["verdict"]))  # a validated enum
            span.set_attribute("heal.review.notes.chars", len(update["review"]["notes"]))
        if "refused_edits" in update:
            span.set_attribute("heal.guardrail.refused_edits", int(update["refused_edits"]))
        if self._calls:
            span.set_attribute("heal.model_calls", self._calls)
            span.set_attribute("heal.retried", self._calls > 1)
        if update.get("escalation") or node == "escalate":
            span.set_attribute("heal.escalation", True)
            span.set_status(Status(StatusCode.ERROR, "escalation"))
        span.end()
        if node == "finish":
            self._stack.pop().end()

    @_never_raise
    def on_node_pause(self, node: str) -> None:
        for span in (self._stack.pop(), self._stack.pop()):  # the node, then the root: this process is done
            span.set_attribute("heal.paused", True)
            span.end()

    # ---- model calls ---------------------------------------------------------------------

    @_never_raise
    def on_model_start(self, role: str, prompt: str) -> None:
        self._calls += 1
        self._start(
            f"chat {self.model_name}",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self.provider,
                "gen_ai.request.model": self.model_name,
                "heal.role": role,
                "heal.attempt": self._calls,
                "heal.prompt.chars": len(prompt),
            },
            SpanKind.CLIENT,
        )

    @_never_raise
    def on_model_call(self, role: str, prompt: str, reply: str) -> None:
        span = self._stack.pop()
        span.set_attribute("heal.reply.chars", len(reply))
        usage = getattr(self.model, "last_usage", None)  # only OllamaModel has it, and only when the server said
        if usage:
            span.set_attribute("gen_ai.usage.input_tokens", int(usage[0]))
            span.set_attribute("gen_ai.usage.output_tokens", int(usage[1]))
        span.end()

    # ---- tool calls ----------------------------------------------------------------------

    @_never_raise
    def on_tool_start(self, name: str) -> None:
        self._start(f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name})

    @_never_raise
    def on_tool_call(self, command: Sequence[str], returncode: int) -> None:
        # `command` is deliberately not recorded: a commit message carries the model's own words.
        span = self._stack[-1]
        span.set_attribute("process.exit.code", returncode)
        if returncode != 0:
            span.set_status(Status(StatusCode.ERROR, f"exit code {returncode}"))

    @_never_raise
    def on_tool_end(self, name: str) -> None:
        self._stack.pop().end()

    # ---- errors --------------------------------------------------------------------------

    @_never_raise
    def on_error(self, error: BaseException) -> None:
        reason = type(error).__qualname__  # the class name only: the message may quote model output
        _fail(self._stack.pop(), reason)
        if len(self._stack) == 1:  # that was a node, so the run stops here and the root ends too
            _fail(self._stack.pop(), reason)


def from_env(model: Model, link_dir: Path | None = None) -> tuple[Hooks, Callable[[], None]]:
    """The hooks to run with, and a function to call before the process exits (it flushes the spans).

    Tracing is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set. Otherwise this returns the no-op
    Hooks and creates nothing: no provider, no exporter, no thread, no connection.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return Hooks(), lambda: None
    # Imported here so that a run without tracing never loads the exporter.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    env_timeout = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT")
    exporter = OTLPSpanExporter(timeout=None if env_timeout else EXPORT_TIMEOUT_S)  # endpoint comes from the env
    resource = Resource.create({} if os.environ.get("OTEL_SERVICE_NAME") else {"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)  # not installed as the global provider
    # Batch, not simple: one export at exit, so a dead collector costs one timeout, not one per span.
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return TracingHooks(provider.get_tracer("heal_loop"), model, link_dir), provider.shutdown
