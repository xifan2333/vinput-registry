#!/usr/bin/env python3
"""OpenAI-compatible batch speech-to-text provider."""

from __future__ import annotations

import io
import json
import os
import sys
import uuid
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-1"
DEFAULT_RESPONSE_FORMAT = "json"
DEFAULT_TIMEOUT = 60

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "OpenAI-Compatible Speech Recognition",
        "err_no_audio": "No audio received on stdin.",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_http": "OpenAI ASR HTTP {code}: {message}",
        "err_network": "Failed to reach OpenAI ASR: {detail}",
        "err_empty_transcript": "OpenAI ASR returned an empty transcript.",
        "err_transcribe_failed": "Transcription failed: {detail}",
    },
    "zh_CN": {
        "title": "OpenAI 兼容语音识别",
        "err_no_audio": "标准输入未接收到音频数据。",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_http": "OpenAI 兼容语音识别 HTTP {code}：{message}",
        "err_network": "连接语音识别服务失败：{detail}",
        "err_empty_transcript": "语音识别返回了空白识别结果。",
        "err_transcribe_failed": "语音识别失败：{detail}",
    },
}


def detect_locale() -> str:
    """Detect system locale from LC_ALL, LC_MESSAGES, or LANG."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.getenv(var, "").strip()
        if not val:
            continue
        base = val.split(".")[0].split("@")[0]
        if base in MESSAGES:
            return base
        prefix = base.split("_")[0]
        for key in MESSAGES:
            if key.startswith(prefix + "_"):
                return key
    return "en_US"


def get_message(key: str, **kwargs: object) -> str:
    """Retrieve localized message string with fallback chain."""
    loc = detect_locale()
    tpl = MESSAGES.get(loc, {}).get(key)
    if not tpl:
        tpl = MESSAGES.get("en_US", {}).get(key, key)
    try:
        return tpl.format(**kwargs)
    except Exception:
        return tpl


def notify_error(key: str, **kwargs: object) -> None:
    """Emit JSONL user desktop notification to stderr."""
    title = get_message("title")
    msg = get_message(key, **kwargs)
    payload = json.dumps({"title": title, "body": msg}, ensure_ascii=False)
    sys.stderr.write(f"{payload}\n")
    sys.stderr.flush()


class VInputError(Exception):
    """Base exception carrying exit code and message key."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Raised when a required environment variable is missing or invalid."""

    exit_code = EXIT_USAGE_ERROR


@dataclass(frozen=True)
class ProviderConfig:
    """Parsed provider configuration from environment variables."""

    api_key: str
    endpoint: str
    model: str
    response_format: str
    timeout: int
    language: str | None
    prompt: str | None
    temperature: str | None


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_api_key")
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


def optional_int_env(name: str, default: int) -> int:
    """Read an optional integer environment variable with fallback."""
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def resolve_endpoint() -> str:
    """Determine endpoint URL based on environment options."""
    explicit_url = optional_env("VINPUT_ASR_URL")
    if explicit_url:
        return explicit_url

    base_url = optional_env("VINPUT_ASR_BASE_URL")
    if base_url:
        return base_url.rstrip("/") + "/audio/transcriptions"

    return DEFAULT_URL


def load_config() -> ProviderConfig:
    """Load and validate all configuration from environment variables."""
    api_key = require_env("VINPUT_ASR_API_KEY")
    endpoint = resolve_endpoint()
    model = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    response_format = optional_env("VINPUT_ASR_RESPONSE_FORMAT", DEFAULT_RESPONSE_FORMAT)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    language = optional_env("VINPUT_ASR_LANGUAGE") or None
    prompt = optional_env("VINPUT_ASR_PROMPT") or None
    temperature = optional_env("VINPUT_ASR_TEMPERATURE") or None

    return ProviderConfig(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        response_format=response_format,
        timeout=timeout,
        language=language,
        prompt=prompt,
        temperature=temperature,
    )


def read_audio_input() -> bytes:
    """Read complete raw PCM audio from stdin buffer."""
    pcm_audio = sys.stdin.buffer.read()
    if not pcm_audio:
        raise VInputError("err_no_audio")
    return pcm_audio


def pcm_to_wav_bytes(pcm_audio: bytes) -> bytes:
    """Convert raw 16kHz s16le PCM to WAV container bytes."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm_audio)
    return buffer.getvalue()


def build_multipart(
    fields: Iterable[tuple[str, str]],
    files: Iterable[tuple[str, str, str, bytes]],
) -> tuple[bytes, str]:
    """Construct multipart/form-data body."""
    boundary = f"----vinput-{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    for field_name, filename, content_type, content in files:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend((f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n').encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def parse_error_payload(payload: bytes) -> str:
    """Extract readable error message from error body."""
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "Empty error response from OpenAI."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return text


def parse_transcript(payload: bytes, response_format: str) -> str:
    """Extract transcript text from response according to format."""
    text = payload.decode("utf-8", errors="replace").strip()
    if response_format in {"text", "vtt", "srt"}:
        if not text:
            raise VInputError("err_empty_transcript")
        return text

    try:
        data = json.loads(text)
    except Exception as exc:
        raise VInputError("err_transcribe_failed", detail=str(exc)) from exc

    if isinstance(data, dict):
        transcript = data.get("text")
        if isinstance(transcript, str) and transcript.strip():
            return transcript.strip()
    raise VInputError("err_empty_transcript")


def transcribe(audio_bytes: bytes, config: ProviderConfig) -> str:
    """Execute batch transcription request."""
    wav_audio = pcm_to_wav_bytes(audio_bytes)
    fields = [
        ("model", config.model),
        ("response_format", config.response_format),
    ]
    if config.language:
        fields.append(("language", config.language))
    if config.prompt:
        fields.append(("prompt", config.prompt))
    if config.temperature:
        fields.append(("temperature", config.temperature))

    body, boundary = build_multipart(
        fields=fields,
        files=[("file", "audio.wav", "audio/wav", wav_audio)],
    )

    request = Request(
        config.endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout) as response:
            return parse_transcript(response.read(), config.response_format)
    except HTTPError as exc:
        payload = exc.read()
        msg = parse_error_payload(payload)
        raise VInputError("err_http", code=exc.code, message=msg) from exc
    except URLError as exc:
        raise VInputError("err_network", detail=str(exc.reason)) from exc
    except VInputError:
        raise
    except Exception as exc:
        raise VInputError("err_transcribe_failed", detail=str(exc)) from exc


def emit_transcript(text: str) -> None:
    """Write recognized transcript string to stdout."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def run() -> int:
    """Main execution flow: load config, read audio, transcribe, emit result."""
    config = load_config()
    pcm_audio = read_audio_input()
    transcript = transcribe(pcm_audio, config)
    emit_transcript(transcript)
    return EXIT_SUCCESS


def main() -> int:
    """CLI wrapper returning standard exit code."""
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_transcribe_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
