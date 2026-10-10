"""Offline regression coverage for Doubao IME client 1.4.6 compatibility."""

import base64
import importlib.util
import json
import os
import queue
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/providers/doubaoime/streaming/entry.py"
spec = importlib.util.spec_from_file_location("doubaoime_streaming_entry", ENTRY)
assert spec and spec.loader
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


def response(event, results=None):
    data = provider.encode_field_string(4, event)
    if results is not None:
        data += provider.encode_field_string(7, json.dumps({"results": results}, ensure_ascii=False))
    return data


class FakeClient:
    def __init__(self, *_args):
        self.responses = queue.Queue()
        self.responses.put(response("TaskStarted"))
        self.responses.put(response("SessionStarted"))
        self.sent = []

    def send_binary(self, data):
        fields = provider.parse_protobuf_fields(data)
        self.sent.append(fields)
        if provider.get_proto_string(fields, 5) == "FinishSession":
            self.responses.put(response("TaskResponse", [{"text": "测试成功。"}]))
            self.responses.put(response("SessionFinished"))

    def recv_binary(self):
        return self.responses.get(timeout=2)

    def close(self):
        self.responses.put(None)


class DoubaoStreamingTests(unittest.TestCase):
    def test_legacy_cache_migrates_key_without_reregistering_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(
                json.dumps({"device_id": "old-device", "token": "old-settings-token", "route_healthy": True})
            )
            with (
                patch.dict(os.environ, {"VINPUT_ASR_CREDENTIAL_PATH": str(path)}, clear=True),
                patch.object(provider, "register_device") as register,
            ):
                creds = provider.ensure_credentials(1)
            register.assert_not_called()
            self.assertEqual(creds.device_id, "old-device")
            self.assertEqual(creds.token, provider.DEFAULT_APP_KEY)
            self.assertEqual(json.loads(path.read_text())["token"], provider.DEFAULT_APP_KEY)

    def test_explicit_device_and_token_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "VINPUT_ASR_CREDENTIAL_PATH": str(Path(directory) / "credentials.json"),
                "VINPUT_ASR_DEVICE_ID": "custom-device",
                "VINPUT_ASR_TOKEN": "custom-token",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(provider, "register_device") as register:
                creds = provider.ensure_credentials(1)
            register.assert_not_called()
            self.assertEqual((creds.device_id, creds.token), ("custom-device", "custom-token"))

    def test_fresh_registration_uses_current_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"VINPUT_ASR_CREDENTIAL_PATH": str(Path(directory) / "credentials.json")}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    provider, "register_device", return_value=provider.DeviceCredentials(device_id="new-device")
                ) as register,
            ):
                creds = provider.ensure_credentials(3)
            register.assert_called_once_with(3)
            self.assertEqual(creds.token, provider.DEFAULT_APP_KEY)

    def test_session_defaults_and_existing_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            config = json.loads(provider.build_session_config("device"))
        self.assertEqual(config["extra"]["input_mode"], "stream")
        self.assertEqual(config["extra"]["app_name"], "oime")
        self.assertFalse(config["extra"]["enable_asr_threepass"])
        with patch.dict(os.environ, {"VINPUT_ASR_APP_NAME": "custom", "VINPUT_ASR_ENABLE_ASR_THREEPASS": "true"}):
            config = json.loads(provider.build_session_config("device"))
        self.assertEqual(config["extra"]["app_name"], "custom")
        self.assertTrue(config["extra"]["enable_asr_threepass"])

    def test_cumulative_results_replace_corrections_and_ignore_auxiliary_candidates(self):
        state = provider.SessionState()
        events = []
        with patch.object(provider, "write_stdout", side_effect=events.append):
            provider.handle_server_message(
                response("TaskResponse", [{"text": "早上酒点"}, {"text": "辅助候选"}]), state, "r"
            )
            provider.handle_server_message(
                response("TaskResponse", [{"text": "早上9点。", "is_interim": False, "is_vad_finished": True}]),
                state,
                "r",
            )
            self.assertEqual([e["type"] for e in events], ["partial", "partial"])
            provider.handle_server_message(response("SessionFinished"), state, "r")
            provider.emit_fallback_final(state)
        self.assertEqual(events[0]["text"], "早上酒点")
        self.assertEqual(events[-1]["text"], "早上9点。")
        self.assertTrue(events[-1]["utterance_final"])

    def test_empty_primary_does_not_promote_auxiliary(self):
        state = provider.SessionState()
        with patch.object(provider, "write_stdout") as emit:
            provider.handle_server_message(response("TaskResponse", [{"text": ""}, {"text": "辅助"}]), state, "r")
        emit.assert_not_called()
        self.assertEqual(state.get_visible_text(), "")

    def test_legacy_segment_final_is_partial_until_finish(self):
        state = provider.SessionState()
        events = []
        with patch.object(provider, "write_stdout", side_effect=events.append):
            for index, text in enumerate(["第一句。", "第二句。"]):
                provider.handle_server_message(
                    response("TaskResponse", [{"index": index, "text": text, "extra": {"nonstream_result": True}}]),
                    state,
                    "r",
                )
            self.assertTrue(all(e["type"] == "partial" for e in events))
            provider.emit_fallback_final(state)
        self.assertEqual(events[-1]["text"], "第一句。第二句。")
        self.assertEqual(sum(e["type"] == "final" for e in events), 1)

    def run_session(self, end):
        # Include a partial PCM frame to exercise padding and terminal framing.
        audio = base64.b64encode(bytes(642)).decode()
        lines = json.dumps({"type": "audio", "audio_base64": audio}) + "\n" + json.dumps({"type": end}) + "\n"
        client = FakeClient()
        events = []
        with (
            patch.object(provider, "WebSocketClient", return_value=client),
            patch.object(
                provider, "ensure_credentials", return_value=provider.DeviceCredentials(device_id="test", token="test")
            ),
            patch.object(provider, "OpusEncoder") as encoder,
            patch.object(provider.sys, "stdin", StringIO(lines)),
            patch.object(provider, "write_stdout", side_effect=events.append),
        ):
            encoder.return_value.encode.return_value = b"opus"
            self.assertEqual(provider.run(), 0)
        return client.sent, events

    def test_finish_sends_empty_terminal_audio_and_exactly_one_final(self):
        sent, events = self.run_session("finish")
        audio = [f for f in sent if provider.get_proto_string(f, 5) == "TaskRequest"]
        self.assertEqual([provider.get_proto_int(f, 9) for f in audio], [1, 3, 9])
        self.assertEqual(audio[-1].get(7, b""), b"")
        self.assertEqual(sum(e["type"] == "final" for e in events), 1)
        self.assertEqual(next(e["text"] for e in events if e["type"] == "final"), "测试成功。")
        self.assertEqual(events[-1]["type"], "closed")

    def test_cancel_closes_without_finish_or_final(self):
        sent, events = self.run_session("cancel")
        self.assertFalse(any(provider.get_proto_string(f, 5) == "FinishSession" for f in sent))
        self.assertFalse(any(e["type"] == "final" for e in events))
        self.assertEqual(events[-1]["type"], "closed")


if __name__ == "__main__":
    unittest.main()
