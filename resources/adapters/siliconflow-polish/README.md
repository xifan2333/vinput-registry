# adapter.siliconflow-polish

Managed local LLM adapter that polishes ASR transcripts via the SiliconFlow
OpenAI-compatible chat completions API
(`https://api.siliconflow.cn/v1/chat/completions`).

Operates as a command pipe filter (`stdin -> stdout`) for `vinput-daemon`.
Receives raw speech-recognition text and returns a polished, corrected,
and fluent version suitable for direct insertion at the input cursor.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only (`dataclasses`, `json`, `os`, `sys`, `urllib`)

## Environment Variables

### Required

- `SILICONFLOW_API_KEY` (required): SiliconFlow API key used as `Bearer`
  credential. Obtain one from the SiliconFlow console
  (<https://cloud.siliconflow.cn>).

### Optional

- `SILICONFLOW_URL` (optional, default: `https://api.siliconflow.cn/v1/chat/completions`):
  Custom upstream chat completions endpoint. Override to point at a
  self-hosted or OpenAI-compatible gateway.
- `SILICONFLOW_MODEL` (optional, default: `Qwen/Qwen2.5-7B-Instruct`):
  Model identifier supported by SiliconFlow. Any chat model available on the
  platform works (e.g. `deepseek-ai/DeepSeek-V3`, `Qwen/Qwen2.5-14B-Instruct`).
- `SILICONFLOW_SYSTEM_PROMPT` (optional, default: built-in Chinese polishing
  prompt): Custom system prompt instructing the model how to polish the
  transcript.
- `SILICONFLOW_TEMPERATURE` (optional, default: `0.3`): Sampling temperature
  for the completion. Lower values yield more deterministic, conservative
  edits; higher values allow more creative restructuring.

## Protocol

- **stdin**: Raw transcript string (dictation mode) or JSON
  `{"text": "<asr>", "selected_text": "<context>"}` (command mode).
- **stdout**: Polished text (single string) or
  `{"candidates": ["<polished>"]}` when multiple candidates are produced.
- **stderr**: JSONL desktop notification `{"title": "...", "body": "..."}`
  on error. Silent during normal operation.
- **exit**: `0` on success, `1` on runtime error, `2` on usage/config error.
