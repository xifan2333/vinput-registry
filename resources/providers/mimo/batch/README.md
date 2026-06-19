# providers.mimo.batch

Cloud ASR provider script for Xiaomi MiMo ASR over the OpenAI-compatible chat completions API.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: raw PCM `S16_LE`, mono, `16000 Hz` via stdin
- output: final transcript via stdout

## Environment Variables

- `VINPUT_ASR_API_KEY` required
- `VINPUT_ASR_URL` optional
- `VINPUT_ASR_BASE_URL` optional
- `VINPUT_ASR_MODEL` optional
- `VINPUT_ASR_LANGUAGE` optional
- `VINPUT_ASR_PROMPT` optional
- `VINPUT_ASR_TIMEOUT` optional

## Notes

- Default endpoint: `https://api.xiaomimimo.com/v1/chat/completions`
- Audio is wrapped as `data:audio/wav;base64,...` and sent in `messages[].content[].input_audio.data`.
- `VINPUT_ASR_PROMPT` is mapped to a leading system message.
- `VINPUT_ASR_LANGUAGE` defaults to `auto` and is forwarded through `asr_options`.
