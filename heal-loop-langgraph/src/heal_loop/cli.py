"""Command line demo: run the loop against the fixture, pause at approval, resume with a decision.

    python -m heal_loop start --dir /tmp/heal-demo
    python -m heal_loop resume --dir /tmp/heal-demo --approve     (or --reject)

The paused run is kept in a SQLite checkpoint under --dir, so the two commands are separate processes.
Set OTEL_EXPORTER_OTLP_ENDPOINT to send traces (see TRACING.md). Without it nothing is traced.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .fixture import STILL_BROKEN, fenced, fixture_config, make_fixture, proposal, review
from .graph import build_graph, resume, start
from .models import Model, OllamaModel, ScriptedModel
from .tracing import from_env

RUN_ID = "demo"


def pick_model(scripted: ScriptedModel) -> Model:
    """The scripted model, unless OLLAMA_BASE_URL points at a local Ollama-compatible server."""
    base_url = os.environ.get("OLLAMA_BASE_URL")
    if base_url:
        return OllamaModel(base_url, os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    return scripted


def report(result: dict[str, Any], directory: Path) -> None:
    if "__interrupt__" in result:
        ask = result["__interrupt__"][0].value
        print(f"Cause:    {ask['cause']}\nReviewer: {ask['reviewer_notes']}\n\n{ask['diff']}")
        print(f"PAUSED: {ask['question']}")
        print(f"Nothing has been merged. Run: python -m heal_loop resume --dir {directory} --approve | --reject")
    else:
        print(f"{result['outcome'].upper()}: {result['summary']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heal-loop", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start_cmd = commands.add_parser("start", help="create the fixture in --dir and run until approval")
    start_cmd.add_argument("--dir", type=Path, required=True, help="a new or empty directory")
    start_cmd.add_argument("--bad-fix", action="store_true", help="script a fix that does not work, to see the revert")
    resume_cmd = commands.add_parser("resume", help="give the decision to the paused run")
    resume_cmd.add_argument("--dir", type=Path, required=True)
    decision = resume_cmd.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    args = parser.parse_args(argv)

    directory: Path = args.dir.resolve()
    if args.command == "start":
        if directory.exists() and any(directory.iterdir()):
            parser.error(f"{directory} is not empty")
        directory.mkdir(parents=True, exist_ok=True)
        cfg = make_fixture(directory)
        content = STILL_BROKEN if args.bad_fix else proposal()["edits"][0]["new_content"]
        model = pick_model(ScriptedModel({"fixer": [fenced(proposal(content))], "reviewer": [fenced(review())]}))
    else:
        cfg = fixture_config(directory)
        model = ScriptedModel({})  # no model is called after the approval gate
    connection = sqlite3.connect(directory / "checkpoints.sqlite", check_same_thread=False)
    hooks, flush_traces = from_env(model, link_dir=directory)
    try:
        graph = build_graph(cfg, model, hooks, checkpointer=SqliteSaver(connection))
        result = start(graph, RUN_ID) if args.command == "start" else resume(graph, RUN_ID, args.approve)
    except RuntimeError as error:  # for example: resume when no run is waiting
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()
        flush_traces()  # before the process exits, or the last spans are lost
    report(result, directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
