"""Fake OpenAI-compatible model server for the pi conformance probes.

The REAL pinned pi CLI talks to this instead of a serve, so a probe tests
pi itself (guard.ts, truncation, parallel tools, steer, abort_bash) with no
live model. Each POST /v1/chat/completions pops the next scripted reply and
streams it as SSE; every request body is kept in ``requests`` so a probe can
read what pi actually sent (system prompt, tools, tool results).

Script items (dicts):
    {"text": "..."}                         a plain reply, finish_reason=stop
    {"tools": [{"name": n, "args": {...}}]}  one message with N tool calls
    {"text": "...", "finish": "length"}      a reply cut on max_tokens
    {"status": 404, "error": "..."}          an HTTP error, no stream
An exhausted script answers "FAKE-DONE" so a turn always ends.

GET /v1/models lists ``served`` (default ["fake-model"]).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeModel:
    def __init__(self, script: list[dict[str, Any]] | None = None,
                 served: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.served = served or ["fake-model"]
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    body = json.dumps({"object": "list", "data": [
                        {"id": m, "object": "model"} for m in outer.served]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                with outer._lock:
                    outer.requests.append(req)
                    item = outer.script.pop(0) if outer.script else {"text": "FAKE-DONE"}
                    outer._calls += 1
                    call = outer._calls
                if "status" in item:
                    body = json.dumps({"error": {"message": item.get("error", "error"),
                                                 "type": "invalid_request_error"}}).encode()
                    self.send_response(item["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for chunk in outer._chunks(item, call, req.get("model") or "fake-model"):
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _chunks(self, item: dict[str, Any], call: int, model: str):
        base = {"id": f"fake-{call}", "object": "chat.completion.chunk",
                "created": 0, "model": model}

        def ch(delta, finish=None):
            return {**base, "choices": [{"index": 0, "delta": delta,
                                         "finish_reason": finish}]}

        yield ch({"role": "assistant", "content": ""})
        text = item.get("text")
        if text:
            yield ch({"content": text})
        tools = item.get("tools") or []
        for i, t in enumerate(tools):
            yield ch({"tool_calls": [{
                "index": i, "id": t.get("id") or f"call_{call}_{i}", "type": "function",
                "function": {"name": t["name"], "arguments": json.dumps(t.get("args") or {})}}]})
        finish = item.get("finish") or ("tool_calls" if tools else "stop")
        yield ch({}, finish)
        yield {**base, "choices": [], "usage": {
            "prompt_tokens": 1000 + call, "completion_tokens": 20, "total_tokens": 1020 + call}}

    def start(self) -> "FakeModel":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def provider_map(self, provider: str = "fake", model: str = "fake-model",
                     **model_fields: Any) -> dict[str, Any]:
        """A pi models.json routing ``provider/model`` to this server."""
        entry = {"id": model, "name": model, "reasoning": False, "input": ["text"],
                 "contextWindow": 131072, "maxTokens": 4096,
                 "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
        entry.update(model_fields)
        return {"providers": {provider: {
            "baseUrl": self.base_url, "api": "openai-completions", "apiKey": "fake",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False,
                       "maxTokensField": "max_tokens"},
            "models": [entry]}}}
