# providers.bailian.qwen-audio3-batch

Cloud ASR provider script for Alibaba Bailian `qwen-audio-3.1-asr-flash` over the native DashScope Multimodal Generation HTTP API.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: raw PCM `S16_LE`, mono, `16000 Hz` via stdin
- output: final transcript via stdout

## Environment Variables

- `VINPUT_ASR_API_KEY`: required
- `VINPUT_ASR_MODEL`: optional (default: `qwen-audio-3.1-asr-flash`)
- `VINPUT_ASR_LANGUAGE`: optional (e.g. `zh`, `en`, `ja`, `ko`)
- `VINPUT_ASR_PROMPT`: optional contextual hint / hotword text
- `VINPUT_ASR_WORKSPACE_ID`: optional Bailian Workspace ID
- `VINPUT_ASR_URL`: optional explicit endpoint URL
- `VINPUT_ASR_BASE_URL`: optional base URL
- `VINPUT_ASR_TIMEOUT`: optional request timeout in seconds (default: `60`)

## Notes

- This script calls Bailian native `POST /api/v1/services/aigc/multimodal-generation/generation`.
- Uses token-based pricing (`qwen-audio-3.1-asr-flash`), significantly lowering costs compared to second-based models.
- Audio is wrapped as `data:audio/wav;base64,...` inside `messages[].content[].input_audio.data`.
- `VINPUT_ASR_PROMPT` is mapped to a leading contextual `input_text` user message.
- `VINPUT_ASR_LANGUAGE` is mapped to `parameters.language_hints`.
