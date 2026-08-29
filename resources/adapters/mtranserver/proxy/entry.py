#!/usr/bin/env python3

import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_MTRAN_URL = "http://localhost:8989"
DEFAULT_PORT = 8990

mtran_url = DEFAULT_MTRAN_URL
mtran_token = ""


def parse_target_lang(system_prompt: str) -> str:
    match = re.search(r"translate\s+to\s+([\w-]+)", system_prompt, re.IGNORECASE)
    return match.group(1) if match else "en"


def call_mtran(text: str, to_lang: str) -> str:
    body = json.dumps({"from": "auto", "to": to_lang, "text": text, "html": False}).encode()
    headers = {"Content-Type": "application/json"}
    if mtran_token:
        headers["Authorization"] = f"Bearer {mtran_token}"
    req = Request(f"{mtran_url}/translate", data=body, headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("result", "")


def make_chat_response(content: str, model: str = "mtranserver") -> dict:
    wrapped = json.dumps({"candidates": [content]})
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
    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        messages = body.get("messages", [])
        system_prompt = ""
        user_text = ""
        for message in messages:
            if message["role"] == "system":
                system_prompt = message.get("content", "")
            elif message["role"] == "user":
                user_text = message.get("content", "")

        to_lang = parse_target_lang(system_prompt)

        try:
            result = call_mtran(user_text, to_lang)
        except (URLError, Exception) as exc:
            self.send_error(502, str(exc))
            return

        resp = json.dumps(make_chat_response(result, body.get("model", "mtranserver")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp.encode())

    def do_GET(self):
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
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.encode())
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[mtranserver-proxy] {fmt % args}")


def main():
    global mtran_url, mtran_token

    port = int(os.getenv("MTRAN_PORT", DEFAULT_PORT))
    mtran_url = os.getenv("MTRAN_URL", DEFAULT_MTRAN_URL).rstrip("/")
    mtran_token = os.getenv("MTRAN_TOKEN", "")

    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[mtranserver-proxy] Listening on http://127.0.0.1:{port}")
    print(f"[mtranserver-proxy] MTranServer: {mtran_url}")
    server.serve_forever()


if __name__ == "__main__":
    main()
