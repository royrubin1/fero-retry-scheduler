"""A fake downstream for trying the service locally. Not part of the service.

    python demo/downstream.py        # listens on http://127.0.0.1:9000

    POST /ok      200: the job is completed
    POST /bad     400: the job is failed at once, no retry
    POST /down    503 every time: the job is dead after MAX_ATTEMPTS
    POST /flaky   503 for the first two attempts of each job, then 200

Every request is printed with its Idempotency-Key, so you can watch the retries arrive.
"""
import collections
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = os.environ.get("HOST", "127.0.0.1")  # 0.0.0.0 inside a container
PORT = int(os.environ.get("PORT", "9000"))
FIXED = {"/ok": 200, "/bad": 400, "/down": 503}

calls = collections.Counter()  # requests received per Idempotency-Key


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        key = self.headers.get("Idempotency-Key")
        calls[key] += 1
        if self.path == "/flaky":
            code = 503 if calls[key] <= 2 else 200
        else:
            code = FIXED.get(self.path, 404)
        self.send_response(code)
        self.end_headers()
        print(f"POST {self.path} key={key} call={calls[key]} body={body.decode()} -> {code}", flush=True)

    def log_message(self, *args):  # the print above replaces the default access log
        pass


if __name__ == "__main__":
    print(f"demo downstream on http://{HOST}:{PORT}", flush=True)
    HTTPServer((HOST, PORT), Handler).serve_forever()
