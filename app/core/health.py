import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.config import settings

logger = logging.getLogger(__name__)

_ready = threading.Event()

def mark_ready() -> None:
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
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on :%d (/healthz, /ready, /internal/investigate)", port)
    return server
