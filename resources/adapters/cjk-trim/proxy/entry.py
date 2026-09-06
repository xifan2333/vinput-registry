#!/usr/bin/env python3
"""Managed local LLM adapter that removes redundant spaces in CJK ASR transcripts:
spaces around CJK punctuation marks and between consecutive CJK ideographs.

Operates locally as an OpenAI-compatible HTTP service. Removes unwanted spaces
around CJK punctuation marks and between consecutive CJK ideographs that some
speech recognition models (e.g. x-asr Zipformer Transducer) emit.

Scope note: ASCII / English text is intentionally left untouched — no ASCII
punctuation normalization, no interior space collapsing. Only leading/trailing
whitespace is stripped.
"""

import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

# Unicode CJK punctuation codepoint ranges (no hardcoded punctuation literals):
#   \u3000-\u303F: CJK Symbols and Punctuation (IDEOGRAPHIC FULL STOP, etc.)
#   \uFF01-\uFF65: Fullwidth ASCII Variants (FULLWIDTH COMMA, EXCLAMATION, etc.)
#   \u2000-\u206F: General Punctuation (HORIZONTAL ELLIPSIS, EM DASH, quotes)
#   \uFE10-\uFE1F: Presentation Forms for Vertical Lines
#   \uFE30-\uFE4F: CJK Compatibility Forms
CJK_PUNCT_RANGE = r"[\u3000-\u303f\uff01-\uff65\u2000-\u206f\ufe10-\ufe1f\ufe30-\ufe4f]"

# CJK Unified Ideographs & Extension ranges:
CJK_IDEOGRAPHS_RANGE = r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"


def get_required_port() -> int:
    val = os.getenv("CJK_TRIM_PORT", "").strip()
    if not val:
        raise ValueError("Missing CJK_TRIM_PORT environment variable.")
    try:
        port = int(val)
    except ValueError as exc:
        raise ValueError(f"Invalid CJK_TRIM_PORT '{val}': must be a valid integer.") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"Invalid CJK_TRIM_PORT '{val}': port number must be between 1 and 65535.")
    return port


def get_bool_env(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def clean_transcript(text: str, collapse_cjk_spaces: bool = True) -> str:
    """Remove redundant spaces around CJK punctuation and between CJK ideographs.

    ASCII content is preserved as-is (except stripping leading/trailing
    whitespace); no interior ASCII space collapsing is performed.
    """
    if not text:
        return ""

    # 1. Remove whitespace immediately after CJK punctuation
    text = re.sub(f"({CJK_PUNCT_RANGE})\\s+", r"\1", text)

    # 2. Remove whitespace immediately before CJK punctuation
    text = re.sub(f"\\s+({CJK_PUNCT_RANGE})", r"\1", text)

    # 3. Remove spaces between consecutive CJK ideographs if enabled.
    #    Loop to collapse sequences where a single pass leaves new adjacencies.
    if collapse_cjk_spaces:
        while re.search(f"({CJK_IDEOGRAPHS_RANGE})\\s+({CJK_IDEOGRAPHS_RANGE})", text):
            text = re.sub(f"({CJK_IDEOGRAPHS_RANGE})\\s+({CJK_IDEOGRAPHS_RANGE})", r"\1\2", text)

    # 4. Strip leading/trailing whitespace only. Interior ASCII spacing is kept.
    return text.strip()


def extract_input_text(messages: list[dict[str, Any]]) -> str:
    """Extract transcription text from messages, preferring <vinput-asr> tags."""
    combined = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                combined += content + "\n"

    # Extract text wrapped in <vinput-asr>...</vinput-asr> if present
    match = re.search(r"<vinput-asr>(.*?)</vinput-asr>", combined, re.DOTALL)
    if match:
        return match.group(1).strip()

    return combined.strip()


def make_chat_response(content: str, model: str = "cjk-trim") -> dict[str, Any]:
    """Wrap cleaned text in candidates JSON expected by vinput-daemon."""
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


class CjkTrimHandler(BaseHTTPRequestHandler):
    collapse_cjk_spaces: bool = True

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
        raw_text = extract_input_text(messages)
        cleaned_text = clean_transcript(raw_text, collapse_cjk_spaces=self.collapse_cjk_spaces)

        resp_obj = make_chat_response(cleaned_text, body.get("model", "cjk-trim"))
        payload = json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            resp_obj = {
                "object": "list",
                "data": [
                    {
                        "id": "cjk-trim",
                        "object": "model",
                        "owned_by": "vinput",
                    }
                ],
            }
            payload = json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")
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
        sys.stderr.write(f"[cjk-trim] {format % args}\n")
        sys.stderr.flush()


def main() -> int:
    try:
        port = get_required_port()
    except ValueError as exc:
        sys.stderr.write(f"[cjk-trim] {exc}\n")
        sys.stderr.flush()
        return EXIT_USAGE_ERROR

    CjkTrimHandler.collapse_cjk_spaces = get_bool_env("CJK_TRIM_COLLAPSE_CJK_SPACES", default=True)

    try:
        server = HTTPServer(("127.0.0.1", port), CjkTrimHandler)
    except Exception as exc:
        sys.stderr.write(f"[cjk-trim] Failed to bind port {port}: {exc}\n")
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
