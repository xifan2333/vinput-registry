#!/usr/bin/env python3
"""Mobile web remote text input trigger for vinput.

Hosts a lightweight web interface and WebSocket endpoint over local network,
translating mobile typing and speech into vinput `inject` commands on stdout.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import socketserver
import struct
import sys
import threading
from typing import Any, cast

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VInput Remote</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #111418;
      --panel: #1a2027;
      --panel-2: #202832;
      --text: #f2f5f8;
      --muted: #9aa7b5;
      --border: #34404c;
      --accent: #1f9d87;
      --danger: #e15a5a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(760px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 40px 0;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 28px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .sub {
      color: var(--muted);
      font-size: 14px;
      margin-bottom: 24px;
    }
    .status {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .tile {
      border: 1px solid var(--border);
      background: var(--panel);
      border-radius: 8px;
      padding: 14px;
      min-height: 76px;
    }
    .label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }
    .value {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 16px;
      font-weight: 600;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--danger);
      flex: 0 0 auto;
    }
    .dot.on { background: var(--accent); }
    textarea {
      width: 100%;
      min-height: 45vh;
      resize: vertical;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--text);
      padding: 16px;
      font: 18px/1.6 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      outline: none;
    }
    textarea:focus { border-color: var(--accent); }
    textarea:disabled { opacity: 0.58; cursor: not-allowed; }
    .bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    button {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 9px 13px;
      font: inherit;
      cursor: pointer;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    @media (max-width: 560px) {
      main { width: min(100vw - 24px, 760px); padding: 24px 0; }
      .status { grid-template-columns: 1fr; }
      textarea { min-height: 52vh; font-size: 17px; }
    }
  </style>
</head>
<body>
  <main>
    <h1>VInput Remote</h1>
    <div class="sub">Local network text input</div>
    <section class="status" role="button" tabindex="0" title="Click to reconnect if disconnected">
      <div class="tile">
        <div class="label">Phone input</div>
        <div class="value"><span id="input-dot" class="dot"></span><span id="input-status">connecting</span></div>
      </div>
      <div class="tile">
        <div class="label">VInput session</div>
        <div class="value"><span id="output-dot" class="dot"></span><span id="output-status">ready</span></div>
      </div>
    </section>
    <textarea id="editor" spellcheck="false" enterkeyhint="send" disabled placeholder="Type or use mobile voice input here..."></textarea>
    <div class="bar">
      <span><span id="count">0</span> chars</span>
    </div>
  </main>
  <script>
    const editor = document.getElementById('editor')
    const count = document.getElementById('count')
    const inputStatus = document.getElementById('input-status')
    const outputStatus = document.getElementById('output-status')
    const inputDot = document.getElementById('input-dot')
    const outputDot = document.getElementById('output-dot')
    let ws = null
    let composing = false
    let outputConnected = false
    let authenticated = false
    let apiKey = ''
    let terminalError = ''

    function readApiKey() {
      const params = new URLSearchParams(location.hash.replace(/^#/, ''))
      const hashKey = params.get('key') || ''
      if (hashKey) {
        localStorage.setItem('vinput_remote_api_key', hashKey)
        history.replaceState(null, '', location.pathname)
        return hashKey
      }
      const stored = localStorage.getItem('vinput_remote_api_key') || ''
      if (stored) return stored
      const entered = window.prompt('API key') || ''
      if (entered) localStorage.setItem('vinput_remote_api_key', entered)
      return entered
    }

    function setInputStatus(state, detail = '') {
      if (terminalError && state === 'disconnected' && !detail) {
        return
      }
      inputStatus.textContent = detail ? `${state} (${detail})` : state
      inputDot.classList.toggle('on', state === 'connected')
      updateEnabled()
    }
    function setOutputStatus(state) {
      outputConnected = state === 'connected'
      outputStatus.textContent = state
      outputDot.classList.toggle('on', outputConnected)
    }
    function updateEnabled() {
      const enabled = ws && ws.readyState === WebSocket.OPEN && authenticated
      editor.disabled = !enabled
      if (enabled && document.activeElement !== editor) {
        editor.focus()
      }
    }
    function send(payload) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload))
      }
    }
    function commitAndClear() {
      const text = editor.value
      if (text.length === 0) {
        return
      }
      send({ type: 'text_update', text: text })
      send({ type: 'finalize' })
      editor.value = ''
      count.textContent = '0'
      editor.focus()
    }
    const messageHandlers = {
      auth_ok: () => {
        authenticated = true
        terminalError = ''
        setInputStatus('connected')
        updateEnabled()
      },
      init: msg => setOutputStatus(msg.output_status || 'ready'),
      output_connected: () => setOutputStatus('connected'),
      output_disconnected: () => setOutputStatus('ready'),
      committed: () => {
        editor.value = ''
        count.textContent = '0'
      },
      displaced: msg => {
        authenticated = false
        terminalError = 'displaced'
        setInputStatus('disconnected', msg && msg.message ? msg.message : 'Replaced by new session')
        updateEnabled()
      },
      busy: () => {
        authenticated = false
        terminalError = 'busy'
        setInputStatus('busy (occupied, retrying...)')
        updateEnabled()
      },
      error: msg => {
        authenticated = false
        terminalError = 'error'
        localStorage.removeItem('vinput_remote_api_key')
        apiKey = ''
        setInputStatus(msg && msg.message ? `failed: ${msg.message}` : 'failed')
        updateEnabled()
      }
    }

    function connect() {
      if (terminalError === 'error') {
        return
      }
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(proto + '//' + location.host + '/ws')
      ws = socket
      if (!terminalError) {
        setInputStatus('connecting')
      }
      socket.onopen = () => {
        if (ws !== socket) return
        apiKey = apiKey || readApiKey()
        if (!apiKey) {
          terminalError = 'error'
          setInputStatus('failed: API key required')
          socket.close()
          return
        }
        send({ type: 'auth', api_key: apiKey })
      }
      socket.onmessage = event => {
        if (ws !== socket) return
        try {
          const msg = JSON.parse(event.data)
          messageHandlers[msg?.type]?.(msg)
        } catch {
          // Ignore invalid JSON payloads
        }
      }
      socket.onclose = () => {
        if (ws !== socket) return
        authenticated = false
        if (!terminalError) {
          setInputStatus('disconnected')
        }
        setOutputStatus('ready')
        updateEnabled()
        if (terminalError !== 'error' && terminalError !== 'displaced') {
          setTimeout(connect, terminalError === 'busy' ? 2000 : 1000)
        }
      }
      socket.onerror = () => {
        if (ws !== socket) return
        socket.close()
      }
    }
    editor.addEventListener('compositionstart', () => { composing = true })
    editor.addEventListener('compositionend', () => {
      composing = false
      count.textContent = String(editor.value.length)
      send({ type: 'text_update', text: editor.value })
    })
    editor.addEventListener('input', () => {
      count.textContent = String(editor.value.length)
      if (!composing) send({ type: 'text_update', text: editor.value })
    })
    editor.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        commitAndClear()
      }
    })
    const statusSection = document.querySelector('.status')
    function triggerReconnect() {
      if (terminalError === 'displaced') {
        terminalError = ''
        connect()
      }
    }
    statusSection.addEventListener('click', triggerReconnect)
    statusSection.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        triggerReconnect()
      }
    })
    connect()
  </script>
</body>
</html>
"""

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#1f9d87"/><path d="M32 12c-5 0-9 4-9 9v16c0 5 4 9 9 9s9-4 9-9V21c0-5-4-9-9-9z" fill="#fff"/><path d="M17 33c0 8 7 15 15 15s15-7 15-15" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round"/><path d="M32 48v8" stroke="#fff" stroke-width="5" stroke-linecap="round"/></svg>"""


