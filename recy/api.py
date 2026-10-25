import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


def create_server(database, host="127.0.0.1", port=8000):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self.respond(200, {"status": "ok"})
                elif path == "/ready":
                    latest = database.status()
                    self.respond(200 if latest else 503,
                                 {"ready": latest is not None, "last_sync": latest})
                elif path == "/api/v1/schema":
                    latest = database.status()
                    if not latest:
                        self.respond(503, {"error": "Registry has not been synced"})
                    else:
                        self.respond(200, {"table": "registered_products", "columns": latest["headers"],
                                           "storage_type": "TEXT", "source_url": latest["source_url"]})
                elif path.startswith("/api/v1/upcs/"):
                    result = database.verify(unquote(path[len("/api/v1/upcs/"):]))
                    if result is None:
                        self.respond(503, {"error": "Registry has not been synced"})
                    else:
                        self.respond(200, result)
                else:
                    self.respond(404, {"error": "Route not found"})
            except ValueError as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                logging.exception("Unexpected API error")
                self.respond(500, {"error": "Internal server error"})

    return ThreadingHTTPServer((host, port), Handler)
