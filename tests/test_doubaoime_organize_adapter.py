import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parent.parent / "resources" / "adapters" / "doubaoime" / "organize" / "entry.py"
spec = importlib.util.spec_from_file_location("doubaoime_organize_entry", MODULE_PATH)
assert spec and spec.loader
organize = importlib.util.module_from_spec(spec)
sys.modules["doubaoime_organize_entry"] = organize
spec.loader.exec_module(organize)


class TestDoubaoImeOrganizeAdapter(unittest.TestCase):
    def test_extract_text_with_tag_variations(self):
        # Case 1: 3 newlines (as produced by fcitx5-vinput builtin __raw__ scene)
        body1 = {
            "messages": [
                {
                    "role": "user",
                    "content": "<vinput-asr>\n今天的天气那个真是太好了\n</vinput-asr>\n\n\n## Constraints\n- Return only JSON",
                }
            ]
        }
        self.assertEqual(organize._extract_text(body1), "今天的天气那个真是太好了")

        # Case 2: 2 newlines (interpolated template)
        body2 = {
            "messages": [
                {
                    "role": "user",
                    "content": "<vinput-asr>\n今天的天气那个真是太好了\n</vinput-asr>\n\n## Constraints\n- Return only JSON",
                }
            ]
        }
        self.assertEqual(organize._extract_text(body2), "今天的天气那个真是太好了")

        # Case 3: With context prefix instructions before the tag
        body3 = {
            "messages": [
                {
                    "role": "user",
                    "content": "Recent history:\nPrevious text\n\n<vinput-asr>\n会议改到两点\n</vinput-asr>\n\n## Constraints\n",
                }
            ]
        }
        self.assertEqual(organize._extract_text(body3), "会议改到两点")

        # Case 4: Multiple messages (e.g. system + user)
        body4 = {
            "messages": [
                {"role": "system", "content": "You are an assistant."},
                {"role": "user", "content": "<vinput-asr>测试文本</vinput-asr>"},
            ]
        }
        self.assertEqual(organize._extract_text(body4), "测试文本")

    def test_extract_text_missing_wrapper_raises(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "没有tag的普通文本",
                }
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            organize._extract_text(body)
        self.assertIn("Missing <vinput-asr> wrapper", str(ctx.exception))

    def test_extract_text_empty_enclosed_raises(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "<vinput-asr>\n   \n</vinput-asr>\n\n## Constraints",
                }
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            organize._extract_text(body)
        self.assertIn("Enclosed ASR text is empty", str(ctx.exception))

    def test_extract_text_rejects_streaming(self):
        body = {
            "stream": True,
            "messages": [{"role": "user", "content": "<vinput-asr>文本</vinput-asr>"}],
        }
        with self.assertRaises(ValueError) as ctx:
            organize._extract_text(body)
        self.assertIn("Only non-streaming chat completions are supported", str(ctx.exception))

    def test_remove_trailing(self):
        self.assertEqual(organize._remove_trailing("你好，世界！"), "你好，世界")
        self.assertEqual(organize._remove_trailing("我说：【好的】"), "我说：【好的】")
        self.assertEqual(organize._remove_trailing("进度 100%"), "进度 100%")
        self.assertEqual(organize._remove_trailing("第一行。\n第二行！"), "第一行\n第二行")

    def test_chat_response_structure(self):
        resp = organize._chat_response("整理结果", model="custom-model")
        self.assertEqual(resp["model"], "custom-model")
        self.assertEqual(len(resp["choices"]), 1)
        content = json.loads(resp["choices"][0]["message"]["content"])
        self.assertEqual(content, {"candidates": ["整理结果"]})

    def test_threaded_server_endpoints(self):
        server = organize.OrganizationServer(("127.0.0.1", 0), organize.Handler)
        server.daemon_threads = True
        server.identity = {"device_id": "test", "install_id": "test"}
        server.no_trailing = False
        server.crypto = organize.CryptoCache()
        port = server.server_port
        base_url = f"http://127.0.0.1:{port}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            # 1. GET /v1/models
            req = urllib.request.Request(f"{base_url}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["object"], "list")
                self.assertEqual(data["data"][0]["id"], "doubaoime-organize")

            # 2. POST /v1/chat/completions without wrapper tag returns 400 and logs to stderr
            stderr_captured = []
            with patch.object(organize.sys.stderr, "write", side_effect=stderr_captured.append):
                req_400 = urllib.request.Request(
                    f"{base_url}/v1/chat/completions",
                    data=json.dumps({"messages": [{"role": "user", "content": "plain text"}]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req_400, timeout=5)
                self.assertEqual(ctx.exception.code, 400)
                ctx.exception.close()
                self.assertTrue(any("[doubaoime-organize]" in line for line in stderr_captured))

            # 3. POST /v1/chat/completions with valid wrapper returns 200
            with patch.object(organize, "_organize", return_value="整理后的句子。"):
                payload = json.dumps(
                    {
                        "model": "doubaoime-organize",
                        "messages": [
                            {
                                "role": "user",
                                "content": "<vinput-asr>\n那个就是测试文本\n</vinput-asr>\n\n\n## Constraints\n",
                            }
                        ],
                    }
                ).encode("utf-8")
                req_ok = urllib.request.Request(
                    f"{base_url}/v1/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req_ok, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    cand = json.loads(body["choices"][0]["message"]["content"])["candidates"][0]
                    self.assertEqual(cand, "整理后的句子。")

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