class WebSocketConnection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False

    def send_text(self, text: str) -> None:
        with self._lock:
            if self.closed:
                return
            payload = text.encode("utf-8")
            length = len(payload)
            header = bytearray([0x81])  # FIN + Text
            if length < 126:
                header.append(length)
            elif length < 65536:
                header.append(126)
                header.extend(struct.pack("!H", length))
            else:
                header.append(127)
                header.extend(struct.pack("!Q", length))
            try:
                self.sock.sendall(header + payload)
            except OSError:
                self.closed = True

    def send_json(self, data: dict[str, Any]) -> None:
        self.send_text(json.dumps(data, ensure_ascii=False))

    def recv_frame(self) -> str | None:
        try:
            head = self._recv_exact(2)
            if not head:
                return None
            first, second = head
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F

            if opcode == 0x8:  # Close
                self.closed = True
                return None

            if length == 126:
                raw_len = self._recv_exact(2)
                if not raw_len:
                    return None
                length = struct.unpack("!H", raw_len)[0]
            elif length == 127:
                raw_len = self._recv_exact(8)
                if not raw_len:
                    return None
                length = struct.unpack("!Q", raw_len)[0]

            mask_key = b""
            if masked:
                mask_key = self._recv_exact(4) or b""
                if len(mask_key) < 4:
                    return None

            payload = self._recv_exact(length)
            if payload is None:
                return None

            if masked:
                unmasked = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
            else:
                unmasked = payload

            if opcode == 0x9:  # Ping -> reply Pong
                with self._lock:
                    self.sock.sendall(bytearray([0x8A, 0x00]))
                return ""

            if opcode == 0x1:  # Text
                return unmasked.decode("utf-8", errors="replace")

            return ""
        except (OSError, ValueError):
            self.closed = True
            return None

    def _recv_exact(self, count: int) -> bytes | None:
        data = bytearray()
        while len(data) < count:
            try:
                chunk = self.sock.recv(count - len(data))
                if not chunk:
                    return None
                data.extend(chunk)
            except OSError:
                return None
        return bytes(data)

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self.closed = True
                try:
                    self.sock.sendall(bytearray([0x88, 0x00]))
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.sock.close()
                except OSError:
                    pass


