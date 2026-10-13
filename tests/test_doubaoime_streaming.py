"""Offline regression coverage for Doubao IME streaming ASR."""

import asyncio
import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/providers/doubaoime/streaming/entry.py"
spec = importlib.util.spec_from_file_location("doubaoime_streaming_entry", ENTRY)
assert spec and spec.loader
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)


class FakeEncoder:
    def __init__(self, bitrate: int):
        self.bitrate = bitrate

    def encode(self, pcm: bytes) -> bytes:
        return b"\xfc" * 20

    def close(self):
        pass


class TestCryptoAndEncoding(unittest.TestCase):
    def test_tt_encrypt_header_and_structure(self):
        payload = b"hello doubao"
        salt = bytes(range(32))
        encrypted = provider.encode_tt_encrypt(payload, salt=salt)
        # ttEncrypt magic prefix: 746305100000 (6 bytes)
        self.assertTrue(encrypted.startswith(bytes.fromhex("746305100000")))
        # Salt follows magic (32 bytes)
        self.assertEqual(encrypted[6:38], salt)
        # Followed by AES-CBC encrypted blocks (aligned to 16 bytes)
        self.assertEqual((len(encrypted) - 38) % 16, 0)
        self.assertGreater(len(encrypted), 38)

    def test_protobuf_varint_and_fields(self):
        for value in (0, 1, 127, 128, 300, 16384, 1000000):
            encoded = provider.encode_varint(value)
            decoded, index = provider.decode_varint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(index, len(encoded))

        # Test field encoding
        data = (
            provider.encode_field_string(1, "doubao")
            + provider.encode_field_int(2, 42)
            + provider.encode_field_bytes(3, b"\x01\x02\x03")
        )
        fields = provider.parse_protobuf_fields(data)
        self.assertEqual(provider.get_proto_string(fields, 1), "doubao")
        self.assertEqual(provider.get_proto_int(fields, 2), 42)
        self.assertEqual(fields[3], b"\x01\x02\x03")

    def test_websocket_request_and_response_roundtrip(self):
        req = provider.WebSocketRequest(
            namespace="ASR",
            event="StartTask",
            task_id="task-123",
            appkey="test-key",
            payload='{"did":"123"}',
            audio=b"\x00\x01",
            seq_id=1,
        )
        serialized = req.SerializeToString()
        fields = provider.parse_protobuf_fields(serialized)
        self.assertEqual(provider.get_proto_string(fields, 2), "test-key")
        self.assertEqual(provider.get_proto_string(fields, 3), "ASR")
        self.assertEqual(provider.get_proto_string(fields, 5), "StartTask")
        self.assertEqual(provider.get_proto_string(fields, 6), '{"did":"123"}')
        self.assertEqual(fields[7], b"\x00\x01")
        self.assertEqual(provider.get_proto_string(fields, 8), "task-123")
        self.assertEqual(provider.get_proto_int(fields, 9), 1)

        # Test response decoding
        res_data = (
            provider.encode_field_string(1, "task-123")
            + provider.encode_field_string(4, "TaskStarted")
            + provider.encode_field_int(5, 200)
            + provider.encode_field_string(6, "OK")
            + provider.encode_field_string(7, '{"results":[{"text":"你好"}]}')
            + provider.encode_field_int(9, 1)
        )
        res = provider.decode_response(res_data)
        self.assertEqual(res.task_id, "task-123")
        self.assertEqual(res.event, "TaskStarted")
        self.assertEqual(res.status_code, 200)

        update = provider.parse_update(res)
        self.assertIsNotNone(update)
        assert update is not None
        self.assertEqual(update.text, "你好")

    def test_decode_response_raises_on_failure_events(self):
        failed_data = (
            provider.encode_field_string(1, "task-123")
            + provider.encode_field_string(4, "TaskFailed")
            + provider.encode_field_int(5, 400)
            + provider.encode_field_string(6, "Invalid token")
        )
        with self.assertRaises(provider.ASRError) as ctx:
            provider.decode_response(failed_data)
        self.assertIn("TaskFailed: 400 Invalid token", str(ctx.exception))


class TestPunctuationAndText(unittest.TestCase):
    def test_remove_trailing_punctuation(self):
        self.assertEqual(provider.remove_trailing_punctuation("你好，世界！"), "你好，世界")
        self.assertEqual(provider.remove_trailing_punctuation("今天天气很好。"), "今天天气很好")
        # Preserved closing punctuation
        self.assertEqual(provider.remove_trailing_punctuation("我说：【好的】"), "我说：【好的】")
        self.assertEqual(provider.remove_trailing_punctuation("进度 100%"), "进度 100%")
        self.assertEqual(provider.remove_trailing_punctuation("温度 25℃"), "温度 25℃")
        # Empty string
        self.assertEqual(provider.remove_trailing_punctuation(""), "")

        # Per line trimming
        multiline = "第一行。\n第二行！\n第三行"
        self.assertEqual(
            provider.remove_trailing_punctuation(multiline, per_line=True),
            "第一行\n第二行\n第三行",
        )

    def test_transcript_state_merging_and_vad(self):
        state = provider.TranscriptState()

        # Interim hypotheses update current text without committing
        self.assertEqual(state.apply("早上", finished=False), "早上")
        self.assertEqual(state.apply("早上九点", finished=False), "早上九点")
        self.assertEqual(state.committed, "")

        # VAD segment finished commits the segment
        self.assertEqual(state.apply("早上9点。", finished=True), "早上9点。")
        self.assertEqual(state.committed, "早上9点。")

        # Duplicate segment final is ignored
        self.assertIsNone(state.apply("早上9点。", finished=True))

        # Next segment builds upon committed text
        self.assertEqual(state.apply("去开会", finished=False), "早上9点。去开会")
        self.assertEqual(state.apply("去开会。", finished=True), "早上9点。去开会。")
        self.assertEqual(state.committed, "早上9点。去开会。")


