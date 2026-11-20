"""Tracing: the span tree, the attributes, what is kept out of spans, and what happens when it is off or broken.

Offline. Spans go to the SDK's in-memory exporter, or to a stub collector on a loopback port.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from helpers import RUN, git
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from heal_loop import HealConfig, Hooks, OllamaModel, ScriptedModel, build_graph, resume, start
from heal_loop.cli import main
from heal_loop.fixture import STILL_BROKEN, fenced, make_fixture, proposal, review
from heal_loop.graph import Graph
from heal_loop.models import Model, ask
from heal_loop.schemas import Review
from heal_loop.tracing import TracingHooks, from_env

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
MARKER = "ZX-CANARY-7731"  # stands in for anything private in a prompt, a reply or a file


class Traced:
    """A loop wired to an in-memory span exporter."""

    def __init__(self, cfg: HealConfig, model: Model) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.hooks = TracingHooks(provider.get_tracer("test"), model, link_dir=cfg.ledger.parent)
        self.graph: Graph = build_graph(cfg, model, self.hooks)

    @property
    def spans(self) -> list[ReadableSpan]:
        return sorted(self.exporter.get_finished_spans(), key=lambda span: span.start_time or 0)

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans if span.name == name]

    def trees(self) -> list[str]:
        """One indented tree of span names per trace, in the order the traces began."""
        spans = self.spans

        def walk(span: ReadableSpan, depth: int) -> list[str]:
            children = [s for s in spans if s.parent and s.parent.span_id == span.context.span_id]
            return ["  " * depth + span.name] + [line for child in children for line in walk(child, depth + 1)]

        return ["\n".join(walk(root, 0)) for root in spans if root.parent is None]


def scripted(fixer: list[dict[str, Any] | str], reviewer: list[dict[str, Any] | str]) -> ScriptedModel:
    def texts(replies: list[dict[str, Any] | str]) -> list[str]:
        return [reply if isinstance(reply, str) else fenced(reply) for reply in replies]

    return ScriptedModel({"fixer": texts(fixer), "reviewer": texts(reviewer)})


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# ---- the span tree -------------------------------------------------------------------------------

START_TREE = """\
heal_loop.start
  node gather_evidence
    execute_tool git.rev-parse
    execute_tool git.worktree
  node fixer
    execute_tool git.ls-files
    chat scripted
  node apply_in_worktree
    execute_tool apply_edits
    execute_tool git.add
    execute_tool git.commit
    execute_tool git.diff
    execute_tool git.rev-parse
  node reviewer
    chat scripted
  node human_approval"""

RESUME_TREE = """\
heal_loop.resume
  node human_approval
  node merge_and_rerun
    execute_tool git.merge
    execute_tool git.rev-parse
    execute_tool rerun
  node finish
    execute_tool git.worktree
    execute_tool git.branch"""


def test_an_approve_run_is_two_traces_with_the_expected_tree(cfg: HealConfig, caplog: pytest.LogCaptureFixture) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    assert resume(traced.graph, RUN, approve=True)["outcome"] == "fixed"

    assert traced.trees() == [START_TREE, RESUME_TREE]
    assert not caplog.records  # the tracer itself logged no failure
    first, second = traced.named("heal_loop.start")[0], traced.named("heal_loop.resume")[0]
    assert first.context.trace_id != second.context.trace_id  # the resume is a new trace, not a child
    assert all(span.attributes and span.attributes["heal.run_id"] == RUN for span in traced.spans)
    assert all(span.end_time for span in traced.spans)

    assert attrs(first) == {"heal.run_id": RUN, "heal.segment": "start", "heal.paused": True}
    assert first.status.status_code is StatusCode.UNSET
    assert attrs(second) == {
        "heal.run_id": RUN,
        "heal.segment": "resume",
        "heal.start_trace_id": format(first.context.trace_id, "032x"),
        "heal.outcome": "fixed",
        "heal.rounds": 1,
        "heal.model_calls": 2,
    }
    assert second.status.status_code is StatusCode.UNSET


def test_the_resume_root_links_to_the_start_root_through_the_file_kept_with_the_run(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    first = traced.named("heal_loop.start")[0].context
    kept = json.loads((cfg.ledger.parent / f"trace-{RUN}.json").read_text())
    assert kept == {"trace_id": format(first.trace_id, "032x"), "span_id": format(first.span_id, "016x")}

    resume(traced.graph, RUN, approve=False)
    (link,) = traced.named("heal_loop.resume")[0].links
    assert (link.context.trace_id, link.context.span_id) == (first.trace_id, first.span_id)
    assert dict(link.attributes or {}) == {"heal.segment": "start"}


def test_a_missing_link_file_does_not_stop_the_resume(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    (cfg.ledger.parent / f"trace-{RUN}.json").write_text("not json")
    assert resume(traced.graph, RUN, approve=True)["outcome"] == "fixed"
    assert traced.named("heal_loop.resume")[0].links == ()


def test_node_model_and_tool_spans_carry_the_required_attributes(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    resume(traced.graph, RUN, approve=True)

    for span in traced.spans:
        if span.name.startswith("node "):
            assert {"heal.run_id", "heal.node", "heal.round"} <= set(attrs(span))
            assert span.name == f"node {attrs(span)['heal.node']}"
    fixer_call, reviewer_call = traced.named("chat scripted")
    assert fixer_call.kind is SpanKind.CLIENT
    assert attrs(fixer_call) == {
        "heal.run_id": RUN,
        "heal.node": "fixer",
        "heal.round": 1,
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "scripted",
        "gen_ai.request.model": "scripted",
        "heal.role": "fixer",
        "heal.attempt": 1,
        "heal.prompt.chars": attrs(fixer_call)["heal.prompt.chars"],
        "heal.reply.chars": len(fenced(proposal())),
    }  # and so no token counts: the scripted model reports none
    assert attrs(fixer_call)["heal.prompt.chars"] > 500
    assert attrs(reviewer_call)["heal.role"] == "reviewer" and attrs(reviewer_call)["heal.node"] == "reviewer"

    tools = [span for span in traced.spans if span.name.startswith("execute_tool ")]
    assert len(tools) == 13
    for tool in tools:
        assert attrs(tool)["gen_ai.operation.name"] == "execute_tool"
        assert tool.name == f"execute_tool {attrs(tool)['gen_ai.tool.name']}"
        assert {"heal.run_id", "heal.node", "heal.round"} <= set(attrs(tool))
        if tool.name != "execute_tool apply_edits":  # the one tool that is not a subprocess
            assert attrs(tool)["process.exit.code"] == 0
    assert "process.exit.code" not in attrs(traced.named("execute_tool apply_edits")[0])

    by_node = {attrs(span)["heal.node"]: attrs(span) for span in traced.spans if span.name.startswith("node ")}
    assert by_node["gather_evidence"]["heal.round"] == 0 and by_node["gather_evidence"]["heal.evidence.chars"] > 0
    assert by_node["fixer"] | {"heal.run_id": RUN} == {
        "heal.run_id": RUN,
        "heal.node": "fixer",
        "heal.round": 1,
        "heal.proposal.edits": 1,
        "heal.model_calls": 1,
        "heal.retried": False,
    }
    assert by_node["apply_in_worktree"]["heal.edits.count"] == 1
    assert by_node["apply_in_worktree"]["heal.edits.chars"] == len(proposal()["edits"][0]["new_content"])
    assert by_node["apply_in_worktree"]["heal.diff.chars"] > 0
    assert by_node["reviewer"]["heal.review.verdict"] == "APPROVE"
    assert by_node["reviewer"]["heal.review.notes.chars"] == len(review()["notes"])
    assert by_node["human_approval"]["heal.decision"] == "approve"  # the resume pass; the paused pass has none
    assert attrs(traced.named("node human_approval")[0])["heal.paused"] is True


# ---- verdicts, outcomes and error status ---------------------------------------------------------


def test_a_changes_round_shows_both_verdicts_and_round_numbers_and_is_not_an_error(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal(STILL_BROKEN), proposal()], [review("CHANGES", "sum all"), review()]))
    start(traced.graph, RUN)

    reviews = traced.named("node reviewer")
    assert [attrs(span)["heal.review.verdict"] for span in reviews] == ["CHANGES", "APPROVE"]
    assert [attrs(span)["heal.round"] for span in reviews] == [1, 2]
    assert [attrs(span)["heal.round"] for span in traced.named("node fixer")] == [1, 2]
    assert [attrs(span)["heal.round"] for span in traced.named("chat scripted")] == [1, 1, 2, 2]
    assert all(span.status.status_code is StatusCode.UNSET for span in traced.spans)
    assert traced.trees()[0].count("node fixer") == 2 and len(traced.trees()) == 1


def test_an_invalid_reply_shows_as_attempt_two_and_a_retried_node(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted(["I am not sure.", proposal()], [review()]))
    start(traced.graph, RUN)

    fixer_calls = [span for span in traced.named("chat scripted") if attrs(span)["heal.role"] == "fixer"]
    assert [attrs(span)["heal.attempt"] for span in fixer_calls] == [1, 2]
    fixer = attrs(traced.named("node fixer")[0])
    assert fixer["heal.model_calls"] == 2 and fixer["heal.retried"] is True
    assert attrs(traced.named("node reviewer")[0])["heal.retried"] is False


def test_an_escalation_marks_the_escalate_node_and_the_root_as_errors(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal()], [review("ESCALATE", "needs a person")]))
    assert start(traced.graph, RUN)["outcome"] == "escalated"

    (root,) = traced.named("heal_loop.start")
    assert attrs(root)["heal.outcome"] == "escalated" and "heal.paused" not in attrs(root)
    assert (root.status.status_code, root.status.description) == (StatusCode.ERROR, "escalated")
    (escalate,) = traced.named("node escalate")
    assert escalate.status.status_code is StatusCode.ERROR
    assert attrs(escalate)["heal.escalation"] is True and attrs(escalate)["heal.outcome"] == "escalated"
    reviewer = traced.named("node reviewer")[0]
    assert attrs(reviewer)["heal.review.verdict"] == "ESCALATE"
    assert reviewer.status.status_code is StatusCode.UNSET  # the reviewer did its job
    assert traced.trees()[0].endswith("node escalate\n  node finish\n    execute_tool git.worktree")


def test_a_guardrail_refusal_is_counted_and_marks_the_node_as_an_error(cfg: HealConfig) -> None:
    bad = proposal()
    bad["edits"] += [
        {"path": "job.py", "new_content": "print('nightly-report ok')\n"},
        {"path": "../x.py", "new_content": ""},
    ]
    traced = Traced(cfg, scripted([bad], []))
    assert start(traced.graph, RUN)["outcome"] == "escalated"

    (apply,) = traced.named("node apply_in_worktree")
    assert attrs(apply)["heal.guardrail.refused_edits"] == 2 and attrs(apply)["heal.escalation"] is True
    assert (apply.status.status_code, apply.status.description) == (StatusCode.ERROR, "escalation")
    assert not traced.named("execute_tool apply_edits")  # nothing was written
    assert traced.named("heal_loop.start")[0].status.status_code is StatusCode.ERROR


def test_a_revert_shows_the_failed_rerun_and_the_outcome(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal(STILL_BROKEN)], [review()]))
    start(traced.graph, RUN)
    assert resume(traced.graph, RUN, approve=True)["outcome"] == "reverted"

    (rerun,) = traced.named("execute_tool rerun")
    assert attrs(rerun)["process.exit.code"] == 1
    assert (rerun.status.status_code, rerun.status.description) == (StatusCode.ERROR, "exit code 1")
    assert attrs(traced.named("node revert")[0])["heal.outcome"] == "reverted"
    assert len(traced.named("execute_tool git.revert")) == 1
    (root,) = traced.named("heal_loop.resume")
    assert attrs(root)["heal.outcome"] == "reverted"
    assert (root.status.status_code, root.status.description) == (StatusCode.ERROR, "reverted")
    assert traced.named("heal_loop.start")[0].status.status_code is StatusCode.UNSET  # the first half went well


def test_a_rejection_is_an_outcome_and_not_an_error(cfg: HealConfig) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    resume(traced.graph, RUN, approve=False)
    (root,) = traced.named("heal_loop.resume")
    assert attrs(root)["heal.outcome"] == "rejected" and root.status.status_code is StatusCode.UNSET
    assert attrs(traced.named("node human_approval")[1])["heal.decision"] == "reject"


def test_an_exception_closes_every_open_span_as_an_error_with_the_class_name_only(cfg: HealConfig) -> None:
    traced = Traced(cfg, ScriptedModel({}))  # no replies: stands in for a model server that is down
    with pytest.raises(RuntimeError, match="no reply left"):
        start(traced.graph, RUN)

    assert traced.trees()[0].endswith("node fixer\n    execute_tool git.ls-files\n    chat scripted")
    for name in ("chat scripted", "node fixer", "heal_loop.start"):
        (span,) = traced.named(name)
        assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, "RuntimeError")
        assert attrs(span)["error.type"] == "RuntimeError" and span.end_time
        assert not span.events  # no recorded exception: its message and stack could quote private text


def test_a_rerun_timeout_is_an_error_on_the_tool_span_only(cfg: HealConfig) -> None:
    slow = replace(cfg, rerun_command=(sys.executable, "-c", "import time; time.sleep(30)"), rerun_timeout_s=1)
    traced = Traced(slow, scripted([proposal()], [review()]))
    start(traced.graph, RUN)
    assert resume(traced.graph, RUN, approve=True)["outcome"] == "reverted"
    (rerun,) = traced.named("execute_tool rerun")
    assert attrs(rerun)["error.type"] == "TimeoutExpired" and rerun.status.status_code is StatusCode.ERROR
    assert traced.named("node merge_and_rerun")[0].status.status_code is StatusCode.UNSET  # the node handled it
    assert traced.trees()[1].startswith("heal_loop.resume\n  node human_approval\n  node merge_and_rerun")


# ---- privacy -------------------------------------------------------------------------------------


class Exploding:
    def complete(self, role: str, prompt: str) -> str:
        raise RuntimeError(f"upstream said: {MARKER}")


def test_no_prompt_reply_file_content_diff_or_path_reaches_a_span(tmp_path: Path) -> None:
    """Every run below is full of MARKER. It must not be in any span, in any form."""
    marked = proposal(f"def total(rows):\n    # {MARKER}\n    return sum(rows)\n")
    marked |= {"cause": f"cause {MARKER}", "rationale": f"why {MARKER}"}
    marked_review = review(notes=f"notes {MARKER}")
    marked_review_changes = review("CHANGES", f"again {MARKER}")
    bad_path = proposal(path=f"../{MARKER}.py")
    still_broken = proposal(f"{STILL_BROKEN}# {MARKER}\n") | {"cause": f"cause {MARKER}"}
    scenarios: list[tuple[Model, bool | None]] = [  # (model, the decision to resume with)
        (scripted([f"{MARKER} is all I have", marked], [f"{MARKER}", marked_review]), True),  # retries, then fixed
        (scripted([marked, marked], [marked_review_changes, marked_review_changes]), None),  # CHANGES twice
        (scripted([bad_path], []), None),  # guardrail refusal
        (scripted([{"fixable": False, "cause": MARKER, "rationale": MARKER}], []), None),  # not fixable
        (scripted([still_broken], [marked_review]), True),  # merged, then reverted
        (scripted([marked], [marked_review]), False),  # rejected
        (Exploding(), None),  # the model call raises
    ]
    spans: list[ReadableSpan] = []
    seen_by_models = ""
    for number, (model, decision) in enumerate(scenarios):
        cfg = make_fixture(tmp_path / str(number))
        with cfg.error_log.open("a") as log:
            log.write(f"Traceback: customer {MARKER} could not be billed\n")  # the evidence
        (cfg.workspace / "app" / "notes.py").write_text(f"SECRET = '{MARKER}'\n")  # a file the fixer is shown
        git(cfg.workspace, "add", "-A")
        git(cfg.workspace, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "notes")
        traced = Traced(cfg, model)
        try:
            result = start(traced.graph, RUN)
            if decision is not None:
                assert MARKER in result["__interrupt__"][0].value["diff"]
                resume(traced.graph, RUN, approve=decision)
        except RuntimeError:
            assert isinstance(model, Exploding)
        spans += traced.spans
        assert MARKER not in "".join(path.read_text() for path in cfg.ledger.parent.glob("trace-*.json"))
        if isinstance(model, ScriptedModel):
            seen_by_models += "".join(prompt for _, prompt in model.calls)

    # The marker really was in the evidence, the pasted file, the prompts and the retry feedback.
    assert seen_by_models.count(MARKER) > 20 and f"SECRET = '{MARKER}'" in seen_by_models
    outcomes = {attrs(span).get("heal.outcome") for span in spans if span.parent is None}
    assert outcomes >= {"fixed", "escalated", "reverted", "rejected"}
    assert len(spans) > 100

    for span in spans:
        assert MARKER not in span.name
        for key, value in attrs(span).items():
            assert MARKER not in key and MARKER not in str(value), (span.name, key)
        for event in span.events:
            assert MARKER not in event.name and MARKER not in str(dict(event.attributes or {})), span.name
        for link in span.links:
            assert MARKER not in str(dict(link.attributes or {}))
        assert MARKER not in (span.status.description or "")
        assert MARKER not in span.to_json()  # everything else: resource, scope, ids
        assert not span.events  # this tracer records no events at all


# ---- token usage ---------------------------------------------------------------------------------


@contextmanager
def http_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("counts", [{"prompt_eval_count": 41, "eval_count": 7}, {}])
def test_token_usage_is_recorded_only_when_the_model_server_reports_it(counts: dict[str, int]) -> None:
    class Ollama(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
            self.rfile.read(int(self.headers["Content-Length"]))
            body = {"message": {"role": "assistant", "content": '{"verdict": "APPROVE", "notes": "ok"}'}} | counts
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with http_server(Ollama) as url:
        model = OllamaModel(url, "some-local-model", timeout_s=5)
        parsed, _, _ = ask(model, "reviewer", "judge this", Review, TracingHooks(provider.get_tracer("test"), model))
    assert parsed is not None

    (span,) = exporter.get_finished_spans()
    assert span.name == "chat some-local-model"
    assert attrs(span)["gen_ai.provider.name"] == "ollama" and attrs(span)["gen_ai.request.model"] == "some-local-model"
    usage = {key: value for key, value in attrs(span).items() if key.startswith("gen_ai.usage.")}
    assert usage == ({"gen_ai.usage.input_tokens": 41, "gen_ai.usage.output_tokens": 7} if counts else {})


# ---- off, broken, unreachable --------------------------------------------------------------------


def refuse(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("tracing is off: nothing may be created and nothing may be connected")


@pytest.mark.parametrize("value", [None, ""])
def test_without_the_endpoint_nothing_is_created_and_nothing_is_connected(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENDPOINT, raising=False)
    if value is not None:
        monkeypatch.setenv(ENDPOINT, value)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", refuse)
    monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", refuse)
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    threads = threading.active_count()

    hooks, flush = from_env(ScriptedModel({}), tmp_path)
    assert type(hooks) is Hooks  # the no-op base class, not a tracer that happens to be quiet
    flush()
    assert main(["start", "--dir", str(tmp_path / "demo")]) == 0
    assert main(["resume", "--dir", str(tmp_path / "demo"), "--approve"]) == 0
    assert not list((tmp_path / "demo").glob("trace-*.json"))
    assert threading.active_count() == threads  # no export thread


def cli(env: dict[str, str], *args: str) -> tuple[subprocess.CompletedProcess[str], float]:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("OTEL_")}
    began = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "heal_loop", *args], capture_output=True, text=True, env=clean | env, timeout=120
    )
    return done, time.monotonic() - began


def test_a_cli_process_without_the_endpoint_loads_no_exporter_and_makes_no_provider(tmp_path: Path) -> None:
    # LangSmith, which langchain-core depends on, imports the SDK's classes whenever the SDK is installed.
    # So "the SDK is not imported" cannot be promised. "No exporter module, no provider object" can.
    code = """
