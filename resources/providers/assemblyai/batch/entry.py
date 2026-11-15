#!/usr/bin/env python3

import json
import os
import sys
import uuid
from collections.abc import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 60
DEFAULT_URL = "https://dictation.assemblyai.com/v1/transcribe"
SAMPLE_RATE = 16000
CHANNELS = 1
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


def build_config(language: str | None) -> dict:
    config: dict = {"sample_rate": SAMPLE_RATE, "channels": CHANNELS}
    if language:
        config["language_codes"] = [language]
    return config


def build_multipart(parts: Iterable[tuple[str, str | None, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----vinput-{uuid.uuid4().hex}"
    body = bytearray()

    for name, filename, content_type, content in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        body.extend(f"{disposition}\r\n".encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def parse_error_payload(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "Empty error response from AssemblyAI."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(data, dict):
        for key in ("error", "detail", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def pick_text(data: dict, verbatim: bool) -> str:
    # llm_response is the cleaned-up text; it is empty when the cleanup failed,
    # and the verbatim transcript in text is still returned.
    candidates = ["text"] if verbatim else ["llm_response", "text"]
    for key in candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError("AssemblyAI returned an empty transcript.")


def transcribe(
    pcm_audio: bytes,
    api_key: str,
    language: str | None,
    timeout: int,
    endpoint: str,
    verbatim: bool,
) -> str:
    body, boundary = build_multipart(
        [
            ("config", None, "application/json", json.dumps(build_config(language)).encode()),
            ("audio", "audio.pcm", "audio/pcm", pcm_audio),
        ]
    )

    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected response from AssemblyAI.")
    return pick_text(data, verbatim)


def main() -> int:
    try:
        api_key = get_required_env("VINPUT_ASR_API_KEY")
        language = get_optional_env("VINPUT_ASR_LANGUAGE") or None
        endpoint = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
        timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
        verbatim = get_optional_bool_env("VINPUT_ASR_ASSEMBLYAI_VERBATIM", False)
        pcm_audio = read_audio_input()

        text = transcribe(
            pcm_audio=pcm_audio,
            api_key=api_key,
            language=language,
            timeout=timeout,
            endpoint=endpoint,
            verbatim=verbatim,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    except HTTPError as exc:
        payload = exc.read()
        message = parse_error_payload(payload)
        print(f"AssemblyAI HTTP {exc.code}: {message}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except URLError as exc:
        print(f"Failed to reach AssemblyAI: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
