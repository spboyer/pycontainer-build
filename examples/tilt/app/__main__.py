"""Tiny dependency-free HTTP service used by the dev-loop examples."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESSAGE = os.environ.get("GREETING", "hello from pycontainer-build").encode()
PORT = int(os.environ.get("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(MESSAGE)))
        self.end_headers()
        self.wfile.write(MESSAGE)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