import gc, sys
from heal_loop.cli import main
code = main(["start", "--dir", sys.argv[1]])
from opentelemetry.sdk.trace import TracerProvider
print("EXPORTER MODULES", [name for name in sys.modules if name.startswith("opentelemetry.exporter")])
print("PROVIDERS", len([thing for thing in gc.get_objects() if isinstance(thing, TracerProvider)]))
sys.exit(code)
"""
    clean = {key: value for key, value in os.environ.items() if not key.startswith("OTEL_")}
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "demo")], capture_output=True, text=True, env=clean
    )
    assert done.returncode == 0, done.stderr
    assert "PAUSED" in done.stdout and "EXPORTER MODULES []" in done.stdout and "PROVIDERS 0" in done.stdout


def test_a_closed_port_does_not_stop_or_fail_the_run(tmp_path: Path) -> None:
    with socket.socket() as probe:  # find a loopback port with nothing listening on it
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {ENDPOINT: f"http://127.0.0.1:{port}"}
    untraced, baseline = cli({}, "start", "--dir", str(tmp_path / "plain"))
    started, took_start = cli(env, "start", "--dir", str(tmp_path / "demo"))
    assert started.returncode == 0, started.stderr
    assert started.stdout.replace("demo", "plain") == untraced.stdout  # same output as an untraced run
    resumed, took_resume = cli(env, "resume", "--dir", str(tmp_path / "demo"), "--approve")
    assert resumed.returncode == 0 and resumed.stdout.startswith("FIXED:")
    # The export timeout is 3 seconds. The rest of the margin is for importing the SDK on a busy machine.
    assert max(took_start, took_resume) < baseline + 15


def test_a_collector_that_never_answers_costs_one_short_timeout(tmp_path: Path) -> None:
    with socket.socket() as silent:  # accepts the connection in its backlog and then says nothing
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        _, baseline = cli({}, "start", "--dir", str(tmp_path / "plain"))
        started, took = cli(
            {ENDPOINT: f"http://127.0.0.1:{silent.getsockname()[1]}"}, "start", "--dir", str(tmp_path / "d")
        )
    assert started.returncode == 0 and "PAUSED" in started.stdout
    assert took < baseline + 15


def test_a_tracing_bug_is_logged_and_the_run_still_finishes(
    cfg: HealConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    traced = Traced(cfg, scripted([proposal()], [review()]))
    monkeypatch.setattr(traced.hooks, "_start", refuse)  # every hook now fails
    with caplog.at_level(logging.ERROR, logger="heal_loop.tracing"):
        start(traced.graph, RUN)
        assert resume(traced.graph, RUN, approve=True)["outcome"] == "fixed"
    assert "tracing failed in on_node_start; the run carries on" in caplog.text
    assert traced.spans == []


# ---- the real exporter, against a stub collector -------------------------------------------------


def test_two_cli_processes_flush_their_spans_over_otlp_http_before_they_exit(tmp_path: Path) -> None:
    posts: list[tuple[str, str, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
            body = self.rfile.read(int(self.headers["Content-Length"]))
            posts.append((self.path, self.headers["Content-Type"], body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with http_server(Collector) as url:
        started, _ = cli({ENDPOINT: url}, "start", "--dir", str(tmp_path / "demo"))
        resumed, _ = cli({ENDPOINT: url}, "resume", "--dir", str(tmp_path / "demo"), "--approve")
        renamed, _ = cli({ENDPOINT: url, "OTEL_SERVICE_NAME": "other"}, "start", "--dir", str(tmp_path / "other"))
    assert (started.returncode, resumed.returncode, renamed.returncode) == (0, 0, 0)
    assert started.stderr == "" and resumed.stderr == ""
    assert {(path, content_type) for path, content_type, _ in posts} == {("/v1/traces", "application/x-protobuf")}

    by_service: dict[str, list[Any]] = {}
    for _, _, body in posts:
        for resource_spans in ExportTraceServiceRequest.FromString(body).resource_spans:
            service = {pair.key: pair.value.string_value for pair in resource_spans.resource.attributes}["service.name"]
            by_service.setdefault(service, []).extend(s for scope in resource_spans.scope_spans for s in scope.spans)
    assert sorted(by_service) == ["heal-loop", "other"]  # the default, and OTEL_SERVICE_NAME

    spans = by_service["heal-loop"]
    assert len(spans) == 16 + 9  # every span of both processes arrived: nothing was lost at exit
    roots = {span.name: span for span in spans if not span.parent_span_id}
    assert sorted(roots) == ["heal_loop.resume", "heal_loop.start"]
    first, second = roots["heal_loop.start"], roots["heal_loop.resume"]
    assert first.trace_id != second.trace_id
    (link,) = second.links  # the link survived the trip through the file and across the two processes
    assert (link.trace_id, link.span_id) == (first.trace_id, first.span_id)
    assert json.loads((tmp_path / "demo" / "trace-demo.json").read_text())["trace_id"] == first.trace_id.hex()
