"""Regression tests for OpenAI-compatible live transcription and finalization."""

import base64
import contextlib
import importlib.util
import io
import json
import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/providers/openai-compatible/streaming/entry.py"
spec = importlib.util.spec_from_file_location("vinput_streaming", ENTRY)
assert spec and spec.loader
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


class FakeWebSocket:
    completion_delay = 0.15

    def __init__(self, *, failure=False, omit_item_id=False):
        self.failure = failure
        self.omit_item_id = omit_item_id
        self.messages = queue.Queue()
        self.commits = 0
        self.audio = []

    def send_json(self, event):
        if event["type"] == "input_audio_buffer.append":
            self.audio.append(base64.b64decode(event["audio"]))
        if event["type"] != "input_audio_buffer.commit":
            return
        self.commits += 1
        item = {"item_id": str(self.commits)} if not self.omit_item_id else {}
        self.messages.put({"type": "input_audio_buffer.committed", **item})
        event = (
            {"type": "error", "error": {"message": "Test API failure"}}
            if self.failure
            else {
                "type": "conversation.item.input_audio_transcription.completed",
                **item,
                "transcript": "The complete final sentence." if self.commits == 1 else "The second sentence.",
            }
        )
        threading.Timer(self.completion_delay * self.commits, self.messages.put, args=[event]).start()

    def recv_json(self):
        return self.messages.get(timeout=2)

    def close(self):
        self.messages.put(None)


