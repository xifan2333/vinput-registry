#!/usr/bin/env python3

import json
import os
import sys
import uuid
from typing import Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_MODEL_ID = "scribe_v2"
DEFAULT_TIMEOUT = 60
DEFAULT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}.")
    return value


def get_optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def get_optional_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return int(value)


def get_optional_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def read_audio_input() -> bytes:
    pcm_audio = sys.stdin.buffer.read()
    if not pcm_audio:
        raise ValueError("No audio received on stdin.")
    return pcm_audio


def build_multipart(
    fields: Iterable[Tuple[str, str]],
    files: Iterable[Tuple[str, str, str, bytes]],
) -> Tuple[bytes, str]:
    boundary = f"----vinput-{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value.encode())
        body.extend(b"\r\n")

    for field_name, filename, content_type, content in files:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def parse_error_payload(payload: bytes) -> str:
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


def transcribe(
    pcm_audio: bytes,
    api_key: str,
    model_id: str,
    language_code: Optional[str],
    timeout: int,
    endpoint: str,
    enable_logging: bool,
    tag_audio_events: bool,
    no_verbatim: bool,
) -> str:
    query = urlencode({"enable_logging": str(enable_logging).lower()})
    url = endpoint
    if query:
        url = f"{endpoint}?{query}"

    fields = [
        ("model_id", model_id),
        ("file_format", "pcm_s16le_16"),
        ("tag_audio_events", str(tag_audio_events).lower()),
        ("no_verbatim", str(no_verbatim).lower()),
    ]
    if language_code:
        fields.append(("language_code", language_code))

    body, boundary = build_multipart(
        fields=fields,
        files=[("file", "audio.pcm", "application/octet-stream", pcm_audio)],
    )

    request = Request(
        url,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("ElevenLabs returned an empty transcript.")
    return text.strip()


def main() -> int:
    try:
        api_key = get_required_env("VINPUT_ASR_API_KEY")
        model_id = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL_ID)
        language_code = get_optional_env("VINPUT_ASR_LANGUAGE") or None
        endpoint = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
        timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
        enable_logging = get_optional_bool_env("VINPUT_ASR_ENABLE_LOGGING", True)
        tag_audio_events = get_optional_bool_env(
            "VINPUT_ASR_TAG_AUDIO_EVENTS", False
        )
        no_verbatim = get_optional_bool_env(
            "VINPUT_ASR_ELEVENLABS_NO_VERBATIM", False
        )
        pcm_audio = read_audio_input()

        text = transcribe(
            pcm_audio=pcm_audio,
            api_key=api_key,
            model_id=model_id,
            language_code=language_code,
            timeout=timeout,
            endpoint=endpoint,
            enable_logging=enable_logging,
            tag_audio_events=tag_audio_events,
            no_verbatim=no_verbatim,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    except HTTPError as exc:
        payload = exc.read()
        message = parse_error_payload(payload)
        print(f"ElevenLabs HTTP {exc.code}: {message}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except URLError as exc:
        print(f"Failed to reach ElevenLabs: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
