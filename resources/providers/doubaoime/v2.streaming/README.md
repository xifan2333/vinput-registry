# provider.doubaoime.v2.streaming

Streaming speech recognition provider for vinput.

## Runtime

- command: `python3`
- system library: `libopus`

The managed script path is `~/.local/share/vinput/providers/doubaoime/v2.streaming`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VINPUT_ASR_CREDENTIAL_PATH` | `~/.cache/vinput/doubaoime/credentials.json` | Anonymous device identity cache. |
| `VINPUT_ASR_LIBOPUS_PATH` | System library lookup | Custom path to the system libopus shared library. |
| `VINPUT_ASR_NO_TRAILING_PUNCTUATION` | false | `1`, `true`, or `yes` removes final punctuation locally, preserving punctuation inside sentences and closing quotes/brackets. |
| `VINPUT_ASR_FINISH_GRACE_SECS` | `9` | Wait after finish, greater than zero and at most 9 seconds. |

CLI equivalents are `--identity`, `--finish-grace`, and `--no-trailing-punctuation`. Set the punctuation option on both the provider and organization adapter when using the organization scene.

The default cache imports a complete identity from the previous (v1) anonymous cache, then the legacy ASR cache, in that order.

## JSONL Protocol

stdin accepts `{"type":"audio","audio_base64":"...","commit":false}`, `{"type":"finish"}`, and `{"type":"cancel"}`. Audio is mono signed 16-bit little-endian PCM at 16000 Hz, without a WAV header. `commit:true`, finish, and EOF terminate audio once. Audio received after finish is an error.
