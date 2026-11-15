#!/usr/bin/env python3
"""Doubao (ByteDance Volcengine) batch speech-to-text provider."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import uuid
import wave
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
DEFAULT_MODEL_NAME = "bigmodel"
DEFAULT_TIMEOUT = 60
SUCCESS_CODE = 20000000

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Doubao Speech Recognition",
        "err_no_audio": "No audio received on stdin.",
        "err_missing_config": "Missing required configuration: {detail}",
        "err_http": "Doubao ASR HTTP {code}: {message}",
        "err_network": "Failed to reach Doubao ASR: {detail}",
        "err_status": "Doubao ASR returned status {code}: {message}",
        "err_empty_transcript": "Doubao ASR returned an empty transcript.",
        "err_transcribe_failed": "Transcription failed: {detail}",
    },
    "zh_CN": {
        "title": "豆包语音识别",
        "err_no_audio": "标准输入未接收到音频数据。",
        "err_missing_config": "缺少必要配置：{detail}",
        "err_http": "豆包语音识别 HTTP {code}：{message}",
        "err_network": "连接豆包语音识别服务失败：{detail}",
        "err_status": "豆包语音识别返回状态码 {code}：{message}",
        "err_empty_transcript": "豆包语音识别返回了空白识别结果。",
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

    app_id: str
    access_token: str
    endpoint: str
    resource_id: str
    model_name: str
    user_id: str
    timeout: int


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_config", detail=name)
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


def load_config() -> ProviderConfig:
    """Load and validate all configuration from environment variables."""
    app_id = require_env("VINPUT_ASR_APP_ID")
    access_token = require_env("VINPUT_ASR_ACCESS_TOKEN")
    endpoint = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    resource_id = optional_env("VINPUT_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID)
    model_name = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL_NAME)
    user_id = optional_env("VINPUT_ASR_USER_ID", app_id)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)

    return ProviderConfig(
        app_id=app_id,
        access_token=access_token,
        endpoint=endpoint,
        resource_id=resource_id,
        model_name=model_name,
        user_id=user_id,
        timeout=timeout,
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
        return "Empty error response from Doubao ASR."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(data, dict):
        for key in ("message", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        result = data.get("result")
        if isinstance(result, dict):
            text_value = result.get("text")
            if isinstance(text_value, str) and text_value.strip():
                return text_value.strip()
    return text


def transcribe(audio_bytes: bytes, config: ProviderConfig) -> str:
    """Execute batch transcription request."""
    wav_audio = pcm_to_wav_bytes(audio_bytes)
    payload = json.dumps(
        {
            "user": {"uid": config.user_id},
            "audio": {"data": base64.b64encode(wav_audio).decode("ascii")},
            "request": {"model_name": config.model_name},
        }
    ).encode("utf-8")

    request = Request(
        config.endpoint,
        data=payload,
        headers={
            "X-Api-App-Key": config.app_id,
            "X-Api-Access-Key": config.access_token,
            "X-Api-Resource-Id": config.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout) as response:
            body = response.read()
            status_code = response.headers.get("X-Api-Status-Code", "").strip()
            status_message = response.headers.get("X-Api-Message", "").strip()
    except HTTPError as exc:
        payload_err = exc.read()
        msg = parse_error_payload(payload_err)
        raise VInputError("err_http", code=exc.code, message=msg) from exc
    except URLError as exc:
        raise VInputError("err_network", detail=str(exc.reason)) from exc
    except VInputError:
        raise
    except Exception as exc:
        raise VInputError("err_transcribe_failed", detail=str(exc)) from exc

    if status_code and status_code != str(SUCCESS_CODE):
        message = status_message or parse_error_payload(body)
        raise VInputError("err_status", code=status_code, message=message)

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise VInputError("err_transcribe_failed", detail=str(exc)) from exc

    result = data.get("result")
    if not isinstance(result, dict):
        raise VInputError("err_transcribe_failed", detail="Doubao ASR returned an unexpected response.")

    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise VInputError("err_empty_transcript")
    return text.strip()


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
