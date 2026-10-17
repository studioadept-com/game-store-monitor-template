"""HTTP entry point for Cloud Run: each authenticated POST runs one check.

Cloud Scheduler calls POST /run every five minutes. The service runs at a
quarter vCPU and is billed only while a request is in progress, which keeps a
five-minute schedule inside Cloud Run's free allowance. Cloud Run checks the
caller's identity before a request arrives here, and the service allows one
request at a time, so checks never overlap.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .__main__ import main
from .providers import safe_error

# ?mode= choices; anything else is refused so a caller can't pass arbitrary flags.
MODES = {"check": [], "dry-run": ["--dry-run"], "test-telegram": ["--test-telegram"], "test-discord": ["--test-discord"]}


def run(mode):
    try:
        return main(MODES[mode])
    except Exception as error:
        print("Monitor failed: " + safe_error(error), file=sys.stderr)
        return 1


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        mode = parse_qs(urlparse(self.path).query).get("mode", ["check"])[0]
        if mode not in MODES:
            return self.reply(400, "Unknown mode; use one of: " + ", ".join(MODES))
        # 500 makes Cloud Scheduler record the run as failed, as job failures were before.
        self.reply(200, "ok") if run(mode) == 0 else self.reply(500, "Check finished with errors; see the logs")

    def reply(self, status, text):
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Cloud Run already logs every request.


if __name__ == "__main__":
    # One thread: requests are handled one after another, never side by side.
    HTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
