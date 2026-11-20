"""Read one run's two traces back from a local Jaeger and print the span tree. Used by `make trace-demo`.

    python -m heal_loop.jaeger <dir>/trace-<run_id>.json [--jaeger http://127.0.0.1:16686] [--json FILE] [--text FILE]

The argument is the link file that tracing.py writes next to the run. It names the start trace. The
resume trace is found by its `heal.start_trace_id` attribute. Exits 1 if either trace is missing.
Jaeger's /api/v3 answers in OTLP JSON, which `summarise` flattens.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KINDS = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
# The text tree leaves these out: the first three are in the span name, the rest only when the parent says the same.
IN_THE_NAME = {"gen_ai.operation.name", "gen_ai.tool.name", "gen_ai.request.model"}
REPEATED = {"heal.run_id", "heal.node", "heal.round"}


def fetch(url: str) -> list[dict[str, Any]]:
    """The resourceSpans of a /api/v3/traces reply. Empty when Jaeger says there is no such trace."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("the Jaeger address must start with http:// or https://")
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - scheme checked above
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise
    found: list[dict[str, Any]] = []
    decoder, at = json.JSONDecoder(), 0
    while at < len(text):  # a large reply arrives as several JSON objects, one after another
        chunk, at = decoder.raw_decode(text, at)
        found += chunk.get("result", {}).get("resourceSpans", [])
        at += len(text[at:]) - len(text[at:].lstrip())
    return found


def _plain(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """OTLP key/value pairs as a plain dict."""
    plain = {}
    for pair in pairs:
        (kind, value), *_ = pair["value"].items() or [("", None)]
        plain[pair["key"]] = int(value) if kind == "intValue" else value
    return dict(sorted(plain.items()))


def summarise(resource_spans: list[dict[str, Any]]) -> dict[str, Any]:
    """One trace as plain data: spans in start order, with parent ids, durations and attributes."""
    raw = [span for resource in resource_spans for scope in resource["scopeSpans"] for span in scope["spans"]]
    raw.sort(key=lambda span: int(span["startTimeUnixNano"]))
    began = int(raw[0]["startTimeUnixNano"])
    spans = []
    for span in raw:
        status = span.get("status", {})
        spans.append(
            {
                "name": span["name"],
                "span_id": span["spanId"],
                "parent_span_id": span.get("parentSpanId") or None,
                "kind": KINDS.get(span.get("kind", 0), str(span.get("kind"))),
                "start_offset_ms": round((int(span["startTimeUnixNano"]) - began) / 1e6, 3),
                "duration_ms": round((int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6, 3),
                "status": STATUS.get(status.get("code", 0), str(status.get("code"))),
                "status_message": status.get("message", ""),
                "attributes": _plain(span.get("attributes", [])),
                "events": [event["name"] for event in span.get("events", [])],
                "links": [
                    {"trace_id": link["traceId"], "span_id": link["spanId"], "attributes": _plain(link["attributes"])}
                    for link in span.get("links", [])
                ],
            }
        )
    return {
        "trace_id": raw[0]["traceId"],
        "resource": _plain(resource_spans[0]["resource"]["attributes"]),
        "span_count": len(spans),
        "spans": spans,
    }


def tree(trace: dict[str, Any]) -> str:
    """An indented text tree of one summarised trace. The JSON summary has every attribute; this is the short view."""
    by_id = {span["span_id"]: span for span in trace["spans"]}
    lines = [f"trace {trace['trace_id']}  service={trace['resource'].get('service.name')}  spans={trace['span_count']}"]

    def walk(span: dict[str, Any], depth: int) -> None:
        inherited = by_id.get(span["parent_span_id"], {}).get("attributes", {})
        shown = [
            f"{key}={value}"
            for key, value in span["attributes"].items()
            if key not in IN_THE_NAME and not (key in REPEATED and inherited.get(key) == value)
        ]
        error = [f"ERROR({span['status_message']})"] if span["status"] == "ERROR" else []
        lines.append("  ".join(["  " * depth + span["name"], f"{span['duration_ms']:.1f} ms", *error, *shown]))
        for link in span["links"]:
            lines.append("  " * (depth + 1) + f"link -> trace {link['trace_id']} span {link['span_id']}")
        for child in trace["spans"]:
            if child["parent_span_id"] == span["span_id"]:
                walk(child, depth + 1)

    for span in trace["spans"]:
        if span["parent_span_id"] not in by_id:
            walk(span, 0)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heal_loop.jaeger", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("link_file", type=Path, help="<dir>/trace-<run_id>.json, written by a traced start")
    parser.add_argument("--jaeger", default="http://127.0.0.1:16686", help="Jaeger query address")
    parser.add_argument("--json", type=Path, help="also write the summary here as JSON")
    parser.add_argument("--text", type=Path, help="also write the text tree here")
    args = parser.parse_args(argv)

    first = json.loads(args.link_file.read_text())
    api = args.jaeger.rstrip("/") + "/api/v3/traces"
    now = datetime.now(UTC)
    start: list[dict[str, Any]] = []
    resume: list[dict[str, Any]] = []
    for _ in range(10):  # the spans were sent a moment ago; give Jaeger a little time to index them
        start = start or fetch(f"{api}/{first['trace_id']}")
        if start and not resume:
            query = {
                "query.service_name": _plain(start[0]["resource"]["attributes"])["service.name"],
                "query.start_time_min": (now - timedelta(hours=1)).isoformat(),
                "query.start_time_max": (now + timedelta(minutes=5)).isoformat(),
                "query.attributes": json.dumps({"heal.start_trace_id": first["trace_id"]}),
            }
            resume = fetch(f"{api}?{urllib.parse.urlencode(query)}")
        if start and resume:
            break
        time.sleep(1)
    if not start or not resume:
        missing = "start" if not start else "resume"
        print(f"error: the {missing} trace for {first['trace_id']} is not in Jaeger at {args.jaeger}", file=sys.stderr)
        return 1

    traces = [summarise(start), summarise(resume)]
    linked = any(link["span_id"] == first["span_id"] for span in traces[1]["spans"] for link in span["links"])
    text = "\n\n".join(tree(trace) for trace in traces)
    text += f"\n\nresume root links to the start root span: {'yes' if linked else 'NO'}"
    print(text)
    if args.json:
        summary = {"source": f"{api} (OTLP JSON, flattened by heal_loop.jaeger)", "traces": traces}
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    if args.text:
        args.text.write_text(text + "\n")
    return 0 if linked else 1


if __name__ == "__main__":
    raise SystemExit(main())
