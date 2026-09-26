"""Regression tests for the iFlytek streaming result protocol."""

import base64
import importlib.util
import json
import os
import sys
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/providers/xfyun/streaming/entry.py"
spec = importlib.util.spec_from_file_location("xfyun_streaming_entry", ENTRY)
assert spec and spec.loader
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


def response(sn, text, *, pgs="apd", rg=None, status=1, rst="pgs", ls=False):
    decoded = {"sn": sn, "pgs": pgs, "rst": rst, "ls": ls, "ws": [{"cw": [{"w": text}]}]}
    if rg is not None:
        decoded["rg"] = rg
    return json.dumps(
        {
            "header": {"code": 0, "sid": "test-session", "status": status},
            "payload": {
                "result": {
                    "compress": "raw",
                    "status": status,
                    "text": base64.b64encode(json.dumps(decoded, ensure_ascii=False).encode()).decode(),
                }
            },
        }
    ).encode()


class XfyunStreamingTests(unittest.TestCase):
    def test_default_requests_realtime_correction_and_disables_vad(self):
        with patch.dict(
            os.environ,
            {
                "VINPUT_ASR_ENABLE_WPGS": "",
                "VINPUT_ASR_SVAD": "",
            },
        ):
            parameters = provider.build_iat_parameters()
            self.assertEqual(parameters["dwa"], "wpgs")
            self.assertEqual(parameters["svad"], 0)
        with patch.dict(os.environ, {"VINPUT_ASR_ENABLE_WPGS": "false", "VINPUT_ASR_SVAD": "1"}):
            parameters = provider.build_iat_parameters()
            self.assertNotIn("dwa", parameters)
            self.assertEqual(parameters["language"], "zh_cn")
            self.assertEqual(parameters["accent"], "mandarin")
            self.assertEqual(parameters["svad"], 1)
        with patch.dict(os.environ, {"VINPUT_ASR_SVAD": "2"}):
            with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
                provider.build_iat_parameters()

    def test_v2_endpoint_contract_defaults(self):
        parameters = provider.build_iat_parameters()
        self.assertEqual(parameters["domain"], "spark_asr_v2")
        self.assertEqual(parameters["language"], "zh_cn")
        self.assertEqual(parameters["accent"], "mandarin")
        self.assertEqual(provider.DEFAULT_URL, "wss://iat.cn-huabei-1.xf-yun.com/v1")

    def test_replacements_update_partial_without_premature_final(self):
        stream = provider.ActiveStream.__new__(provider.ActiveStream)
        stream.state = provider.ProviderState()
        stream.segments = {}
        stream.final_event = threading.Event()
        events = []
        with patch.object(provider, "write_stdout", side_effect=events.append):
            stream._handle_message(response(1, "这个"))
            stream._handle_message(response(2, "这个崩", pgs="rpl", rg=[1, 1]))
            stream._handle_message(response(3, "这个崩溃", pgs="rpl", rg=[1, 2]))
            stream._handle_message(response(4, "这个崩溃是不是似曾相识。", pgs="rpl", rg=[1, 3], rst="rlt"))
            self.assertFalse(stream.state.final_received)
            self.assertNotIn("final", [event["type"] for event in events])
            self.assertEqual(events[-1]["text"], "这个崩溃是不是似曾相识。")
            stream._handle_message(response(5, "", status=2, rst="rlt", ls=True))
            self.assertTrue(stream.state.final_received)
            provider.emit_final(stream.state)
            provider.emit_final(stream.state)
        self.assertEqual([event["type"] for event in events].count("final"), 1)
        self.assertEqual(events[-1]["text"], "这个崩溃是不是似曾相识。")

    def test_clean_close_after_partial_reports_error_and_preserves_text(self):
        stream = provider.ActiveStream.__new__(provider.ActiveStream)
        stream.state = provider.ProviderState(latest_text="已经识别")
        stream.stop_event = threading.Event()
        stream.final_event = threading.Event()
        stream.client = MagicMock()
        stream.client.recv_message.return_value = None
        events = []
        with patch.object(provider, "write_stdout", side_effect=events.append):
            stream._reader()
            self.assertTrue(stream.final_event.is_set())
            self.assertEqual([event["type"] for event in events], ["error"])
            self.assertEqual(stream.state.latest_text, "已经识别")
            provider.emit_final(stream.state)
        self.assertEqual(events[-1]["text"], "已经识别")

    def test_write_failure_emits_error_and_closed_without_retry(self):
        stream = MagicMock()
        stream.send_audio.side_effect = OSError("broken socket")
        audio = base64.b64encode(b"\x00\x01").decode()
        events = []
        with (
            patch.dict(
                os.environ,
                {
                    "VINPUT_ASR_APP_ID": "test-app",
                    "VINPUT_ASR_API_KEY": "test-key",
                    "VINPUT_ASR_API_SECRET": "test-secret",
                },
            ),
            patch.object(provider, "build_auth_url", return_value="ws://localhost/v1"),
            patch.object(provider, "ActiveStream", return_value=stream),
            patch.object(provider, "write_stdout", side_effect=events.append),
            patch.object(sys, "stdin", StringIO(json.dumps({"type": "audio", "audio_base64": audio}) + "\n")),
        ):
            self.assertEqual(provider.run(), provider.EXIT_RUNTIME_ERROR)
        stream.finish.assert_not_called()
        stream.cancel.assert_called_once()
        self.assertEqual([event["type"] for event in events], ["error", "closed"])

    def test_websocket_rejects_oversized_frame_and_fragmented_message(self):
        client = provider.WebSocketClient.__new__(provider.WebSocketClient)
        client._recv_buffer = b""
        client.socket = MagicMock()
        client.socket.recv.return_value = b""
        client._closed = False
        with patch.object(
            client,
            "_recv_exact",
            side_effect=[b"\x81\x7f", (provider.MAX_WEBSOCKET_MESSAGE_BYTES + 1).to_bytes(8, "big")],
        ):
            with self.assertRaisesRegex(RuntimeError, "frame exceeds"):
                client._recv_frame()
        with patch.object(
            client,
            "_recv_frame",
            side_effect=[
                (0x1, b"a" * provider.MAX_WEBSOCKET_MESSAGE_BYTES, False),
                (0x0, b"b", True),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "message exceeds"):
                client.recv_message()

    def test_alternative_words_and_ascii_boundaries(self):
        self.assertEqual(
            provider.extract_result_text({"ws": [{"cw": [{"w": "Hello"}, {"w": "wrong"}]}, {"cw": [{"w": "world"}]}]}),
            "Hello world",
        )
        self.assertEqual(provider.combine_text_fragments(["这个", "崩溃", "ABC", "test"]), "这个崩溃ABC test")


if __name__ == "__main__":
    unittest.main()