class TestIdentityAndMigration(unittest.TestCase):
    def test_existing_identity_loaded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "credentials.json"
            identity_data = {
                "device_id": "dev-123",
                "install_id": "inst-456",
                "cdid": "cdid-789",
                "openudid": "open-abc",
                "clientudid": "client-def",
            }
            path.write_text(json.dumps(identity_data), encoding="utf-8")
            loaded = provider._ensure_identity(path)
            self.assertEqual(loaded["device_id"], "dev-123")
            self.assertEqual(loaded["install_id"], "inst-456")

    def test_clean_registration_ignores_legacy_cache_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "doubaoime" / "credentials.json"
            fake_identifiers = {"device_id": "new-dev", "install_id": "new-inst"}
            with patch.object(provider, "register_device", return_value=(fake_identifiers, {})) as reg_mock:
                loaded = provider._ensure_identity(target_path)
                reg_mock.assert_called_once()
                self.assertEqual(loaded["device_id"], "new-dev")
                self.assertEqual(loaded["install_id"], "new-inst")
                self.assertTrue(target_path.exists())

    def test_fresh_registration_saves_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "new_credentials.json"
            fake_identifiers = {"device_id": "registered-dev", "install_id": "registered-inst"}
            with patch.object(provider, "register_device", return_value=(fake_identifiers, {})) as reg_mock:
                state = provider._ensure_identity(path)
                reg_mock.assert_called_once()
                self.assertEqual(state["device_id"], "registered-dev")
                self.assertEqual(state["install_id"], "registered-inst")
                self.assertTrue(path.exists())
                # Permissions restricted to owner (0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TestStreamingLifecycle(unittest.TestCase):
    def test_run_provider_success_flow(self):
        events = []

        async def fake_transcribe_stream(source, on_update=None, *, on_start=None):
            if on_start:
                on_start()
            # Consume all audio chunks
            async for _ in source:
                pass
            update = provider.ASRUpdate(
                text="识别成功。",
                vad_finished=True,
                stream_finished=True,
                sequence=1,
                payload={},
            )
            if on_update:
                on_update(update)
            return provider.ASRResult(text="识别成功。", task_id="t-1", updates=(update,))

        fake_client = MagicMock()
        fake_client.transcribe_stream = fake_transcribe_stream

        # 320 bytes = 10 ms mono 16 kHz PCM
        pcm_chunk = bytes(320)
        audio_msg = json.dumps({"type": "audio", "audio_base64": base64.b64encode(pcm_chunk).decode()}) + "\n"
        finish_msg = json.dumps({"type": "finish"}) + "\n"

        async def lines_gen():
            yield audio_msg
            yield finish_msg

        with (
            patch.object(provider, "ASRClient", return_value=fake_client),
            patch.object(
                provider,
                "ensure_identity",
                return_value={"device_id": "dev-1", "install_id": "inst-1"},
            ),
        ):
            code = asyncio.run(
                provider.run_provider(
                    lines_gen(),
                    events.append,
                    finish_grace=2.0,
                    no_trailing_punctuation=True,
                )
            )

        self.assertEqual(code, 0)
        event_types = [e["type"] for e in events]
        self.assertIn("session_started", event_types)
        self.assertIn("partial", event_types)
        self.assertIn("final", event_types)
        self.assertIn("closed", event_types)

        # Verified trailing punctuation removal
        final_event = next(e for e in events if e["type"] == "final")
        self.assertEqual(final_event["text"], "识别成功")

    def test_run_provider_cancel_flow(self):
        events = []

        async def fake_transcribe_stream(source, on_update=None, *, on_start=None):
            if on_start:
                on_start()
            async for _ in source:
                pass
            return provider.ASRResult(text="", task_id="t-1", updates=())

        fake_client = MagicMock()
        fake_client.transcribe_stream = fake_transcribe_stream

        async def lines_gen():
            yield json.dumps({"type": "audio", "audio_base64": base64.b64encode(bytes(320)).decode()}) + "\n"
            yield json.dumps({"type": "cancel"}) + "\n"

        with (
            patch.object(provider, "ASRClient", return_value=fake_client),
            patch.object(
                provider,
                "ensure_identity",
                return_value={"device_id": "dev-1", "install_id": "inst-1"},
            ),
        ):
            code = asyncio.run(provider.run_provider(lines_gen(), events.append, finish_grace=2.0))

        self.assertEqual(code, 0)
        # Cancelled sessions must not emit 'final'
        self.assertFalse(any(e["type"] == "final" for e in events))
        self.assertEqual(events[-1]["type"], "closed")

    def test_audio_received_after_finish_emits_error(self):
        events = []

        async def lines_gen():
            yield json.dumps({"type": "finish"}) + "\n"
            yield json.dumps({"type": "audio", "audio_base64": base64.b64encode(bytes(320)).decode()}) + "\n"

        with patch.object(
            provider,
            "ensure_identity",
            return_value={"device_id": "dev-1", "install_id": "inst-1"},
        ):
            code = asyncio.run(provider.run_provider(lines_gen(), events.append, finish_grace=1.0))

        self.assertEqual(code, 1)
        self.assertTrue(any(e["type"] == "error" and "Audio received after finish" in e["message"] for e in events))
        self.assertEqual(events[-1]["type"], "closed")


if __name__ == "__main__":
    unittest.main()
