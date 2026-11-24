"""Regression tests for the unified streaming ASR protocol (#118)."""

from __future__ import annotations

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
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

STREAMING_PROVIDERS = [
    "resources/providers/bailian/streaming/entry.py",
    "resources/providers/bailian/qwen-audio3-streaming/entry.py",
    "resources/providers/doubao/streaming/entry.py",
    "resources/providers/doubaoime/streaming/entry.py",
    "resources/providers/elevenlabs/streaming/entry.py",
    "resources/providers/iflyime/streaming/entry.py",
    "resources/providers/openai-compatible/streaming/entry.py",
    "resources/providers/soniox/streaming/entry.py",
    "resources/providers/vllm/streaming/entry.py",
    "resources/providers/wetype/streaming/entry.py",
    "resources/providers/xfyun/streaming/entry.py",
    "resources/providers/xfyun/iat/streaming/entry.py",
]


def load_provider(rel_path: str, module_name: str) -> Any:
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def audio_line(pcm: bytes = b"\x01\x00" * 160, *, legacy_commit: bool = False) -> str:
    payload: dict[str, Any] = {
        "type": "audio",
        "audio_base64": base64.b64encode(pcm).decode("ascii"),
    }
    if legacy_commit:
        payload["commit"] = True
    return json.dumps(payload) + "\n"


class UnifiedEmitFinalTests(unittest.TestCase):
    def test_all_streaming_providers_emit_minimal_final_payload(self) -> None:
        for idx, rel_path in enumerate(STREAMING_PROVIDERS):
            with self.subTest(provider=rel_path):
                mod = load_provider(rel_path, f"vinput_test_stream_{idx}")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    mod.emit_final("hello world")
                event = json.loads(out.getvalue().strip())
                self.assertEqual(event, {"type": "final", "text": "hello world"})


class BailianStreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_provider("resources/providers/bailian/streaming/entry.py", "test_bailian_streaming_mod")

    def run_session(
        self, stdin_text: str, messages: list[dict[str, Any] | None]
    ) -> tuple[int, list[dict[str, Any]], str, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        for item in messages:
            msg_queue.put(item)

        class FakeWS:
            def send_json(self, payload: dict[str, Any]) -> None:
                sent.append(payload)
                if payload.get("type") == "session.finish":
                    msg_queue.put({"type": "session.finished"})

            def recv_json(self) -> dict[str, Any] | None:
                return msg_queue.get(timeout=2)

            def close(self) -> None:
                msg_queue.put(None)

        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(
                os.environ, {"VINPUT_ASR_API_KEY": "test-key", "VINPUT_ASR_FINISH_GRACE_SECS": "0.5"}, clear=True
            ),
            patch.object(self.mod, "WebSocketClient", return_value=FakeWS()),
            patch.object(sys, "stdin", io.StringIO(stdin_text)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.mod.run()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, events, err.getvalue(), sent

    def test_mid_utterance_completions_emit_partial_and_single_final_on_finish(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"type": "session.created", "session": {"id": "s1"}},
            {"type": "conversation.item.input_audio_transcription.text", "item_id": "1", "text": "First", "stash": ""},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "1",
                "transcript": "First sentence.",
            },
            {"type": "conversation.item.input_audio_transcription.text", "item_id": "2", "text": "Second", "stash": ""},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "2",
                "transcript": "Second sentence.",
            },
        ]
        stdin_text = audio_line(legacy_commit=True) + audio_line() + '{"type":"finish"}\n'
        code, events, err, sent = self.run_session(stdin_text, messages)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(any(p.get("type") == "input_audio_buffer.commit" for p in sent))
        self.assertEqual(
            [e for e in events if e["type"] == "final"],
            [{"type": "final", "text": "First sentence. Second sentence."}],
        )
        self.assertEqual(events[-1], {"type": "closed"})
        self.assertTrue(any(e == {"type": "partial", "text": "First sentence."} for e in events))

    def test_cancel_and_eof_never_emit_final(self) -> None:
        for stdin_text in (audio_line() + '{"type":"cancel"}\n', audio_line()):
            messages: list[dict[str, Any] | None] = [
                {"type": "session.created", "session": {"id": "s1"}},
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "1",
                    "transcript": "Partial only.",
                },
            ]
            code, events, _, _ = self.run_session(stdin_text, messages)
            self.assertEqual(code, 0)
            self.assertFalse(any(e["type"] == "final" for e in events))
            self.assertEqual(events[-1], {"type": "closed"})

    def test_upstream_error_reports_stderr_without_final(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"type": "session.created", "session": {"id": "s1"}},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "1", "transcript": "First."},
            {"type": "error", "error": {"message": "Upstream failure"}},
        ]
        code, events, err, _ = self.run_session(audio_line() + '{"type":"finish"}\n', messages)
        self.assertEqual(code, self.mod.EXIT_RUNTIME_ERROR)
        self.assertIn("Upstream failure", json.loads(err.strip())["body"])
        self.assertFalse(any(e["type"] == "final" for e in events))


