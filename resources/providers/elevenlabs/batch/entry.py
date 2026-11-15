#!/usr/bin/env python3
"""ElevenLabs batch speech-to-text provider."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_MODEL_ID = "scribe_v2"
DEFAULT_TIMEOUT = 60
DEFAULT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "ElevenLabs Speech Recognition",
        "err_no_audio": "No audio received on stdin.",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_http": "ElevenLabs HTTP {code}: {message}",
        "err_network": "Failed to reach ElevenLabs: {detail}",
        "err_empty_transcript": "ElevenLabs returned an empty transcript.",
        "err_transcribe_failed": "Transcription failed: {detail}",
    },
    "zh_CN": {
        "title": "ElevenLabs 语音识别",
        "err_no_audio": "标准输入未接收到音频数据。",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_http": "ElevenLabs HTTP {code}：{message}",
        "err_network": "连接 ElevenLabs 失败：{detail}",
        "err_empty_transcript": "ElevenLabs 返回了空白识别结果。",
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
    model_id: str
    timeout: int
    language_code: str | None
    enable_logging: bool
    tag_audio_events: bool
    no_verbatim: bool


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_api_key")
    return val
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


def optional_bool_env(name: str, default: bool) -> bool:
    """Read an optional boolean environment variable."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def load_config() -> ProviderConfig:
    """Load and validate all configuration from environment variables."""
    api_key = require_env("VINPUT_ASR_API_KEY")
    endpoint = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    model_id = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL_ID)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    language_code = optional_env("VINPUT_ASR_LANGUAGE") or None
    enable_logging = optional_bool_env("VINPUT_ASR_ENABLE_LOGGING", True)
    tag_audio_events = optional_bool_env("VINPUT_ASR_TAG_AUDIO_EVENTS", False)
    no_verbatim = optional_bool_env("VINPUT_ASR_ELEVENLABS_NO_VERBATIM", False)

    return ProviderConfig(
        api_key=api_key,
        endpoint=endpoint,
        model_id=model_id,
        timeout=timeout,
        language_code=language_code,
        enable_logging=enable_logging,
        tag_audio_events=tag_audio_events,
        no_verbatim=no_verbatim,
    )


def read_audio_input() -> bytes:
    """Read complete raw PCM audio from stdin buffer."""
    pcm_audio = sys.stdin.buffer.read()
    if not pcm_audio:
        raise VInputError("err_no_audio")
    return pcm_audio


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
        return "Empty error response from ElevenLabs."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return text


def transcribe(audio_bytes: bytes, config: ProviderConfig) -> str:
    """Execute batch transcription request."""
    query = urlencode({"enable_logging": str(config.enable_logging).lower()})
    url = f"{config.endpoint}?{query}" if query else config.endpoint

    fields = [
        ("model_id", config.model_id),
        ("file_format", "pcm_s16le_16"),
        ("tag_audio_events", str(config.tag_audio_events).lower()),
        ("no_verbatim", str(config.no_verbatim).lower()),
    ]
    if config.language_code:
        fields.append(("language_code", config.language_code))

    body, boundary = build_multipart(
        fields=fields,
        files=[("file", "audio.pcm", "application/octet-stream", audio_bytes)],
    )

    request = Request(
        url,
        data=body,
        headers={
            "xi-api-key": config.api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout) as response:
            data = json.loads(response.read())
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

    if not isinstance(data, dict):
        raise VInputError("err_transcribe_failed", detail="Unexpected response from ElevenLabs.")

    text = data.get("text")
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
