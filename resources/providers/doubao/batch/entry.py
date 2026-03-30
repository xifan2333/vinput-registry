#!/usr/bin/env python3

import base64
import io
import json
import os
import sys
import uuid
import wave
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
)
DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
DEFAULT_MODEL_NAME = "bigmodel"
DEFAULT_TIMEOUT = 60
SUCCESS_CODE = 20000000
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


def transcribe(
    pcm_audio: bytes,
    app_id: str,
    access_token: str,
    endpoint: str,
    resource_id: str,
    model_name: str,
    user_id: str,
    timeout: int,
) -> str:
    wav_audio = pcm_to_wav_bytes(pcm_audio)
    payload = json.dumps(
        {
            "user": {"uid": user_id},
            "audio": {"data": base64.b64encode(wav_audio).decode("ascii")},
            "request": {"model_name": model_name},
        }
    ).encode("utf-8")

    request = Request(
        endpoint,
        data=payload,
        headers={
            "X-Api-App-Key": app_id,
            "X-Api-Access-Key": access_token,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        status_code = response.headers.get("X-Api-Status-Code", "").strip()
        status_message = response.headers.get("X-Api-Message", "").strip()

    if status_code and status_code != str(SUCCESS_CODE):
        message = status_message or parse_error_payload(body)
        raise RuntimeError(
            f"Doubao ASR returned status {status_code}: {message}"
        )

    data = json.loads(body.decode("utf-8"))
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Doubao ASR returned an unexpected response.")

    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Doubao ASR returned an empty transcript.")
    return text.strip()


def main() -> int:
    try:
        app_id = get_required_env("VINPUT_ASR_APP_ID")
        access_token = get_required_env("VINPUT_ASR_ACCESS_TOKEN")
        endpoint = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
        resource_id = get_optional_env(
            "VINPUT_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID
        )
        model_name = get_optional_env(
            "VINPUT_ASR_MODEL", DEFAULT_MODEL_NAME
        )
        user_id = get_optional_env("VINPUT_ASR_USER_ID", app_id)
        timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
        pcm_audio = read_audio_input()

        text = transcribe(
            pcm_audio=pcm_audio,
            app_id=app_id,
            access_token=access_token,
            endpoint=endpoint,
            resource_id=resource_id,
            model_name=model_name,
            user_id=user_id,
            timeout=timeout,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    except HTTPError as exc:
        payload = exc.read()
        print(
            f"Doubao ASR HTTP {exc.code}: {parse_error_payload(payload)}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR
    except URLError as exc:
        print(f"Failed to reach Doubao ASR: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
