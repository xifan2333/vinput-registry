#!/usr/bin/env python3

import base64
import io
import json
import os
import sys
import wave
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "https://api.xiaomimimo.com/v1/chat/completions"
DEFAULT_MODEL = "mimo-v2.5-asr"
DEFAULT_TIMEOUT = 60
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


def read_audio_input() -> bytes:
    pcm_audio = sys.stdin.buffer.read()
    if not pcm_audio:
        raise ValueError("No audio received on stdin.")
    return pcm_audio


def pcm_to_wav_bytes(pcm_audio: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm_audio)
    return buffer.getvalue()


def parse_error_payload(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "Empty error response from MiMo."

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


def resolve_endpoint() -> str:
    explicit_url = get_optional_env("VINPUT_ASR_URL")
    if explicit_url:
        return explicit_url

    base_url = get_optional_env("VINPUT_ASR_BASE_URL")
    if base_url:
        return base_url.rstrip("/") + "/chat/completions"

    return DEFAULT_URL


def build_request_body(
    model: str,
    data_uri: str,
    language: str | None,
    prompt: str | None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if prompt:
        messages.append({"role": "system", "content": prompt})

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

    asr_options: dict[str, Any] = {}
    if language:
        asr_options["language"] = language

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if asr_options:
        body["asr_options"] = asr_options

    return body


def parse_transcript(payload: bytes) -> str:
    text_payload = payload.decode("utf-8", errors="replace").strip()
    if not text_payload:
        raise RuntimeError("MiMo ASR returned an empty response.")

    data = json.loads(text_payload)
    if not isinstance(data, dict):
        raise RuntimeError("MiMo ASR returned an unexpected response.")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("MiMo ASR returned no choices.")

    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("MiMo ASR returned an invalid choice.")

    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("MiMo ASR returned no message content.")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("MiMo ASR returned an empty transcript.")
    return content.strip()


def transcribe(
    pcm_audio: bytes,
    api_key: str,
    endpoint: str,
    model: str,
    timeout: int,
    language: str | None,
    prompt: str | None,
) -> str:
    wav_audio = pcm_to_wav_bytes(pcm_audio)
    data_uri = (
        "data:audio/wav;base64,"
        + base64.b64encode(wav_audio).decode("ascii")
    )
    payload = build_request_body(
        model=model,
        data_uri=data_uri,
        language=language,
        prompt=prompt,
    )

    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        return parse_transcript(response.read())


def main() -> int:
    try:
        api_key = get_required_env("VINPUT_ASR_API_KEY")
        endpoint = resolve_endpoint()
        model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
        timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
        language = get_optional_env("VINPUT_ASR_LANGUAGE", "auto") or None
        prompt = get_optional_env("VINPUT_ASR_PROMPT") or None
        pcm_audio = read_audio_input()

        text = transcribe(
            pcm_audio=pcm_audio,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            language=language,
            prompt=prompt,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    except HTTPError as exc:
        payload = exc.read()
        print(
            f"MiMo ASR HTTP {exc.code}: {parse_error_payload(payload)}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR
    except URLError as exc:
        print(f"Failed to reach MiMo ASR: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
