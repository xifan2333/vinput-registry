#!/usr/bin/env python3
"""Mobile web remote text input trigger for vinput.

Hosts a lightweight web interface and WebSocket endpoint over local network,
translating mobile typing and speech into vinput `inject` commands on stdout.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import fcntl
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
import time
from dataclasses import dataclass
from typing import Any

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0
BUS_VIRTUAL = 0x06
UINPUT_PATH = "/dev/uinput"
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
STANDARD_KEY_MAX = 248
KEY_CODE_MAX = 0x2FF

INPUT_EVENT_STRUCT = struct.Struct("llHHi")
UINPUT_SETUP_STRUCT = struct.Struct("=HHHH80sI")

VIRTUAL_KEY_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("menu", "Menu", "VINPUT_TRIGGER_MENU_KEY", "KEY_RIGHTSHIFT"),
    ("esc", "Esc", "VINPUT_TRIGGER_ESC_KEY", "KEY_ESC"),
    ("up", "↑", "VINPUT_TRIGGER_UP_KEY", "KEY_UP"),
    ("down", "↓", "VINPUT_TRIGGER_DOWN_KEY", "KEY_DOWN"),
    ("left", "←", "VINPUT_TRIGGER_LEFT_KEY", "KEY_LEFT"),
    ("right", "→", "VINPUT_TRIGGER_RIGHT_KEY", "KEY_RIGHT"),
    ("backspace", "⌫", "VINPUT_TRIGGER_BACKSPACE_KEY", "KEY_BACKSPACE"),
    ("enter", "⏎", "VINPUT_TRIGGER_ENTER_KEY", "KEY_ENTER"),
)

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Web Remote Trigger",
        "connected": "Mobile phone connected.",
        "err_missing_api_key": "Missing required VINPUT_TRIGGER_API_KEY environment variable.",
        "err_invalid_port": "VINPUT_TRIGGER_PORT must be an integer between 1 and 65535.",
        "err_invalid_key_spec": "Invalid key binding in {env}: {spec!r}.",
        "err_unknown_key": "Unknown Linux key name {key_name!r} in {env}.",
        "err_libevdev_missing": "Cannot load libevdev ({detail}); set VINPUT_TRIGGER_LIBEVDEV_PATH or use numeric keycodes.",
        "err_uinput_permission": "Permission denied opening {path}. Add user to the 'input' or 'uinput' group.",
        "err_uinput_missing": "Virtual keyboard device {path} does not exist (ensure the uinput kernel module is loaded).",
        "err_uinput_init": "Failed to initialize virtual keyboard: {detail}",
        "err_bind": "Failed to bind port {port}: {detail}",
        "err_runtime": "Web Remote runtime error: {detail}",
    },
    "zh_CN": {
        "title": "网页远控触发器",
        "connected": "手机端已成功连接。",
        "err_missing_api_key": "缺少必需的 VINPUT_TRIGGER_API_KEY 环境变量。",
        "err_invalid_port": "VINPUT_TRIGGER_PORT 必须是 1 到 65535 之间的整数。",
        "err_invalid_key_spec": "环境变量 {env} 的按键绑定格式无效：{spec!r}。",
        "err_unknown_key": "环境变量 {env} 中存在未知的 Linux 按键名 {key_name!r}。",
        "err_libevdev_missing": "无法加载 libevdev（{detail}）；请设置 VINPUT_TRIGGER_LIBEVDEV_PATH 或改用数字键码。",
        "err_uinput_permission": "打开 {path} 权限不足，请将当前用户加入 'input' 或 'uinput' 用户组。",
        "err_uinput_missing": "虚拟键盘设备 {path} 不存在（请确认已加载 uinput 内核模块）。",
        "err_uinput_init": "初始化虚拟键盘失败：{detail}",
        "err_bind": "绑定端口 {port} 失败：{detail}",
        "err_runtime": "网页远控触发器运行异常：{detail}",
    },
}


class VInputError(Exception):
    """Base exception for structured vinput client/trigger errors."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Configuration error resulting in usage exit code."""

    exit_code = EXIT_USAGE_ERROR