class TriggerServer:
    def __init__(self, api_key: str, port: int, debounce_ms: int) -> None:
        self.api_key = api_key
        self.port = port
        self.debounce_sec = debounce_ms / 1000.0

        self.stdout_lock = threading.Lock()
        self.active_ws_lock = threading.Lock()
        self.active_ws: WebSocketConnection | None = None

        self.text_lock = threading.Lock()
        self.pending_text = ""
        self.debounce_timer: threading.Timer | None = None

    def emit_command(self, cmd: dict[str, Any]) -> None:
        with self.stdout_lock:
            sys.stdout.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def emit_notification(self, title: str, body: str) -> None:
        sys.stderr.write(json.dumps({"title": title, "body": body}, ensure_ascii=False) + "\n")
        sys.stderr.flush()

    def handle_text_update(self, text: str) -> None:
        with self.text_lock:
            self.pending_text = text
            if self.debounce_timer is not None:
                self.debounce_timer.cancel()
            self.debounce_timer = threading.Timer(self.debounce_sec, self.finalize_pending_text)
            self.debounce_timer.daemon = True
            self.debounce_timer.start()

        self.emit_command({"type": "inject", "stage": "partial", "text": text, "ime": True})

    def finalize_pending_text(self) -> None:
        final_text = ""
        with self.text_lock:
            if self.debounce_timer is not None:
                self.debounce_timer.cancel()
                self.debounce_timer = None
            final_text = self.pending_text
            self.pending_text = ""

        if final_text:
            self.emit_command({"type": "inject", "stage": "final", "text": final_text, "ime": True})
            with self.active_ws_lock:
                if self.active_ws and not self.active_ws.closed:
                    self.active_ws.send_json({"type": "committed"})

    def on_daemon_broadcast(self, event: dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "committed":
            with self.active_ws_lock:
                if self.active_ws and not self.active_ws.closed:
                    self.active_ws.send_json({"type": "committed"})


class RemoteHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Silence all HTTP access logs on stderr
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return

        if self.path == "/favicon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(FAVICON_SVG.encode("utf-8"))
            return

        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}\n')
            return

        if self.path == "/ws":
            self.handle_websocket_upgrade()
            return

        self.send_error(404, "Not Found")

    def handle_websocket_upgrade(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        upgrade = self.headers.get("Upgrade", "").lower()
        if upgrade != "websocket" or not key:
            self.send_error(400, "Bad Request")
            return

        accept_raw = hashlib.sha1((key + WS_MAGIC).encode("utf-8")).digest()
        accept_key = base64.b64encode(accept_raw).decode("ascii")

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
        )
        self.wfile.write(response.encode("ascii"))
        self.wfile.flush()

        ws = WebSocketConnection(self.connection)
        trigger = cast(ThreadedHttpServer, self.server).trigger

        authenticated = False
        try:
            while not ws.closed:
                msg_text = ws.recv_frame()
                if msg_text is None:
                    break
                if not msg_text:
                    continue

                try:
                    data = json.loads(msg_text)
                except json.JSONDecodeError:
                    continue

                if not authenticated:
                    if data.get("type") == "auth" and secrets.compare_digest(
                        str(data.get("api_key", "")), trigger.api_key
                    ):
                        authenticated = True
                        with trigger.active_ws_lock:
                            if trigger.active_ws and trigger.active_ws != ws:
                                trigger.active_ws.send_json({"type": "displaced", "message": "New session opened"})
                                trigger.active_ws.close()
                            trigger.active_ws = ws
                        ws.send_json({"type": "auth_ok"})
                        ws.send_json({"type": "init", "output_status": "ready"})
                        trigger.emit_notification("Web Remote", "Mobile phone connected")
                    else:
                        ws.send_json({"type": "error", "message": "Unauthorized."})
                        ws.close()
                        break
                    continue

                # Authenticated session
                mtype = data.get("type")
                if mtype == "text_update":
                    trigger.handle_text_update(str(data.get("text", "")))
                elif mtype == "finalize":
                    trigger.finalize_pending_text()

        finally:
            with trigger.active_ws_lock:
                if trigger.active_ws == ws:
                    trigger.active_ws = None
            ws.close()


class ThreadedHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], trigger: TriggerServer) -> None:
        super().__init__(server_address, RemoteHttpHandler)
        self.trigger = trigger


def listen_daemon_stdin(trigger: TriggerServer) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            trigger.on_daemon_broadcast(event)
        except json.JSONDecodeError:
            pass


def main() -> None:
    api_key = os.environ.get("VINPUT_TRIGGER_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            json.dumps(
                {
                    "title": "Web Remote Error",
                    "body": "Missing required VINPUT_TRIGGER_API_KEY environment variable.",
                }
            )
            + "\n"
        )
        sys.exit(2)

    port_raw = os.environ.get("VINPUT_TRIGGER_PORT", "").strip()
    if not port_raw.isdigit() or not (1 <= int(port_raw) <= 65535):
        sys.stderr.write(
            json.dumps(
                {
                    "title": "Web Remote Error",
                    "body": "VINPUT_TRIGGER_PORT must be an integer between 1 and 65535.",
                }
            )
            + "\n"
        )
        sys.exit(2)
    port = int(port_raw)

    debounce_raw = os.environ.get("VINPUT_TRIGGER_DEBOUNCE_MS", "1500").strip()
    debounce_ms = int(debounce_raw) if debounce_raw.isdigit() else 1500

    trigger = TriggerServer(api_key, port, debounce_ms)

    # Background pump to consume stdin broadcasts from vinput-daemon
    stdin_thread = threading.Thread(target=listen_daemon_stdin, args=(trigger,), daemon=True)
    stdin_thread.start()

    try:
        httpd = ThreadedHttpServer(("0.0.0.0", port), trigger)
    except OSError as exc:
        trigger.emit_notification("Web Remote Error", f"Failed to bind port {port}: {exc}")
        sys.exit(1)

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
