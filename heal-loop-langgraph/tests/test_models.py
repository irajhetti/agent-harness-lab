"""Reply parsing, the retry helper, and the Ollama client against a local stub server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from heal_loop import Hooks, OllamaModel, ScriptedModel
from heal_loop.cli import pick_model
from heal_loop.models import ask, last_json
from heal_loop.schemas import Review, Verdict


def test_last_json_takes_the_final_fenced_block_or_a_bare_object() -> None:
    assert last_json('text ```json\n{"a":1}\n``` more ```json\n{"a":2}\n```') == {"a": 2}
    assert last_json('I think {"verdict":"APPROVE","notes":"ok"}') == {"verdict": "APPROVE", "notes": "ok"}
    assert last_json('{"edits": [{"path": "a"}]}') == {"edits": [{"path": "a"}]}  # nested, nothing but JSON
    assert last_json("nothing here") is None


def test_ask_retries_once_with_the_validation_error() -> None:
    model = ScriptedModel({"reviewer": ['{"verdict": "MAYBE", "notes": "x"}', '{"verdict": "CHANGES", "notes": "y"}']})
    review, error, calls = ask(model, "reviewer", "judge this", Review, Hooks())
    assert review is not None and review.verdict is Verdict.CHANGES
    assert (error, calls) == ("", 2)
    assert model.calls[0][1] == "judge this"
    assert "Your last reply was not valid" in model.calls[1][1] and "verdict" in model.calls[1][1]


def test_ask_gives_up_after_the_second_invalid_reply() -> None:
    model = ScriptedModel({"reviewer": ["no", "still no", "a third reply that must never be asked for"]})
    review, error, calls = ask(model, "reviewer", "judge this", Review, Hooks())
    assert review is None and error and calls == 2
    assert len(model.calls) == 2


def test_ollama_model_speaks_plain_http_to_a_local_endpoint() -> None:
    seen: list[dict[str, Any]] = []

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
            seen.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            reply = json.dumps({"message": {"role": "assistant", "content": '{"verdict": "APPROVE", "notes": "ok"}'}})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply.encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = OllamaModel(f"http://127.0.0.1:{server.server_port}/", "some-local-model", timeout_s=5)
        review, _, _ = ask(model, "reviewer", "judge this", Review, Hooks())
    finally:
        server.shutdown()
        server.server_close()
    assert review is not None and review.verdict is Verdict.APPROVE
    assert seen[0]["path"] == "/api/chat"
    assert seen[0]["body"]["model"] == "some-local-model" and seen[0]["body"]["stream"] is False
    assert seen[0]["body"]["messages"][-1] == {"role": "user", "content": "judge this"}


def test_ollama_model_refuses_other_url_schemes() -> None:
    with pytest.raises(ValueError, match="http"):
        OllamaModel("file:///etc/passwd", "m")


def test_ollama_is_only_used_when_the_env_var_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    scripted = ScriptedModel({})
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert pick_model(scripted) is scripted
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    assert isinstance(pick_model(scripted), OllamaModel)
