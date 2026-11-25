#!/usr/bin/env python3
"""Managed local LLM adapter: SiliconFlow text polishing bridge.

Polishes and refines ASR transcripts using SiliconFlow OpenAI-compatible
chat completions API (https://api.siliconflow.cn/v1/chat/completions).
Operates as a command pipe filter (stdin -> stdout) for vinput-daemon.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

DEFAULT_SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_POLISH_SYSTEM_PROMPT = (
    "你是一名专业的文本润色助手。请对用户提供的语音识别文本进行润色，"
    "修正错别字、去除口语化冗余、优化标点与语句通顺度，"
    "保持原意不变，直接输出润色后的文本，不要添加任何解释或前缀。"
)

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "SiliconFlow Polish Adapter",
        "missing_env": "Missing required environment variable '{name}'.",
        "err_polish_failed": "SiliconFlow text polishing failed: {detail}",
        "err_network": "Network error communicating with SiliconFlow: {detail}",
        "err_empty_response": "SiliconFlow returned an empty response.",
    },
    "zh_CN": {
        "title": "硅基流动润色适配器",
        "missing_env": "缺少必需的环境变量 '{name}'。",
        "err_polish_failed": "硅基流动文本润色失败：{detail}",
        "err_network": "连接硅基流动发生网络错误：{detail}",
        "err_empty_response": "硅基流动返回了空响应。",
    },
}


class VInputError(Exception):
    """Base exception for structured vinput provider/adapter errors."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Configuration error resulting in usage exit code."""

    exit_code = EXIT_USAGE_ERROR


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


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("missing_env", name=name)
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration parsed from environment variables."""

    url: str
    api_key: str
    model: str
    system_prompt: str
    temperature: float


def load_config() -> AdapterConfig:
    """Load and validate all adapter runtime configuration from environment."""
    url = optional_env("SILICONFLOW_URL", DEFAULT_SILICONFLOW_URL).rstrip("/")
    api_key = require_env("SILICONFLOW_API_KEY")
    model = optional_env("SILICONFLOW_MODEL", DEFAULT_SILICONFLOW_MODEL)
    system_prompt = optional_env("SILICONFLOW_SYSTEM_PROMPT", DEFAULT_POLISH_SYSTEM_PROMPT)
    temperature_raw = optional_env("SILICONFLOW_TEMPERATURE", "0.3")
    try:
        temperature = float(temperature_raw)
    except (TypeError, ValueError):
        temperature = 0.3
    return AdapterConfig(
        url=url,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        temperature=temperature,
    )


def call_siliconflow(text: str, config: AdapterConfig) -> str:
    """Call SiliconFlow chat completions endpoint for text polishing."""
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": config.temperature,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }

    req = Request(config.url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise VInputError(
            "err_network",
            detail=f"Failed to connect to SiliconFlow at {config.url}: {exc.reason}",
        ) from exc

    try:
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        result = str(message.get("content", "")).strip()
    except (IndexError, AttributeError, TypeError) as exc:
        raise VInputError("err_polish_failed", detail=f"Unexpected response shape: {exc}") from exc

    if not result:
        raise VInputError("err_empty_response")
    return result


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite raw dictation transcript by polishing it via SiliconFlow."""
    clean = text.strip()
    if not clean:
        return ""
    result = call_siliconflow(clean, config)
    return [result] if result else []


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite text in command mode using selected text as primary context."""
    target_text = selected.strip() if selected.strip() else text.strip()
    if not target_text:
        return ""
    result = call_siliconflow(target_text, config)
    return [result] if result else []


def run() -> int:
    """Main filter loop reading stdin and writing candidates to stdout."""
    config = load_config()

    raw_input = sys.stdin.read()
    if not raw_input:
        return EXIT_SUCCESS

    is_json = False
    text = raw_input
    selected = ""

    try:
        data = json.loads(raw_input)
        if isinstance(data, dict) and "text" in data:
            is_json = True
            text = str(data.get("text", ""))
            selected = str(data.get("selected_text", ""))
    except Exception:
        is_json = False

    if is_json and selected:
        output = rewrite_command(text, selected, config)
    else:
        output = rewrite_text(text, config)

    if isinstance(output, list):
        sys.stdout.write(json.dumps({"candidates": output}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(f"{output}\n")
    sys.stdout.flush()
    return EXIT_SUCCESS


def main() -> int:
    """CLI wrapper returning standard exit code."""
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_polish_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
