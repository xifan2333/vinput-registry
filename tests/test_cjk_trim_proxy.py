import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Dynamically import resources/adapters/cjk-trim/proxy/entry.py
MODULE_PATH = Path(__file__).resolve().parent.parent / "resources" / "adapters" / "cjk-trim" / "proxy" / "entry.py"
spec = importlib.util.spec_from_file_location("cjk_trim_entry", MODULE_PATH)
assert spec and spec.loader
cjk = importlib.util.module_from_spec(spec)
sys.modules["cjk_trim_entry"] = cjk
spec.loader.exec_module(cjk)


class TestCjkTrimProxy(unittest.TestCase):
    def test_clean_transcript_punctuation_and_ideographs(self):
        # 1. Whitespace around CJK punctuation
        self.assertEqual(cjk.clean_transcript("你好 ， 世界 ！"), "你好，世界！")
        self.assertEqual(cjk.clean_transcript("「 引号 」 与 『 双引号 』"), "「引号」与『双引号』")

        # 2. Spaces between consecutive CJK ideographs
        self.assertEqual(cjk.clean_transcript("今 天 天 气 真 好"), "今天天气真好")

        # 3. Collapse spaces disabled
        self.assertEqual(
            cjk.clean_transcript("今 天 ， 天 气 ！", collapse_cjk_spaces=False),
            "今 天，天 气！",
        )

        # 4. English / ASCII spaces preserved
        self.assertEqual(cjk.clean_transcript("Hello world this is a test"), "Hello world this is a test")
        self.assertEqual(cjk.clean_transcript("中英 混排 Test 123 ！"), "中英混排 Test 123！")

        # 5. Empty or whitespace-only
        self.assertEqual(cjk.clean_transcript(""), "")
        self.assertEqual(cjk.clean_transcript("   \n\t  "), "")

    def test_extract_input_text(self):
        # With <vinput-asr> tag
        messages = [
            {"role": "user", "content": "Prefix\n<vinput-asr>\n你好 ， 世界\n</vinput-asr>\nSuffix"},
        ]
        self.assertEqual(cjk.extract_input_text(messages), "你好 ， 世界")

        # Without <vinput-asr> tag
        raw_messages = [
            {"role": "user", "content": "测试 文本"},
        ]
        self.assertEqual(cjk.extract_input_text(raw_messages), "测试 文本")

    def test_get_required_port(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                cjk.get_required_port()
            self.assertIn("Missing CJK_TRIM_PORT", str(ctx.exception))

        with patch.dict(os.environ, {"CJK_TRIM_PORT": "invalid"}):
            with self.assertRaises(ValueError) as ctx:
                cjk.get_required_port()
            self.assertIn("must be a valid integer", str(ctx.exception))

        with patch.dict(os.environ, {"CJK_TRIM_PORT": "0"}):
            with self.assertRaises(ValueError) as ctx:
                cjk.get_required_port()
            self.assertIn("between 1 and 65535", str(ctx.exception))

        with patch.dict(os.environ, {"CJK_TRIM_PORT": "70000"}):
            with self.assertRaises(ValueError) as ctx:
                cjk.get_required_port()
            self.assertIn("between 1 and 65535", str(ctx.exception))

        with patch.dict(os.environ, {"CJK_TRIM_PORT": " 19000 \n"}):
            self.assertEqual(cjk.get_required_port(), 19000)

    def test_get_bool_env(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cjk.get_bool_env("TEST_FLAG", default=True))
            self.assertFalse(cjk.get_bool_env("TEST_FLAG", default=False))

        for false_val in ("0", "false", "False", "no", "off"):
            with patch.dict(os.environ, {"TEST_FLAG": false_val}):
                self.assertFalse(cjk.get_bool_env("TEST_FLAG", default=True))

        for true_val in ("1", "true", "True", "yes", "on"):
            with patch.dict(os.environ, {"TEST_FLAG": true_val}):
                self.assertTrue(cjk.get_bool_env("TEST_FLAG", default=False))

    def test_make_chat_response(self):
        resp = cjk.make_chat_response("cleaned text", "custom-model")
        self.assertEqual(resp["model"], "custom-model")
        self.assertEqual(len(resp["choices"]), 1)
        content_json = json.loads(resp["choices"][0]["message"]["content"])
        self.assertEqual(content_json, {"candidates": ["cleaned text"]})

    def test_threaded_server_endpoints_and_concurrency(self):
        server = cjk.ThreadingHTTPServer(("127.0.0.1", 0), cjk.CjkTrimHandler)
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
                self.assertEqual(data["data"][0]["id"], "cjk-trim")

            # 2. Test GET /unknown (404)
            req_404 = urllib.request.Request(f"{base_url}/unknown")
            with self.assertRaises(urllib.request.HTTPError) as ctx:
                urllib.request.urlopen(req_404, timeout=5)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()

            # 3. Test POST /v1/chat/completions (concurrent requests)
            results: list[str] = []
            errors: list[Any] = []

            def send_chat(text: str):
                try:
                    payload = json.dumps(
                        {
                            "model": "cjk-trim",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": f"<vinput-asr>\n{text}\n</vinput-asr>\n\n## Constraints",
                                },
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

            t1 = threading.Thread(target=send_chat, args=("测 试 一 下 ， 可 以 吗 ？",))
            t2 = threading.Thread(target=send_chat, args=("这 是 第二 个 请求 ！",))
            t1.start()
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertIn("测试一下，可以吗？", results)
            self.assertIn("这是第二个请求！", results)

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