class StreamingTests(unittest.TestCase):
    def test_live_session_uses_medium_delay_and_multiple_languages(self):
        with patch.dict(os.environ, {"VINPUT_ASR_LANGUAGES": '["en", "zh", "ja"]'}, clear=True):
            self.assertEqual(
                provider.build_url("gpt-live-transcribe"),
                "wss://api.openai.com/v1/realtime?intent=transcription",
            )
            session = provider.build_session_update_event("gpt-live-transcribe", 24000, "transcription")["session"]
        audio = session["audio"]["input"]
        self.assertIsNone(audio["turn_detection"])
        self.assertEqual(audio["transcription"]["delay"], "medium")
        self.assertEqual(audio["transcription"]["languages"], ["en", "zh", "ja"])
        self.assertNotIn("language", audio["transcription"])

    def test_live_defaults_do_not_assume_input_languages(self):
        with patch.dict(os.environ, {}, clear=True):
            event = provider.build_session_update_event("gpt-live-transcribe", 24000, "transcription")
        self.assertNotIn("languages", event["session"]["audio"]["input"]["transcription"])

    def test_invalid_live_configuration_is_rejected(self):
        for env in (
            {"VINPUT_ASR_LANGUAGES": '"en"'},
            {"VINPUT_ASR_LANGUAGES": "["},
            {"VINPUT_ASR_LANGUAGES": '["  "]'},
            {"VINPUT_ASR_DELAY": "invalid"},
            {"VINPUT_ASR_ENABLE_VAD": "true"},
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(provider.ConfigError):
                provider.build_session_update_event("gpt-live-transcribe", 24000, "transcription")

    def test_other_models_keep_model_query_and_single_language_hint(self):
        with patch.dict(os.environ, {"VINPUT_ASR_LANGUAGE": "ja"}, clear=True):
            self.assertEqual(provider.build_url("whisper-1"), "wss://api.openai.com/v1/realtime?model=whisper-1")
            event = provider.build_session_update_event("whisper-1", 24000, "transcription")
        self.assertEqual(event["session"]["input_audio_transcription"], {"model": "whisper-1", "language": "ja"})

    def test_handshake_preserves_first_websocket_event(self):
        client = provider.WebSocketClient.__new__(provider.WebSocketClient)
        client.host, client.port, client.path = "example.com", 80, "/"
        client.timeout, client.headers, client.scheme = 1, {}, "ws"
        sock = MagicMock()
        key_bytes = bytes(16)
        key = base64.b64encode(key_bytes)
        accept = base64.b64encode(provider.hashlib.sha1(key + provider.GUID.encode("ascii")).digest())
        sock.recv.return_value = (
            b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\nfirst-event"
        )
        with (
            patch.object(provider.socket, "create_connection", return_value=sock),
            patch.object(provider.secrets, "token_bytes", return_value=key_bytes),
        ):
            self.assertIs(client._connect(), sock)
        self.assertEqual(client._recv_buffer, b"first-event")

    def test_commit_acknowledgement_preserves_text_already_streamed(self):
        state = {"last_item_id": "", "partials": {}}
        with patch.object(provider, "emit_partial") as emit_partial:
            provider.handle_server_message(
                {"type": "conversation.item.input_audio_transcription.delta", "item_id": "test", "delta": "Already"},
                state,
            )
            provider.handle_server_message({"type": "input_audio_buffer.committed", "item_id": "test"}, state)
            provider.handle_server_message(
                {"type": "conversation.item.input_audio_transcription.delta", "delta": " transcribed"}, state
            )
        self.assertEqual(state["partials"]["test"], "Already transcribed")
        emit_partial.assert_called_with("Already transcribed")

    def test_compatible_server_deltas_without_item_ids_accumulate(self):
        state = {"last_item_id": "", "partials": {}}
        with patch.object(provider, "emit_partial") as emit_partial:
            for delta in ("Hello", " world"):
                provider.handle_server_message({"type": "response.audio_transcript.delta", "delta": delta}, state)
        emit_partial.assert_called_with("Hello world")

    def run_adapter(self, failure=False, commit_audio=False, multiple_chunks=False, omit_item_id=False):
        audio = {
            "type": "audio",
            "sample_rate": 24000,
            "audio_base64": base64.b64encode(b"\0\0" * 2400).decode(),
            "commit": commit_audio,
        }
        events = [audio]
        if multiple_chunks:
            events.append({**audio, "commit": False})
        events.append({"type": "finish"})
        input_text = "\n".join(map(json.dumps, events)) + "\n"
        output = io.StringIO()
        notifications = io.StringIO()
        websocket = FakeWebSocket(failure=failure, omit_item_id=omit_item_id)
        with (
            patch.dict(
                os.environ,
                {
                    "VINPUT_ASR_API_KEY": "test",
                    "VINPUT_ASR_MODEL": "gpt-live-transcribe",
                    "VINPUT_ASR_FINISH_GRACE_SECS": "1",
                },
                clear=True,
            ),
            patch.object(provider, "WebSocketClient", return_value=websocket),
            patch.object(sys, "stdin", io.StringIO(input_text)),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(notifications),
        ):
            status = provider.run()
        return (
            status,
            [json.loads(line) for line in output.getvalue().splitlines()],
            notifications.getvalue(),
            websocket,
        )

    def test_waits_for_final_instead_of_closing_after_fixed_grace(self):
        status, events, _, websocket = self.run_adapter()
        self.assertEqual(status, 0)
        self.assertEqual(websocket.audio, [b"\0\0" * 2400])
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["The complete final sentence."])
        self.assertEqual(events[-1]["type"], "closed")

    def test_finish_does_not_commit_an_already_committed_empty_buffer(self):
        status, events, _, websocket = self.run_adapter(commit_audio=True)
        self.assertEqual(status, 0)
        self.assertEqual(websocket.commits, 1)
        self.assertEqual(sum(e["type"] == "final" for e in events), 1)

    def test_multiple_commits_produce_one_final_even_without_item_ids(self):
        status, events, _, websocket = self.run_adapter(commit_audio=True, multiple_chunks=True, omit_item_id=True)
        self.assertEqual(status, 0)
        self.assertEqual(websocket.commits, 2)
        self.assertEqual(
            [event["text"] for event in events if event["type"] == "final"],
            ["The complete final sentence. The second sentence."],
        )

    def test_api_failure_is_reported_without_a_final_transcript(self):
        status, events, notifications, _ = self.run_adapter(failure=True)
        self.assertEqual(status, provider.EXIT_RUNTIME_ERROR)
        self.assertIn("Test API failure", json.loads(notifications)["body"])
        self.assertFalse(any(e["type"] == "final" for e in events))


if __name__ == "__main__":
    unittest.main()
