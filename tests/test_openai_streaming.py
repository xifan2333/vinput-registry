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
spec.loader.exec_module(provider)


class FakeWebSocket:
    completion_delay = 0.15
    failure = False

    def __init__(self, *args):
        self.messages = queue.Queue()

    def send_json(self, event):
        if event["type"] != "input_audio_buffer.commit":
            return
        self.messages.put({"type": "input_audio_buffer.committed", "item_id": "test"})
        event = (
            {"type": "error", "error": {"message": "Test API failure"}}
            if self.failure
            else {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "test",
                "transcript": "The complete final sentence.",
            }
        )
        threading.Timer(self.completion_delay, self.messages.put, args=[event]).start()

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
            {"VINPUT_ASR_DELAY": "invalid"},
            {"VINPUT_ASR_ENABLE_VAD": "true"},
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                provider.build_session_update_event("gpt-live-transcribe", 24000, "transcription")

    def test_other_models_keep_model_query_and_single_language_hint(self):
        with patch.dict(os.environ, {"VINPUT_ASR_LANGUAGE": "ja"}, clear=True):
            self.assertEqual(provider.build_url("whisper-1"), "wss://api.openai.com/v1/realtime?model=whisper-1")
            event = provider.build_session_update_event("whisper-1", 24000, "transcription")
        self.assertEqual(event["session"]["audio"]["input"]["transcription"], {"model": "whisper-1", "language": "ja"})

    def test_handshake_preserves_first_websocket_event(self):
        client = provider.WebSocketClient.__new__(provider.WebSocketClient)
        client.host, client.port, client.path = "example.com", 80, "/"
        client.timeout, client.headers, client.scheme = 1, {}, "ws"
        sock = MagicMock()
        with (
            patch.object(provider.socket, "create_connection", return_value=sock),
            patch.object(
                client, "_read_http_response", return_value=b"HTTP/1.1 101 Switching Protocols\r\n\r\nfirst-event"
            ),
            patch.object(client, "_validate_handshake"),
        ):
            self.assertIs(client._connect(), sock)
        self.assertEqual(client._recv_buffer, b"first-event")

    def test_commit_acknowledgement_preserves_text_already_streamed(self):
        state = {"last_item_id": "", "partials": {"test": "Already transcribed"}}
        provider.handle_server_message({"type": "input_audio_buffer.committed", "item_id": "test"}, state)
        self.assertEqual(state["partials"]["test"], "Already transcribed")

    def run_adapter(self, failure=False, commit_audio=False):
        audio = {
            "type": "audio",
            "sample_rate": 24000,
            "audio_base64": base64.b64encode(b"\0\0" * 2400).decode(),
            "commit": commit_audio,
        }
        input_text = json.dumps(audio) + '\n{"type":"finish"}\n'
        output = io.StringIO()
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
            patch.object(provider, "WebSocketClient", FakeWebSocket),
            patch.object(FakeWebSocket, "failure", failure),
            patch.object(sys, "stdin", io.StringIO(input_text)),
            contextlib.redirect_stdout(output),
        ):
            status = provider.run()
        return status, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_waits_for_final_instead_of_closing_after_fixed_grace(self):
        status, events = self.run_adapter()
        self.assertEqual(status, 0)
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["The complete final sentence."])
        self.assertEqual(events[-1]["type"], "closed")

    def test_finish_does_not_commit_an_already_committed_empty_buffer(self):
        status, events = self.run_adapter(commit_audio=True)
        self.assertEqual(status, 0)
        self.assertEqual(sum(e["type"] == "final" for e in events), 1)

    def test_api_failure_is_reported_without_a_final_transcript(self):
        status, events = self.run_adapter(failure=True)
        self.assertEqual(status, provider.EXIT_RUNTIME_ERROR)
        self.assertTrue(any(e["type"] == "error" for e in events))
        self.assertFalse(any(e["type"] == "final" for e in events))


if __name__ == "__main__":
    unittest.main()