def detect_locale() -> str:
    """Detect system locale from LC_ALL, LC_MESSAGES, or LANG."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.getenv(var, "").strip()
        if not val:
            continue
        base = val.split(".")[0].split("@")[0]
        if base in MESSAGES:
            return base
        prefix = base.split("_")[0]
        for key in MESSAGES:
            if key.startswith(prefix + "_"):
                return key
    return "en_US"


def get_message(key: str, **kwargs: object) -> str:
    """Retrieve localized message string with fallback chain."""
    loc = detect_locale()
    tpl = MESSAGES.get(loc, {}).get(key)
    if not tpl:
        tpl = MESSAGES.get("en_US", {}).get(key, key)
    try:
        return tpl.format(**kwargs)
    except Exception:
        return tpl


def notify_error(key: str, **kwargs: object) -> None:
    """Emit JSONL user desktop notification to stderr."""
    title = get_message("title")
    msg = get_message(key, **kwargs)
    payload = json.dumps({"title": title, "body": msg}, ensure_ascii=False)
    sys.stderr.write(f"{payload}\n")
    sys.stderr.flush()


def notify_info(key: str, **kwargs: object) -> None:
    """Emit informational notification to stderr."""
    title = get_message("title")
    msg = get_message(key, **kwargs)
    payload = json.dumps({"title": title, "body": msg}, ensure_ascii=False)
    sys.stderr.write(f"{payload}\n")
    sys.stderr.flush()


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
      width: min(680px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 14px 0 18px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }
    .brand {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.01em;
      white-space: nowrap;
    }
    .status {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 6px;
      cursor: pointer;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border: 1px solid var(--border);
      background: var(--panel);
      border-radius: 999px;
      font-size: 12px;
      line-height: 1.35;
    }
    .pill-name { color: var(--muted); }
    .pill-state {
      color: var(--text);
      font-weight: 500;
      max-width: 130px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--danger);
      flex: 0 0 auto;
    }
    .dot.on { background: var(--accent); }
    .editor-wrap {
      position: relative;
      margin-bottom: 12px;
    }
    textarea {
      display: block;
      width: 100%;
      min-height: 150px;
      height: clamp(150px, 34vh, 320px);
      resize: vertical;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--text);
      padding: 14px 14px 28px;
      font: 17px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      outline: none;
    }
    textarea:focus { border-color: var(--accent); }
    textarea:disabled { opacity: 0.58; cursor: not-allowed; }
    .count-badge {
      position: absolute;
      right: 12px;
      bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      pointer-events: none;
    }
    button {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .vkeys {
      display: flex;
      align-items: stretch;
      gap: 12px;
    }
    .vkeys[hidden] { display: none; }
    .vk-group {
      display: grid;
      gap: 6px;
      padding: 6px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--panel);
    }
    .vk-dpad {
      flex: 3 1 0;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      grid-template-rows: repeat(2, 44px);
      grid-template-areas:
        ". up ."
        "left down right";
    }
    .vk-actions {
      flex: 2.2 1 0;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      grid-template-rows: repeat(2, 44px);
      grid-template-areas:
        "menu esc"
        "backspace enter";
    }
    .vk-btn {
      width: 100%;
      height: 100%;
      padding: 0 6px;
      font-size: 14px;
      font-weight: 600;
      background: var(--panel-2);
      touch-action: manipulation;
      user-select: none;
      -webkit-user-select: none;
      transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease, transform 0.08s ease;
    }
    .vk-btn:not(:disabled):active,
    .vk-btn:not(:disabled).is-active {
      background: rgba(31, 157, 135, 0.22);
      border-color: var(--accent);
      color: #5ee6cc;
      transform: scale(0.96);
    }
    @media (max-width: 560px) {
      main { width: calc(100vw - 20px); padding: 10px 0 12px; }
      textarea { min-height: 120px; height: 27vh; font-size: 16px; padding: 12px 12px 26px; }
      .vkeys { gap: 8px; }
      .vk-group { gap: 5px; padding: 5px; }
      .vk-dpad, .vk-actions { grid-template-rows: repeat(2, 42px); }
    }
  </style>
</head>
<body>
  <main>
    <header class="topbar">
      <div class="brand">VInput</div>
      <div class="status" role="button" tabindex="0" title="Click to reconnect if disconnected">
        <span class="pill"><span id="input-dot" class="dot"></span><span class="pill-name">Phone</span><span id="input-status" class="pill-state">connecting</span></span>
        <span class="pill"><span id="output-dot" class="dot"></span><span class="pill-name">Session</span><span id="output-status" class="pill-state">ready</span></span>
      </div>
    </header>
    <div class="editor-wrap">
      <textarea id="editor" spellcheck="false" enterkeyhint="send" disabled placeholder="Type or use mobile voice input here..."></textarea>
      <span class="count-badge"><span id="count">0</span> chars</span>
    </div>
    <section id="vkeys" class="vkeys" aria-label="Virtual keys" hidden></section>
  </main>
  <script>
    const editor = document.getElementById('editor')
    const count = document.getElementById('count')
    const vkeys = document.getElementById('vkeys')
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
      for (const btn of vkeys.querySelectorAll('.vk-btn')) {
        btn.disabled = !enabled
      }
      if (enabled && document.activeElement !== editor) {
        editor.focus()
      }
    }
    function send(payload) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload))
      }
    }
    function triggerVirtualKey(id) {
      if (!ws || ws.readyState !== WebSocket.OPEN || !authenticated) {
        return
      }
      if (editor.value.length > 0 && !composing) {
        send({ type: 'text_update', text: editor.value })
      }
      send({ type: 'virtual_key', id: id })
    }
    function createVirtualButton(item, enabled) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'vk-btn'
      btn.textContent = item.label
      btn.dataset.keyId = item.id
      btn.style.gridArea = item.id
      btn.disabled = !enabled
      let activePointerId = null
      let releaseTimer = null
      function flashActive() {
        if (releaseTimer !== null) {
          clearTimeout(releaseTimer)
        }
        btn.classList.add('is-active')
        releaseTimer = setTimeout(() => {
          btn.classList.remove('is-active')
          releaseTimer = null
        }, 140)
      }
      btn.addEventListener('pointerdown', e => {
        e.preventDefault()
        if (!btn.disabled && e.isPrimary && (e.pointerType !== 'mouse' || e.button === 0)) {
          activePointerId = e.pointerId
          if (releaseTimer !== null) {
            clearTimeout(releaseTimer)
            releaseTimer = null
          }
          btn.classList.add('is-active')
        }
      })
      btn.addEventListener('mousedown', e => e.preventDefault())
      btn.addEventListener('touchstart', e => e.preventDefault(), { passive: false })
      btn.addEventListener('pointerup', e => {
        if (activePointerId !== e.pointerId) return
        activePointerId = null
        if (!btn.disabled) {
          flashActive()
          triggerVirtualKey(item.id)
        } else {
          btn.classList.remove('is-active')
        }
      })
      btn.addEventListener('pointercancel', () => {
        activePointerId = null
        if (releaseTimer !== null) {
          clearTimeout(releaseTimer)
          releaseTimer = null
        }
        btn.classList.remove('is-active')
      })
      btn.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          if (!btn.disabled) {
            flashActive()
            triggerVirtualKey(item.id)
          }
        }
      })
      return btn
    }
    function renderVirtualKeys(items) {
      vkeys.innerHTML = ''
      if (!Array.isArray(items) || items.length === 0) {
        vkeys.hidden = true
        return
      }
      const dpadIds = new Set(['up', 'down', 'left', 'right'])
      const dpadItems = []
      const actionItems = []
      for (const item of items) {
        if (!item || !item.id || !item.label) continue
        if (dpadIds.has(item.id)) {
          dpadItems.push(item)
        } else {
          actionItems.push(item)
        }
      }
      const enabled = ws && ws.readyState === WebSocket.OPEN && authenticated
      if (dpadItems.length > 0) {
        const dpad = document.createElement('div')
        dpad.className = 'vk-group vk-dpad'
        for (const item of dpadItems) {
          dpad.appendChild(createVirtualButton(item, enabled))
        }
        vkeys.appendChild(dpad)
      }
      if (actionItems.length > 0) {
        const actions = document.createElement('div')
        actions.className = 'vk-group vk-actions'
        for (const item of actionItems) {
          actions.appendChild(createVirtualButton(item, enabled))
        }
        vkeys.appendChild(actions)
      }
      vkeys.hidden = vkeys.children.length === 0
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
      init: msg => {
        setOutputStatus(msg.output_status || 'ready')
        if (Array.isArray(msg.virtual_keys)) {
          renderVirtualKeys(msg.virtual_keys)
        }
      },
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

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="12" fill="#1f9d87"/>'
    '<path d="M32 12c-5 0-9 4-9 9v16c0 5 4 9 9 9s9-4 9-9V21c0-5-4-9-9-9z" fill="#fff"/>'
    '<path d="M17 33c0 8 7 15 15 15s15-7 15-15" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round"/>'
    '<path d="M32 48v8" stroke="#fff" stroke-width="5" stroke-linecap="round"/>'
    "</svg>"
)


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


# Protocol command emitting
_STDOUT_LOCK = threading.Lock()
_ACTIVE_WS_LOCK = threading.Lock()
_ACTIVE_WS: WebSocketConnection | None = None
_TEXT_LOCK = threading.Lock()
_PENDING_TEXT = ""
_DEBOUNCE_TIMER: threading.Timer | None = None
_DEBOUNCE_SEC = 1.5
_API_KEY = ""
_VIRTUAL_KEY_LIST: tuple[VirtualKeyBinding, ...] = ()
_VIRTUAL_KEY_MAP: dict[str, VirtualKeyBinding] = {}
_UINPUT_LOCK = threading.Lock()
_KEY_RESOLVER: LibEvdevResolver | None = None
_UINPUT_KEYBOARD: UInputKeyboard | None = None
_RESOLVED_CHORDS: dict[str, tuple[int, ...]] = {}


@dataclass(frozen=True)
class VirtualKeyBinding:
    action_id: str
    label: str
    env_name: str
    tokens: tuple[str, ...]


def _parse_numeric_keycode(token: str) -> int | None:
    stripped = token.strip()
    if not stripped:
        return None
    if stripped.isdigit() or stripped.lower().startswith("0x"):
        try:
            code = int(stripped, 0)
        except ValueError:
            return None
        if 0 <= code <= KEY_CODE_MAX:
            return code
    return None


def _parse_key_tokens(spec: str, env_name: str) -> tuple[str, ...]:
    parts = [part.strip() for part in spec.split("+")]
    if not parts or any(not part for part in parts):
        raise ConfigError("err_invalid_key_spec", env=env_name, spec=spec)
    return tuple(parts)


class LibEvdevResolver:
    """Resolve Linux UAPI EV_KEY names using system libevdev via ctypes."""

    def __init__(self, custom_path: str = "") -> None:
        resolved_custom = custom_path.strip() or os.environ.get("VINPUT_TRIGGER_LIBEVDEV_PATH", "").strip()
        loaded_lib = self._load_library(resolved_custom)
        loaded_lib.libevdev_event_code_from_name.argtypes = [ctypes.c_uint, ctypes.c_char_p]
        loaded_lib.libevdev_event_code_from_name.restype = ctypes.c_int
        self._lib = loaded_lib

    def _load_library(self, custom_path: str) -> ctypes.CDLL:
        if custom_path:
            try:
                return ctypes.CDLL(custom_path)
            except OSError as exc:
                raise VInputError("err_libevdev_missing", detail=str(exc)) from exc

        candidates: list[str] = []
        discovered = ctypes.util.find_library("evdev")
        if discovered:
            candidates.append(discovered)
        candidates.append("libevdev.so.2")

        last_err: OSError | None = None
        for candidate in candidates:
            try:
                return ctypes.CDLL(candidate)
            except OSError as exc:
                last_err = exc
        raise VInputError("err_libevdev_missing", detail=str(last_err or "libevdev.so.2 not found"))

    def resolve_token(self, token: str, env_name: str) -> int:
        numeric = _parse_numeric_keycode(token)
        if numeric is not None:
            return numeric
        try:
            raw_name = token.strip().upper().encode("ascii")
        except UnicodeEncodeError as exc:
            raise ConfigError("err_unknown_key", env=env_name, key_name=token) from exc
        code = int(self._lib.libevdev_event_code_from_name(EV_KEY, raw_name))
        if code < 0:
            raise ConfigError("err_unknown_key", env=env_name, key_name=token)
        return code


class UInputKeyboard:
    """Minimal Linux /dev/uinput virtual keyboard emitter."""

    def __init__(self, extra_codes: set[int] | None = None) -> None:
        self.fd = self._open_device()
        try:
            self._configure_device(extra_codes or set())
        except VInputError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise VInputError("err_uinput_init", detail=str(exc)) from exc

    def _open_device(self) -> int:
        try:
            return os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise VInputError("err_uinput_permission", path=UINPUT_PATH) from exc
        except FileNotFoundError as exc:
            raise VInputError("err_uinput_missing", path=UINPUT_PATH) from exc
        except OSError as exc:
            raise VInputError("err_uinput_init", detail=str(exc)) from exc

    def _configure_device(self, extra_codes: set[int]) -> None:
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for code in range(1, STANDARD_KEY_MAX + 1):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in sorted(extra_codes):
            if STANDARD_KEY_MAX < code <= KEY_CODE_MAX:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)

        setup_bytes = UINPUT_SETUP_STRUCT.pack(
            BUS_VIRTUAL,
            0x1234,
            0x5678,
            1,
            b"vinput-web-remote",
            0,
        )
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup_bytes)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.05)

    def emit_chord(self, codes: tuple[int, ...]) -> None:
        if self.fd < 0 or not codes:
            return
        events = bytearray()
        for code in codes:
            events.extend(INPUT_EVENT_STRUCT.pack(0, 0, EV_KEY, code, 1))
            events.extend(INPUT_EVENT_STRUCT.pack(0, 0, EV_SYN, SYN_REPORT, 0))
        for code in reversed(codes):
            events.extend(INPUT_EVENT_STRUCT.pack(0, 0, EV_KEY, code, 0))
            events.extend(INPUT_EVENT_STRUCT.pack(0, 0, EV_SYN, SYN_REPORT, 0))
        try:
            os.write(self.fd, bytes(events))
        except OSError as exc:
            raise VInputError("err_uinput_init", detail=str(exc)) from exc

    def close(self) -> None:
        if self.fd < 0:
            return
        fd = self.fd
        self.fd = -1
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError as exc:
            notify_error("err_uinput_init", detail=str(exc))
        try:
            os.close(fd)
        except OSError as exc:
            notify_error("err_uinput_init", detail=str(exc))


def _resolve_binding_codes(binding: VirtualKeyBinding) -> tuple[int, ...]:
    global _KEY_RESOLVER
    numeric_only = [_parse_numeric_keycode(tok) for tok in binding.tokens]
    if all(code is not None for code in numeric_only):
        return tuple(code for code in numeric_only if code is not None)
    if _KEY_RESOLVER is None:
        _KEY_RESOLVER = LibEvdevResolver()
    return tuple(_KEY_RESOLVER.resolve_token(tok, binding.env_name) for tok in binding.tokens)


def _collect_extra_key_codes() -> set[int]:
    extra: set[int] = set()
    for binding in _VIRTUAL_KEY_LIST:
        if binding.action_id in _RESOLVED_CHORDS:
            extra.update(_RESOLVED_CHORDS[binding.action_id])
            continue
        try:
            codes = _resolve_binding_codes(binding)
        except VInputError:
            continue
        _RESOLVED_CHORDS[binding.action_id] = codes
        extra.update(codes)
    return extra


def trigger_virtual_key(action_id: str) -> bool:
    """Resolve and emit the configured Linux key chord for a virtual button."""
    global _UINPUT_KEYBOARD
    binding = _VIRTUAL_KEY_MAP.get(action_id)
    if binding is None:
        return False
    try:
        with _UINPUT_LOCK:
            codes = _RESOLVED_CHORDS.get(action_id)
            if codes is None:
                codes = _resolve_binding_codes(binding)
                _RESOLVED_CHORDS[action_id] = codes
            if _UINPUT_KEYBOARD is None:
                extra_codes = _collect_extra_key_codes()
                extra_codes.update(codes)
                _UINPUT_KEYBOARD = UInputKeyboard(extra_codes)
            _UINPUT_KEYBOARD.emit_chord(codes)
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return False
    return True


def close_virtual_keyboard() -> None:
    """Close and destroy the lazy uinput device if initialized."""
    global _UINPUT_KEYBOARD
    with _UINPUT_LOCK:
        if _UINPUT_KEYBOARD is not None:
            _UINPUT_KEYBOARD.close()
            _UINPUT_KEYBOARD = None


def send_command(cmd: dict[str, Any]) -> None:
    """Emit JSON command to vinput daemon over stdout."""
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def on_daemon_broadcast(event: dict[str, Any]) -> None:
    """Handle daemon broadcast events received from stdin."""
    typ = event.get("type")
    if typ == "committed":
        with _ACTIVE_WS_LOCK:
            if _ACTIVE_WS and not _ACTIVE_WS.closed:
                _ACTIVE_WS.send_json({"type": "committed"})


def listen_daemon_broadcasts() -> None:
    """Background reader consuming JSONL events from stdin."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                on_daemon_broadcast(event)
        except json.JSONDecodeError:
            pass


