# providers.assemblyai.batch

Cloud ASR provider script for the AssemblyAI Dictation API. It returns the
transcript already cleaned up: filler words removed, punctuation and
capitalization added, and self-corrections resolved.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: raw PCM `S16_LE`, mono, `16000 Hz` via stdin
- output: final transcript via stdout

## Environment Variables

- `VINPUT_ASR_API_KEY` required
  An AssemblyAI API key from https://www.assemblyai.com/dashboard/api-keys.
- `VINPUT_ASR_LANGUAGE` optional
  A language code such as `en` or `de`, sent as `language_codes`. Leave it
  empty to detect the language.
- `VINPUT_ASR_ASSEMBLYAI_VERBATIM` optional
  Set to `true` to output the words as spoken instead of the cleaned-up text,
  for example when an LLM scene cleans up the text afterwards. Defaults to
  `false`.
- `VINPUT_ASR_URL` optional
- `VINPUT_ASR_TIMEOUT` optional

## Notes

- If the cleanup fails, the verbatim transcript is output instead.
- API reference: https://www.assemblyai.com/docs/dictation
- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
