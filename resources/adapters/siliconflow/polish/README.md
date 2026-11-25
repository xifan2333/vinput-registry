# adapter.siliconflow.polish

Managed local LLM adapter that polishes ASR transcripts via any
**OpenAI-compatible** chat completions API. Works with OpenAI, SiliconFlow,
DeepSeek, Moonshot, vLLM, and any other service exposing the standard
`/v1/chat/completions` endpoint.

Operates as a command pipe filter (`stdin -> stdout`) for `vinput-daemon`.
Receives raw speech-recognition text and returns a polished, corrected,
and fluent version suitable for direct insertion at the input cursor.

## Entry

- `resources/adapters/siliconflow/polish/entry.py`

## Runtime

- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only (`dataclasses`, `json`, `os`, `sys`, `urllib`)

## Environment Variables

### Required

- `OPENAI_API_KEY` (required): API key sent as the `Authorization: Bearer`
  credential. Use the key issued by your chosen OpenAI-compatible provider.

### Optional

- `OPENAI_API_URL` (optional, default: `https://api.openai.com/v1/chat/completions`):
  Full chat completions endpoint. Override to target SiliconFlow
  (`https://api.siliconflow.cn/v1/chat/completions`), DeepSeek, Moonshot,
  a local vLLM server, or any other OpenAI-compatible gateway.
- `OPENAI_MODEL` (optional, default: `gpt-4o-mini`): Model identifier accepted
  by the endpoint. Examples: `gpt-4o-mini`, `deepseek-chat`,
  `Qwen/Qwen2.5-7B-Instruct`, `THUDM/GLM-4-9B-0414`.
- `OPENAI_SYSTEM_PROMPT` (optional, default: built-in Chinese polishing
  prompt): Custom system prompt instructing the model how to polish the
  transcript.
- `OPENAI_TEMPERATURE` (optional, default: `0.3`): Sampling temperature.
  Lower values yield more deterministic, conservative edits; higher values
  allow more creative restructuring.

## Protocol

- **stdin**: Raw transcript string (dictation mode) or JSON
  `{"text": "<asr>", "selected_text": "<context>"}` (command mode).
- **stdout**: Polished text (single string) or
  `{"candidates": ["<polished>"]}` when multiple candidates are produced.
- **stderr**: JSONL desktop notification `{"title": "...", "body": "..."}`
  on error. Silent during normal operation.
- **exit**: `0` on success, `1` on runtime error, `2` on usage/config error.