class QwenAudio3StreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_provider(
            "resources/providers/bailian/qwen-audio3-streaming/entry.py",
            "test_qwen_audio3_streaming_mod",
        )

    def run_session(
        self, stdin_text: str, messages: list[dict[str, Any] | None]
    ) -> tuple[int, list[dict[str, Any]], str]:
        msg_queue: queue.Queue[bytes | None] = queue.Queue()
        for item in messages:
            msg_queue.put(None if item is None else json.dumps(item).encode("utf-8"))

        class FakeWS:
            def send_text(self, text: str) -> None:
                payload = json.loads(text)
                if payload.get("header", {}).get("action") == "finish-task":
                    msg_queue.put(json.dumps({"header": {"event": "task-finished"}}).encode("utf-8"))

            def send_binary(self, payload: bytes) -> None:
                del payload

            def recv_message(self) -> bytes | None:
                return msg_queue.get(timeout=2)

            def close(self) -> None:
                msg_queue.put(None)

        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(
                os.environ, {"VINPUT_ASR_API_KEY": "test-key", "VINPUT_ASR_FINISH_GRACE_SECS": "0.5"}, clear=True
            ),
            patch.object(self.mod, "WebSocketClient", return_value=FakeWS()),
            patch.object(sys, "stdin", io.StringIO(stdin_text)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.mod.run()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, events, err.getvalue()

    def test_sentence_end_emits_partial_and_single_final_on_finish(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"header": {"event": "task-started"}},
            {
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {"text": "第一句。", "end_time": 1000}}},
            },
            {
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {"text": "第二句。", "end_time": 2000}}},
            },
        ]
        code, events, err = self.run_session(audio_line(legacy_commit=True) + '{"type":"finish"}\n', messages)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            [e for e in events if e["type"] == "partial"],
            [
                {"type": "partial", "text": "第一句。"},
                {"type": "partial", "text": "第一句。第二句。"},
            ],
        )
        self.assertEqual(
            [e for e in events if e["type"] == "final"],
            [{"type": "final", "text": "第一句。第二句。"}],
        )
        self.assertEqual(events[-1], {"type": "closed"})

    def test_cancel_and_task_failed_never_emit_final(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"header": {"event": "task-started"}},
            {
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {"text": "第一句。", "end_time": 1000}}},
            },
        ]
        code, events, _ = self.run_session(audio_line() + '{"type":"cancel"}\n', messages)
        self.assertEqual(code, 0)
        self.assertFalse(any(e["type"] == "final" for e in events))

        fail_messages: list[dict[str, Any] | None] = [
            {"header": {"event": "task-started"}},
            {
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {"text": "第一句。", "end_time": 1000}}},
            },
            {"header": {"event": "task-failed", "error_code": "500", "error_message": "Task failed"}},
        ]
        code, events, err = self.run_session(audio_line() + '{"type":"finish"}\n', fail_messages)
        self.assertEqual(code, self.mod.EXIT_RUNTIME_ERROR)
        self.assertIn("Task failed", json.loads(err.strip())["body"])
        self.assertFalse(any(e["type"] == "final" for e in events))


class ElevenLabsStreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_provider("resources/providers/elevenlabs/streaming/entry.py", "test_elevenlabs_streaming_mod")

    def run_session(
        self, stdin_text: str, messages: list[dict[str, Any] | None]
    ) -> tuple[int, list[dict[str, Any]], str, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        for item in messages:
            msg_queue.put(item)

        class FakeWS:
            def send_json(self, payload: dict[str, Any]) -> None:
                sent.append(payload)
                if payload.get("commit") is True:
                    msg_queue.put({"message_type": "committed_transcript", "text": "Second part."})

            def recv_json(self) -> dict[str, Any] | None:
                return msg_queue.get(timeout=2)

            def close(self) -> None:
                msg_queue.put(None)

        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(
                os.environ, {"VINPUT_ASR_API_KEY": "test-key", "VINPUT_ASR_FINISH_GRACE_SECS": "0.5"}, clear=True
            ),
            patch.object(self.mod, "WebSocketClient", return_value=FakeWS()),
            patch.object(sys, "stdin", io.StringIO(stdin_text)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.mod.run()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, events, err.getvalue(), sent

    def test_committed_transcripts_emit_partial_and_single_final_on_finish(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"message_type": "session_started", "session_id": "el-1"},
            {"message_type": "partial_transcript", "text": "First"},
            {
                "message_type": "committed_transcript_with_timestamps",
                "text": "First part.",
                "words": [{"text": "First"}],
            },
        ]
        code, events, err, sent = self.run_session(audio_line(legacy_commit=True) + '{"type":"finish"}\n', messages)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual([p["commit"] for p in sent], [False, True])
        self.assertFalse(any(e["type"] == "final_timestamps" for e in events))
        self.assertEqual(
            [e for e in events if e["type"] == "partial"],
            [
                {"type": "partial", "text": "First"},
                {"type": "partial", "text": "First part."},
                {"type": "partial", "text": "First part. Second part."},
            ],
        )
        self.assertEqual(
            [e for e in events if e["type"] == "final"],
            [{"type": "final", "text": "First part. Second part."}],
        )
        self.assertEqual(events[-1], {"type": "closed"})

    def test_cancel_and_error_never_emit_final(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"message_type": "session_started", "session_id": "el-1"},
            {"message_type": "committed_transcript", "text": "First part."},
        ]
        code, events, _, _ = self.run_session(audio_line() + '{"type":"cancel"}\n', messages)
        self.assertEqual(code, 0)
        self.assertFalse(any(e["type"] == "final" for e in events))

        err_messages: list[dict[str, Any] | None] = [
            {"message_type": "session_started", "session_id": "el-1"},
            {"message_type": "committed_transcript", "text": "First part."},
            {"message_type": "scribe_error", "error": "Quota exceeded"},
        ]
        code, events, err, _ = self.run_session(audio_line() + '{"type":"finish"}\n', err_messages)
        self.assertEqual(code, self.mod.EXIT_RUNTIME_ERROR)
        self.assertIn("Quota exceeded", json.loads(err.strip())["body"])
        self.assertFalse(any(e["type"] == "final" for e in events))


class SonioxStreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_provider("resources/providers/soniox/streaming/entry.py", "test_soniox_streaming_mod")

    def run_session(
        self, stdin_text: str, messages: list[dict[str, Any] | None]
    ) -> tuple[int, list[dict[str, Any]], str, list[dict[str, Any]], list[bool]]:
        sent_json: list[dict[str, Any]] = []
        end_of_stream: list[bool] = []
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        for item in messages:
            msg_queue.put(item)

        class FakeWS:
            def disable_read_timeout(self) -> None:
                pass

            def send_json(self, payload: dict[str, Any]) -> None:
                sent_json.append(payload)

            def send_binary(self, payload: bytes) -> None:
                del payload

            def send_end_of_stream(self) -> None:
                end_of_stream.append(True)
                msg_queue.put(
                    {
                        "tokens": [{"text": "world", "is_final": True}, {"text": "<end>", "is_final": True}],
                        "finished": True,
                    }
                )

            def recv_json(self) -> dict[str, Any] | None:
                return msg_queue.get(timeout=2)

            def close(self) -> None:
                msg_queue.put(None)

        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(
                os.environ, {"VINPUT_ASR_API_KEY": "test-key", "VINPUT_ASR_FINISH_GRACE_SECS": "0.5"}, clear=True
            ),
            patch.object(self.mod, "WebSocketClient", return_value=FakeWS()),
            patch.object(sys, "stdin", io.StringIO(stdin_text)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.mod.run()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, events, err.getvalue(), sent_json, end_of_stream

    def test_final_tokens_emit_partial_and_single_final_on_finish(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {
                "tokens": [
                    {"text": "hello", "is_final": True},
                    {"text": "<fin>", "is_final": True},
                    {"text": "wor", "is_final": False},
                ]
            },
        ]
        code, events, err, sent_json, end_of_stream = self.run_session(
            audio_line(legacy_commit=True) + '{"type":"finish"}\n',
            messages,
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(end_of_stream, [True])
        self.assertFalse(any(p.get("type") == "finalize" for p in sent_json))
        self.assertEqual(
            [e for e in events if e["type"] == "partial"],
            [
                {"type": "partial", "text": "hello wor"},
                {"type": "partial", "text": "hello world"},
            ],
        )
        self.assertEqual(
            [e for e in events if e["type"] == "final"],
            [{"type": "final", "text": "hello world"}],
        )
        self.assertEqual(events[-1], {"type": "closed"})

    def test_cancel_and_error_never_emit_final(self) -> None:
        messages: list[dict[str, Any] | None] = [
            {"tokens": [{"text": "hello", "is_final": True}]},
        ]
        code, events, _, _, _ = self.run_session(audio_line() + '{"type":"cancel"}\n', messages)
        self.assertEqual(code, 0)
        self.assertFalse(any(e["type"] == "final" for e in events))

        err_messages: list[dict[str, Any] | None] = [
            {"tokens": [{"text": "hello", "is_final": True}]},
            {"error_code": 401, "error_message": "Invalid Soniox key"},
        ]
        code, events, err, _, _ = self.run_session(audio_line() + '{"type":"finish"}\n', err_messages)
        self.assertEqual(code, self.mod.EXIT_RUNTIME_ERROR)
        self.assertIn("Invalid Soniox key", json.loads(err.strip())["body"])
        self.assertFalse(any(e["type"] == "final" for e in events))


class VllmStreamingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_provider("resources/providers/vllm/streaming/entry.py", "test_vllm_streaming_mod")

    def test_legacy_commit_ignored_and_single_final_on_finish(self) -> None:
        sent: list[dict[str, Any]] = []
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        msg_queue.put({"type": "session.created"})

        class FakeWS:
            def send_json(self, payload: dict[str, Any]) -> None:
                sent.append(payload)
                if payload.get("type") == "input_audio_buffer.commit" and payload.get("final") is True:
                    msg_queue.put(
                        {
                            "type": "transcription.delta",
                            "delta": "<|startoftranscript|><|zh|><|transcribe|><|notimestamps|>你好",
                        }
                    )
                    msg_queue.put(
                        {
                            "type": "transcription.done",
                            "text": "<|startoftranscript|><|zh|><|transcribe|><|notimestamps|>你好世界",
                        }
                    )
                    threading.Timer(0.05, msg_queue.put, args=[None]).start()

            def recv_json(self) -> dict[str, Any] | None:
                return msg_queue.get(timeout=2)

            def close(self) -> None:
                msg_queue.put(None)

        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"VINPUT_ASR_FINISH_GRACE_SECS": "0.5"}, clear=True),
            patch.object(self.mod, "WebSocketClient", return_value=FakeWS()),
            patch.object(sys, "stdin", io.StringIO(audio_line(legacy_commit=True) + '{"type":"finish"}\n')),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.mod.run()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        final_commits = [p for p in sent if p.get("type") == "input_audio_buffer.commit" and p.get("final") is True]
        self.assertEqual(len(final_commits), 1)
        self.assertEqual([e for e in events if e["type"] == "final"], [{"type": "final", "text": "你好世界"}])
        self.assertEqual(events[-1], {"type": "closed"})


if __name__ == "__main__":
    unittest.main()
