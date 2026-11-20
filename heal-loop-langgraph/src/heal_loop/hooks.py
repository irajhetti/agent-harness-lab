"""The one seam for tracing. Every method is a no-op.

The graph calls these around each node, each model call and each subprocess. To add tracing later
(for example OpenTelemetry spans), subclass Hooks and pass it to build_graph. Nothing else changes.

Note: a node that pauses on interrupt() never reaches on_node_end, and starts again on resume.
"""

from collections.abc import Mapping, Sequence
from typing import Any


class Hooks:
    def on_node_start(self, node: str, state: Mapping[str, Any]) -> None:
        pass

    def on_node_end(self, node: str, update: Mapping[str, Any]) -> None:
        pass

    def on_model_call(self, role: str, prompt: str, reply: str) -> None:
        pass

    def on_tool_call(self, command: Sequence[str], returncode: int) -> None:
        pass
