# adapter.doubao-organize

Managed local LLM adapter that connects to Doubao IME AI text organization service to polish and structure ASR transcripts for `vinput-daemon`.

Operates as a command pipe filter (`stdin -> stdout`) for `vinput-daemon`.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only (`base64`, `codecs`, `dataclasses`, `fcntl`, `gzip`, `hashlib`, `json`, `os`, `pathlib`, `secrets`, `sys`, `tempfile`, `threading`, `time`, `unicodedata`, `urllib`, `uuid`)

## Environment Variables

### Optional
- `DOUBAOIME_IDENTITY_PATH` (optional, default: `~/.cache/vinput/doubaoime/credentials.json`): Persistent credentials path.
- `DOUBAOIME_NO_TRAILING_PUNCTUATION` (optional, default: `false`): Strip trailing punctuation from organized text.
