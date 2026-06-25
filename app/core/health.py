"""Minimal stdlib HTTP server exposing /healthz, /ready, and /internal/investigate.

Celery and the SQS consumer have no web framework and no HTTP port. K8s
liveness/readiness probes need an HTTP endpoint, so this runs a tiny
http.server on a daemon thread alongside the real process — no new
dependency, no interference with Celery's own event loop / the consumer's
polling loop. /internal/investigate piggybacks on the same server rather
than introducing FastAPI/uvicorn just for one synchronous endpoint:
agora-api's chat.py calls it directly (not via Celery/SQS) because a chat
request needs a request/response cycle, not async dispatch.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.config import settings

logger = logging.getLogger(__name__)

_ready = threading.Event()


def mark_ready() -> None:
    """Call once the process has finished startup and is doing real work."""
    _ready.set()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, b"ok")
        elif self.path == "/ready":
            if _ready.is_set():
                self._respond(200, b"ok")
            else:
                self._respond(503, b"not ready")
        else:
            self._respond(404, b"not found")

    def do_POST(self) -> None:
        if self.path == "/internal/investigate":
            self._handle_investigate()
        else:
            self._respond(404, b"not found")

    def _handle_investigate(self) -> None:
        if not settings.INTERNAL_API_KEY or self.headers.get("X-Internal-Api-Key") != settings.INTERNAL_API_KEY:
            self._respond(403, b"forbidden")
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            question = payload["question"]
            history = payload.get("history") or []
        except (json.JSONDecodeError, KeyError):
            self._respond(400, b"invalid request body")
            return

        try:
            from app.agents.investigator import investigate

            result = investigate(question, history=history)
            self._respond_json(200, result)
        except Exception as exc:
            logger.exception("investigate failed for question %r: %s", question, exc)
            self._respond_json(500, {"error": str(exc)})

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def start_health_server(port: int = 8080) -> ThreadingHTTPServer:
    """Start the health server on a daemon thread and return it."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on :%d (/healthz, /ready, /internal/investigate)", port)
    return server
