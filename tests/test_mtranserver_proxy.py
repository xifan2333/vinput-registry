import importlib.util
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Dynamically import resources/adapters/mtranserver/proxy/entry.py
MODULE_PATH = Path(__file__).resolve().parent.parent / "resources" / "adapters" / "mtranserver" / "proxy" / "entry.py"
spec = importlib.util.spec_from_file_location("mtranserver_entry", MODULE_PATH)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules["mtranserver_entry"] = m
spec.loader.exec_module(m)


class TestMTranServerProxy(unittest.TestCase):
    def test_parse_target_lang(self):
        self.assertEqual(m.parse_target_lang("translate to en"), "en")
        self.assertEqual(m.parse_target_lang("Translate to zh-Hans"), "zh-Hans")
        self.assertEqual(m.parse_target_lang("Please translate to ja, thank you"), "ja")
        self.assertEqual(m.parse_target_lang("no translation instruction here"), "en")

    def test_extract_text(self):
        # 1. Standard vinput format with XML tag
        content_with_tag = (
            "translate to zh\n\n"
            "<vinput-asr>\nHello world\n</vinput-asr>\n\n"
            "## Constraints\n- Return only the JSON object."
        )
        self.assertEqual(m.extract_text(content_with_tag), "Hello world")

        # 2. Raw plain text without XML tag
        self.assertEqual(m.extract_text("Good morning"), "Good morning")

        # 3. Empty content
        self.assertEqual(m.extract_text(""), "")

    def test_get_required_port(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                m.get_required_port()
            self.assertIn("Missing MTRAN_PORT", str(ctx.exception))

        with patch.dict(os.environ, {"MTRAN_PORT": "not-a-number"}):
            with self.assertRaises(ValueError) as ctx:
                m.get_required_port()
            self.assertIn("must be a valid integer", str(ctx.exception))

        with patch.dict(os.environ, {"MTRAN_PORT": "0"}):
            with self.assertRaises(ValueError) as ctx:
                m.get_required_port()
            self.assertIn("between 1 and 65535", str(ctx.exception))

        with patch.dict(os.environ, {"MTRAN_PORT": "70000"}):
            with self.assertRaises(ValueError) as ctx:
                m.get_required_port()
            self.assertIn("between 1 and 65535", str(ctx.exception))

        with patch.dict(os.environ, {"MTRAN_PORT": " 18989 \n"}):
            self.assertEqual(m.get_required_port(), 18989)

    def test_call_mtran(self):
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"result": "Hello world"}).encode("utf-8")
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_response

        with patch.object(m, "urlopen", return_value=fake_cm) as mock_urlopen:
            with patch.object(m, "mtran_url", "http://test-mtran:8989"), patch.object(m, "mtran_token", "secret123"):
                result = m.call_mtran("你好世界", "en")
                self.assertEqual(result, "Hello world")
                self.assertEqual(mock_urlopen.call_count, 1)

                req = mock_urlopen.call_args[0][0]
                self.assertEqual(req.full_url, "http://test-mtran:8989/translate")
                self.assertEqual(req.headers.get("Authorization"), "Bearer secret123")
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body, {"from": "auto", "to": "en", "text": "你好世界", "html": False})

    def test_make_chat_response(self):
        resp = m.make_chat_response("translated text", "test-model")
        self.assertEqual(resp["model"], "test-model")
        self.assertEqual(len(resp["choices"]), 1)
        content_json = json.loads(resp["choices"][0]["message"]["content"])
        self.assertEqual(content_json, {"candidates": ["translated text"]})

    def test_threaded_server_endpoints_and_concurrency(self):
        # Bind to port 0 on loopback to allocate an unused ephemeral port
        server = m.ThreadingHTTPServer(("127.0.0.1", 0), m.ProxyHandler)
        server.daemon_threads = True
        port = server.server_port
        base_url = f"http://127.0.0.1:{port}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            # 1. Test GET /v1/models
            req = urllib.request.Request(f"{base_url}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["object"], "list")
                self.assertEqual(data["data"][0]["id"], "mtranserver")

            # 2. Test GET /unknown (404)
            req_404 = urllib.request.Request(f"{base_url}/unknown")
            with self.assertRaises(urllib.request.HTTPError) as ctx:
                urllib.request.urlopen(req_404, timeout=5)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()

            # 3. Test POST /v1/chat/completions (concurrent calls with mocked mtran)
            def fake_call_mtran(text: str, to_lang: str) -> str:
                time.sleep(0.05)
                return f"translated({text}->{to_lang})"

            with patch.object(m, "call_mtran", side_effect=fake_call_mtran):
                results: list[str] = []
                errors: list[Any] = []

                def send_chat(text: str, lang: str):
                    try:
                        payload = json.dumps(
                            {
                                "model": "mtranserver",
                                "messages": [
                                    {"role": "system", "content": f"translate to {lang}"},
                                    {"role": "user", "content": text},
                                ],
                            }
                        ).encode("utf-8")
                        r = urllib.request.Request(
                            f"{base_url}/v1/chat/completions",
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(r, timeout=5) as res:
                            body = json.loads(res.read().decode("utf-8"))
                            cand = json.loads(body["choices"][0]["message"]["content"])["candidates"][0]
                            results.append(cand)
                    except Exception as exc:
                        errors.append(exc)

                t1 = threading.Thread(target=send_chat, args=("hello", "zh"))
                t2 = threading.Thread(target=send_chat, args=("world", "ja"))
                t1.start()
                t2.start()
                t1.join(timeout=3)
                t2.join(timeout=3)

            # 4. Test POST with real fcitx5-vinput single-user payload (tag extraction + prompt parsing)
            with patch.object(m, "call_mtran", return_value="早上好") as mock_mtran:
                payload = json.dumps(
                    {
                        "model": "mtranserver",
                        "messages": [
                            {
                                "role": "user",
                                "content": "translate to zh\n\n<vinput-asr>\nGood morning\n</vinput-asr>\n\n## Constraints\n- JSON",
                            }
                        ],
                    }
                ).encode("utf-8")
                r = urllib.request.Request(
                    f"{base_url}/v1/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(r, timeout=5) as res:
                    body = json.loads(res.read().decode("utf-8"))
                    cand = json.loads(body["choices"][0]["message"]["content"])["candidates"][0]
                    self.assertEqual(cand, "早上好")
                    # Ensure mock_mtran received ONLY the clean extracted ASR text and target language 'zh'
                    mock_mtran.assert_called_once_with("Good morning", "zh")

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
