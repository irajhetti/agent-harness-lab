"""The one seam for tracing. Every method is a no-op.

The graph calls these around each node, each model call and each tool call (a subprocess, or the
step that writes the edits). `tracing.py` subclasses Hooks to emit OpenTelemetry spans. Nothing
else in the loop knows about tracing.

Order of calls: on_node_start, then inside it any number of on_model_start/on_model_call and
on_tool_start/[on_tool_call]/on_tool_end pairs, then exactly one of on_node_end, on_node_pause
or on_error. on_error is also called for a model or tool call that raises, before the node's own.

Note: a node that pauses on interrupt() never reaches on_node_end, and starts again on resume.
"""

from collections.abc import Mapping, Sequence
from typing import Any


class Hooks:
    def on_node_start(self, node: str, state: Mapping[str, Any]) -> None:
        pass

    def on_node_end(self, node: str, update: Mapping[str, Any]) -> None:
        pass

    def on_node_pause(self, node: str) -> None:
        """The node called interrupt(): the run stops here until a person resumes it."""

    def on_model_start(self, role: str, prompt: str) -> None:
        pass

    def on_model_call(self, role: str, prompt: str, reply: str) -> None:
        """The model replied. Called before the reply is validated."""

    def on_tool_start(self, name: str) -> None:
        """`name` is chosen by code, for example "git.commit", "apply_edits" or "rerun"."""

    def on_tool_call(self, command: Sequence[str], returncode: int) -> None:
        """A subprocess finished. Not called for a tool that is not a subprocess."""

    def on_tool_end(self, name: str) -> None:
        pass

    def on_error(self, error: BaseException) -> None:
        """The innermost open model call, tool call or node raised. The exception carries on up."""