def handle_text_update(text: str) -> None:
    global _PENDING_TEXT, _DEBOUNCE_TIMER
    with _TEXT_LOCK:
        _PENDING_TEXT = text
        if _DEBOUNCE_TIMER is not None:
            _DEBOUNCE_TIMER.cancel()
        _DEBOUNCE_TIMER = threading.Timer(_DEBOUNCE_SEC, finalize_pending_text)
        _DEBOUNCE_TIMER.daemon = True
        _DEBOUNCE_TIMER.start()

    send_command({"type": "inject", "stage": "partial", "text": text, "ime": True})


def finalize_pending_text() -> None:
    global _PENDING_TEXT, _DEBOUNCE_TIMER
    final_text = ""
    with _TEXT_LOCK:
        if _DEBOUNCE_TIMER is not None:
            _DEBOUNCE_TIMER.cancel()
            _DEBOUNCE_TIMER = None
        final_text = _PENDING_TEXT
        _PENDING_TEXT = ""

    if final_text:
        send_command({"type": "inject", "stage": "final", "text": final_text, "ime": True})
        with _ACTIVE_WS_LOCK:
            if _ACTIVE_WS and not _ACTIVE_WS.closed:
                _ACTIVE_WS.send_json({"type": "committed"})


class RemoteHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
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
        global _ACTIVE_WS
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
                    if data.get("type") == "auth" and secrets.compare_digest(str(data.get("api_key", "")), _API_KEY):
                        authenticated = True
                        with _ACTIVE_WS_LOCK:
                            if _ACTIVE_WS and _ACTIVE_WS != ws:
                                _ACTIVE_WS.send_json({"type": "displaced", "message": "New session opened"})
                                _ACTIVE_WS.close()
                            _ACTIVE_WS = ws
                        ws.send_json({"type": "auth_ok"})
                        ws.send_json(
                            {
                                "type": "init",
                                "output_status": "ready",
                                "virtual_keys": [{"id": k.action_id, "label": k.label} for k in _VIRTUAL_KEY_LIST],
                            }
                        )
                        notify_info("connected")
                    else:
                        ws.send_json({"type": "error", "message": "Unauthorized."})
                        ws.close()
                        break
                    continue

                mtype = data.get("type")
                if mtype == "text_update":
                    handle_text_update(str(data.get("text", "")))
                elif mtype == "finalize":
                    finalize_pending_text()
                elif mtype == "virtual_key":
                    finalize_pending_text()
                    trigger_virtual_key(str(data.get("id", "")))

        finally:
            with _ACTIVE_WS_LOCK:
                if _ACTIVE_WS == ws:
                    _ACTIVE_WS = None
            ws.close()


class ThreadedHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@dataclass(frozen=True)
class RemoteConfig:
    api_key: str
    port: int
    debounce_ms: int
    virtual_keys: tuple[VirtualKeyBinding, ...]


def load_virtual_keys() -> tuple[VirtualKeyBinding, ...]:
    bindings: list[VirtualKeyBinding] = []
    for action_id, label, env_name, default_spec in VIRTUAL_KEY_SPECS:
        raw = os.environ.get(env_name, "").strip()
        if raw.lower() == "none":
            continue
        spec = raw or default_spec
        tokens = _parse_key_tokens(spec, env_name)
        bindings.append(VirtualKeyBinding(action_id=action_id, label=label, env_name=env_name, tokens=tokens))
    return tuple(bindings)


def load_config() -> RemoteConfig:
    api_key = os.environ.get("VINPUT_TRIGGER_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("err_missing_api_key")

    port_raw = os.environ.get("VINPUT_TRIGGER_PORT", "").strip()
    if not port_raw.isdigit() or not (1 <= int(port_raw) <= 65535):
        raise ConfigError("err_invalid_port")
    port = int(port_raw)

    debounce_raw = os.environ.get("VINPUT_TRIGGER_DEBOUNCE_MS", "1500").strip()
    debounce_ms = int(debounce_raw) if debounce_raw.isdigit() else 1500

    virtual_keys = load_virtual_keys()

    return RemoteConfig(api_key=api_key, port=port, debounce_ms=debounce_ms, virtual_keys=virtual_keys)


def run() -> int:
    global _API_KEY, _DEBOUNCE_SEC, _VIRTUAL_KEY_LIST, _VIRTUAL_KEY_MAP
    config = load_config()

    _API_KEY = config.api_key
    _DEBOUNCE_SEC = config.debounce_ms / 1000.0
    _VIRTUAL_KEY_LIST = config.virtual_keys
    _VIRTUAL_KEY_MAP = {binding.action_id: binding for binding in config.virtual_keys}
    _RESOLVED_CHORDS.clear()

    stdin_thread = threading.Thread(target=listen_daemon_broadcasts, daemon=True)
    stdin_thread.start()

    try:
        httpd = ThreadedHttpServer(("0.0.0.0", config.port), RemoteHttpHandler)
    except OSError as exc:
        notify_error("err_bind", port=config.port, detail=str(exc))
        return EXIT_RUNTIME_ERROR

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        close_virtual_keyboard()
        httpd.server_close()

    return EXIT_SUCCESS


def main() -> int:
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_runtime", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
