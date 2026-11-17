"""A tiny webhook receiver for manual tests.

    uv run python scripts/webhook_receiver.py <secret> [--port 9999] [--fail-first N]

Prints every delivery and whether its signature is valid. With --fail-first N the first N
requests are answered with HTTP 500, which shows the retry behaviour.
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from scribe.domain.webhooks import SIGNATURE_HEADER, verify_signature


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("secret")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--fail-first", type=int, default=0)
    args = parser.parse_args()
    failures_left = args.fail_first

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal failures_left
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            signature = self.headers.get(SIGNATURE_HEADER, "")
            valid = verify_signature(args.secret, signature, body, now=time.time())
            print(f"\n{time.strftime('%H:%M:%S')} {self.headers.get('X-Scribe-Event')}")
            print(f"  delivery id:     {self.headers.get('X-Scribe-Delivery')}")
            print(f"  signature valid: {valid}")
            print(f"  body: {json.dumps(json.loads(body), indent=2, ensure_ascii=False)}")
            if failures_left > 0:
                failures_left -= 1
                status = 500
                print("  -> answering 500 (--fail-first)")
            else:
                status = 200 if valid else 400
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *log_args: Any) -> None:
            return None

    print(f"listening on 0.0.0.0:{args.port}")
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
