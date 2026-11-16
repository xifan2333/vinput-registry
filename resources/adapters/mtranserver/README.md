# adapter.mtranserver

Managed local LLM adapter that exposes MTranServer translation bridge for `vinput-daemon`.

Translates input transcripts using a local or remote MTranServer instance. Operates as a command pipe filter (`stdin -> stdout`) for `vinput-daemon`.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only (`dataclasses`, `json`, `os`, `re`, `sys`, `urllib`)

## Environment Variables

### Optional
- `MTRAN_URL` (optional, default: `http://127.0.0.1:8989`): Endpoint URL of the MTranServer instance.
- `MTRAN_TOKEN` (optional, default: `""`): Bearer token for authentication if required by MTranServer.
- `MTRAN_TARGET_LANG` (optional, default: `"en"`): Default target translation language.
