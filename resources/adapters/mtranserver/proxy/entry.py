#!/usr/bin/env python3
"""Managed local LLM adaptor that exposes MTranServer through an OpenAI-compatible API."""

import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2


def get_required_port() -> int:
    val = os.getenv("MTRAN_PORT", "").strip()
    if not val:
        raise ValueError("Missing MTRAN_PORT environment variable.")
    try:
        port = int(val)
    except ValueError as exc:
        raise ValueError(f"Invalid MTRAN_PORT '{val}': must be a valid integer.") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"Invalid MTRAN_PORT '{val}': port number must be between 1 and 65535.")
    return port


def parse_target_lang(text: str) -> str:
    match = re.search(r"translate\s+to\s+([\w-]+)", text, re.IGNORECASE)
    return match.group(1) if match else "en"


def extract_text(content: str) -> str:
    match = re.search(r"<vinput-asr>\n?(.*?)\n?</vinput-asr>", content, re.DOTALL)
    return match.group(1).strip() if match else content.strip()


def call_mtran(text: str, to_lang: str) -> str:
    url = os.getenv("MTRAN_URL", "").strip().rstrip("/") or "http://127.0.0.1:8989"
    token = os.getenv("MTRAN_TOKEN", "").strip()

    body = json.dumps({"from": "auto", "to": to_lang, "text": text, "html": False}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{url}/translate", data=body, headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("result", "")


def make_chat_response(content: str, model: str = "mtranserver") -> dict[str, Any]:
    wrapped = json.dumps({"candidates": [content]}, ensure_ascii=False)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": wrapped},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404, "Endpoint not found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self.send_error(400, f"Invalid JSON payload: {exc}")
            return

        messages = body.get("messages", [])
        content = "\n".join(msg.get("content", "") for msg in messages if isinstance(msg, dict))
        to_lang = parse_target_lang(content)
        user_text = extract_text(content)

        if not user_text:
            result = ""
        else:
            try:
                result = call_mtran(user_text, to_lang)
            except (URLError, Exception) as exc:
                self.send_error(502, str(exc))
                return

        resp = json.dumps(make_chat_response(result, body.get("model", "mtranserver")), ensure_ascii=False)
        payload = resp.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            resp = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "mtranserver",
                            "object": "model",
                            "owned_by": "mtranserver",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            payload = resp.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404, "Endpoint not found")

    def log_message(self, format: str, *args: Any) -> None:
        """Override BaseHTTPRequestHandler.log_message.

        Called implicitly by BaseHTTPRequestHandler.send_response -> log_request.
        Suppresses normal HTTP 200 access logs from polluting stderr, preventing
        vinput-daemon from treating access logs as desktop error notifications.
        """
        pass

    def log_error(self, format: str, *args: Any) -> None:
        """Override BaseHTTPRequestHandler.log_error.

        Called implicitly by BaseHTTPRequestHandler.send_error on 4xx/5xx responses.
        Because log_message is suppressed above, this ensures genuine HTTP errors
        are still routed to stderr with the adapter prefix for troubleshooting.
        """
        sys.stderr.write(f"[mtranserver-proxy] {format % args}\n")
        sys.stderr.flush()


def main() -> int:
    try:
        port = get_required_port()
    except ValueError as exc:
        sys.stderr.write(f"[mtranserver-proxy] {exc}\n")
        sys.stderr.flush()
        return EXIT_USAGE_ERROR

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
        server.daemon_threads = True
    except Exception as exc:
        sys.stderr.write(f"[mtranserver-proxy] Failed to bind port {port}: {exc}\n")
        sys.stderr.flush()
        return EXIT_RUNTIME_ERROR

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
