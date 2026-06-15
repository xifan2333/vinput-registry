# providers.elevenlabs.streaming

Cloud ASR provider script for the ElevenLabs realtime speech-to-text API.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: JSONL via stdin
- output: JSONL via stdout
- diagnostics: stderr only
- dependencies: Python standard library only

## Input Protocol

- `{"type":"audio","audio_base64":"...","commit":false}`
- `{"type":"audio","audio_base64":"...","commit":true}`
- `{"type":"finish"}`
- `{"type":"cancel"}`

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"final_timestamps","text":"...","words":[...]}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

Normalized transcript semantics used by this script:

- `partial.text` is the full user-visible transcript at the current moment.
- `final.text` is the full confirmed transcript at the current moment.
- The script accumulates committed segments locally before emitting output.
- `segment_final: true` is included on committed transcript events.

## Environment Variables

- `VINPUT_ASR_API_KEY` required
- `VINPUT_ASR_MODEL` optional
- `VINPUT_ASR_LANGUAGE` optional
- `VINPUT_ASR_URL` optional
- `VINPUT_ASR_AUDIO_FORMAT` optional
- `VINPUT_ASR_INCLUDE_TIMESTAMPS` optional
- `VINPUT_ASR_INCLUDE_LANGUAGE_DETECTION` optional
- `VINPUT_ASR_COMMIT_STRATEGY` optional
- `VINPUT_ASR_ELEVENLABS_NO_VERBATIM` optional
  Set to `true` to ask ElevenLabs to remove filler words, false starts,
  non-speech sounds, and disfluencies. Supported by ElevenLabs Scribe v2
  models. Defaults to `false`.
- `VINPUT_ASR_VAD_SILENCE_THRESHOLD_SECS` optional
- `VINPUT_ASR_VAD_THRESHOLD` optional
- `VINPUT_ASR_MIN_SPEECH_DURATION_MS` optional
- `VINPUT_ASR_MIN_SILENCE_DURATION_MS` optional
- `VINPUT_ASR_ENABLE_LOGGING` optional
- `VINPUT_ASR_TIMEOUT` optional
- `VINPUT_ASR_FINISH_GRACE_SECS` optional

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
- `finish` does not synthesize a commit; callers should mark the final audio chunk with `commit: true` when they need a final transcript.
