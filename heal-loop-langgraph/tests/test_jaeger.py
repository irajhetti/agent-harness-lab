"""The Jaeger reader: flattening an OTLP JSON reply and drawing the tree. No Jaeger is needed here."""

from typing import Any

from heal_loop.jaeger import summarise, tree


def value(v: str | int | bool) -> dict[str, Any]:
    kind = "boolValue" if isinstance(v, bool) else "intValue" if isinstance(v, int) else "stringValue"
    return {kind: str(v) if kind == "intValue" else v}  # OTLP JSON sends integers as strings


def span(name: str, span_id: str, parent: str, start: int, end: int, **more: Any) -> dict[str, Any]:
    attributes = [{"key": key.replace("__", "."), "value": value(v)} for key, v in more.pop("attributes", {}).items()]
    base = {"traceId": "t1", "spanId": span_id, "name": name, "kind": 1, "attributes": attributes, "status": {}}
    times = {"startTimeUnixNano": str(start), "endTimeUnixNano": str(end)}
    return base | times | ({"parentSpanId": parent} if parent else {}) | more


REPLY = [
    {
        "resource": {"attributes": [{"key": "service.name", "value": value("heal-loop")}]},
        "scopeSpans": [
            {
                "scope": {"name": "heal_loop"},
                "spans": [  # deliberately out of order, as a store may return them
                    span(
                        "execute_tool rerun",
                        "c",
                        "b",
                        2_000_000,
                        7_500_000,
                        attributes={"heal.run_id": "demo", "heal.round": 1, "process.exit.code": 1},
                        status={"code": 2, "message": "exit code 1"},
                    ),
                    span(
                        "heal_loop.resume",
                        "a",
                        "",
                        0,
                        9_000_000,
                        attributes={"heal.run_id": "demo", "heal.paused": False},
                        links=[{"traceId": "t0", "spanId": "z", "attributes": []}],
                    ),
                    span(
                        "node merge_and_rerun",
                        "b",
                        "a",
                        1_000_000,
                        8_000_000,
                        attributes={"heal.run_id": "demo", "heal.round": 1},
                    ),
                ],
            }
        ],
    }
]


def test_summarise_flattens_an_otlp_reply_in_start_order() -> None:
    trace = summarise(REPLY)
    assert (trace["trace_id"], trace["span_count"], trace["resource"]) == ("t1", 3, {"service.name": "heal-loop"})
    assert [(s["name"], s["parent_span_id"]) for s in trace["spans"]] == [
        ("heal_loop.resume", None),
        ("node merge_and_rerun", "a"),
        ("execute_tool rerun", "b"),
    ]
    rerun = trace["spans"][2]
    assert (rerun["start_offset_ms"], rerun["duration_ms"]) == (2.0, 5.5)
    assert (rerun["status"], rerun["status_message"], rerun["kind"]) == ("ERROR", "exit code 1", "INTERNAL")
    assert rerun["attributes"] == {"heal.round": 1, "heal.run_id": "demo", "process.exit.code": 1}
    assert trace["spans"][0]["attributes"]["heal.paused"] is False
    assert trace["spans"][0]["links"] == [{"trace_id": "t0", "span_id": "z", "attributes": {}}]


def test_tree_indents_children_and_leaves_out_what_the_parent_already_says() -> None:
    assert tree(summarise(REPLY)).splitlines() == [
        "trace t1  service=heal-loop  spans=3",
        "heal_loop.resume  9.0 ms  heal.paused=False  heal.run_id=demo",
        "  link -> trace t0 span z",
        "  node merge_and_rerun  7.0 ms  heal.round=1",
        "    execute_tool rerun  5.5 ms  ERROR(exit code 1)  process.exit.code=1",
    ]
