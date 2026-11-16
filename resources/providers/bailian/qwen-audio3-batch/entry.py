#!/usr/bin/env python3
"""Aliyun Bailian Qwen-Audio native batch speech-to-text provider.

Uses the DashScope Multimodal Generation HTTP API for qwen-audio-3.1-asr-flash.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import wave
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen-audio-3.1-asr-flash"
DEFAULT_TIMEOUT = 60

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Bailian Qwen-Audio Speech Recognition",
        "err_no_audio": "No audio received on stdin.",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_http": "Bailian ASR HTTP {code}: {message}",
        "err_network": "Failed to reach Bailian ASR: {detail}",
        "err_empty_transcript": "Bailian ASR returned an empty transcript.",
        "err_transcribe_failed": "Transcription failed: {detail}",
    },
    "zh_CN": {
        "title": "百炼 Qwen-Audio 语音识别",
        "err_no_audio": "标准输入未接收到音频数据。",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_http": "百炼语音识别 HTTP {code}：{message}",
        "err_network": "连接百炼语音识别服务失败：{detail}",
        "err_empty_transcript": "百炼语音识别返回了空白识别结果。",
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


@dataclass
class ProviderConfig:
    api_key: str
    endpoint: str
    model: str
    timeout: int
    language: str | None
    prompt: str | None
    workspace_id: str | None


def require_env(name: str) -> str:
    """Read mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_api_key")
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read optional environment variable with fallback default."""
    return os.getenv(name, default).strip()


def optional_int_env(name: str, default: int) -> int:
    """Read optional integer environment variable."""
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def resolve_endpoint() -> str:
    """Resolve endpoint URL from envs, falling back to DashScope default."""
    explicit_url = optional_env("VINPUT_ASR_URL")
    if explicit_url:
        return explicit_url

    workspace_id = optional_env("VINPUT_ASR_WORKSPACE_ID")
    if workspace_id:
        return (
            f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        )

    base_url = optional_env("VINPUT_ASR_BASE_URL")
    if base_url:
        return base_url.rstrip("/") + "/api/v1/services/aigc/multimodal-generation/generation"

    return DEFAULT_URL


def load_config() -> ProviderConfig:
    """Load and validate all configuration from environment variables."""
    api_key = require_env("VINPUT_ASR_API_KEY")
    endpoint = resolve_endpoint()
    model = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    language = optional_env("VINPUT_ASR_LANGUAGE") or None
    prompt = optional_env("VINPUT_ASR_PROMPT") or None
    workspace_id = optional_env("VINPUT_ASR_WORKSPACE_ID") or None

    return ProviderConfig(
        api_key=api_key,
        endpoint=endpoint,
        model=model,
        timeout=timeout,
        language=language,
        prompt=prompt,
        workspace_id=workspace_id,
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


def parse_error_payload(payload: bytes) -> str:
    """Extract readable error message from error body."""
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "Empty error response from Bailian."

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

        code = data.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()

    return text


def build_request_body(
    model: str,
    data_uri: str,
    language: str | None,
    prompt: str | None,
) -> dict[str, Any]:
    """Construct Bailian native multimodal generation request body."""
    messages: list[dict[str, Any]] = []
    if prompt:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    }
                ],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": data_uri},
                }
            ],
        }
    )

    parameters: dict[str, Any] = {
        "format": "wav",
        "sample_rate": "16000",
    }
    if language:
        hints = [item.strip() for item in language.split(",") if item.strip()]
        if hints:
            parameters["language_hints"] = [hints[0]]

    return {
        "model": model,
        "input": {"messages": messages},
        "parameters": parameters,
    }


def parse_transcript(payload: bytes) -> str:
    """Extract transcript text from successful multimodal generation response."""
    text_payload = payload.decode("utf-8", errors="replace").strip()
    if not text_payload:
        raise VInputError("err_empty_transcript")

    try:
        data = json.loads(text_payload)
    except json.JSONDecodeError as exc:
        raise VInputError("err_transcribe_failed", detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise VInputError("err_transcribe_failed", detail="Bailian ASR returned non-object response.")

    output = data.get("output")
    if not isinstance(output, dict):
        raise VInputError("err_transcribe_failed", detail="Bailian ASR returned no output object.")

    text = output.get("text")
    if not isinstance(text, str) or not text.strip():
        raise VInputError("err_empty_transcript")

    return text.strip()


def transcribe(audio_bytes: bytes, config: ProviderConfig) -> str:
    """Execute batch transcription request."""
    wav_audio = pcm_to_wav_bytes(audio_bytes)
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav_audio).decode("ascii")
    payload = build_request_body(
        model=config.model,
        data_uri=data_uri,
        language=config.language,
        prompt=config.prompt,
    )

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-DashScope-SSE": "disable",
    }
    if config.workspace_id:
        headers["X-DashScope-WorkSpace"] = config.workspace_id

    request = Request(
        config.endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout) as response:
            return parse_transcript(response.read())
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
