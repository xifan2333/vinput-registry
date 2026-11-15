"""Request shape and output selection for the AssemblyAI Dictation batch provider."""

import importlib.util
import json
import os
import sys
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/providers/assemblyai/batch/entry.py"
spec = importlib.util.spec_from_file_location("assemblyai_batch_entry", ENTRY)
assert spec and spec.loader
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)

PCM = b"\x01\x00" * 1600


class FakeDictation:
    """Local HTTP server that records one request and replies with a fixed status and body."""

    def __init__(self, status: int, body: dict):
        self.request: dict = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                message = BytesParser(policy=default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw
                )
                fake.request = {
                    "path": self.path,
                    "authorization": self.headers["Authorization"],
                    "parts": {
                        part.get_param("name", header="content-disposition"): (
                            part.get_content_type(),
                            part.get_payload(decode=True),
                        )
                        for part in message.iter_parts()
                    },
                }
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/transcribe"
        threading.Thread(target=self.server.handle_request, daemon=True).start()

    def close(self):
        self.server.server_close()


def run(env: dict) -> tuple[int, str, str]:
    stdin = type("Stdin", (), {"buffer": BytesIO(PCM)})()
    stdout, stderr = StringIO(), StringIO()
    with patch.dict(os.environ, env, clear=False), patch.object(sys, "stdin", stdin):
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = provider.main()
    return code, stdout.getvalue(), stderr.getvalue()


class AssemblyAIBatchTests(unittest.TestCase):
    def call(self, status=200, body=None, **env):
        server = FakeDictation(status, body or {"text": "um so the meeting", "llm_response": "So the meeting."})
        self.addCleanup(server.close)
        base = {
            "VINPUT_ASR_API_KEY": "aai-key",
            "VINPUT_ASR_URL": server.url,
            "VINPUT_ASR_LANGUAGE": "",
            "VINPUT_ASR_ASSEMBLYAI_VERBATIM": "",
        }
        return (*run({**base, **env}), server.request)

    def test_sends_raw_pcm_with_its_format_in_a_json_config_part(self):
        code, _, _, request = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(request["path"], "/v1/transcribe")
        self.assertEqual(request["authorization"], "aai-key")
        self.assertEqual(request["parts"]["audio"], ("audio/pcm", PCM))
        content_type, config = request["parts"]["config"]
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(config), {"sample_rate": 16000, "channels": 1})

    def test_language_is_sent_as_language_codes(self):
        _, _, _, request = self.call(VINPUT_ASR_LANGUAGE="de")
        self.assertEqual(json.loads(request["parts"]["config"][1])["language_codes"], ["de"])

    def test_outputs_the_cleaned_up_text(self):
        _, out, _, _ = self.call()
        self.assertEqual(out, "So the meeting.")

    def test_falls_back_to_the_transcript_when_the_cleanup_is_empty(self):
        _, out, _, _ = self.call(body={"text": "so the meeting", "llm_response": ""})
        self.assertEqual(out, "so the meeting")

    def test_verbatim_outputs_the_words_as_spoken(self):
        _, out, _, _ = self.call(VINPUT_ASR_ASSEMBLYAI_VERBATIM="true")
        self.assertEqual(out, "um so the meeting")

    def test_http_error_message_goes_to_stderr(self):
        code, out, err, _ = self.call(status=401, body={"error": "Invalid API key"})
        self.assertEqual(code, provider.EXIT_RUNTIME_ERROR)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "AssemblyAI HTTP 401: Invalid API key")

    def test_missing_api_key_is_a_usage_error(self):
        code, _, err = run({"VINPUT_ASR_API_KEY": ""})
        self.assertEqual(code, provider.EXIT_USAGE_ERROR)
        self.assertEqual(err.strip(), "Missing VINPUT_ASR_API_KEY.")


if __name__ == "__main__":
    unittest.main()
