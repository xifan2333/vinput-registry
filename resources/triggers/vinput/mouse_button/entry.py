#!/usr/bin/env python3
"""Mouse button trigger for vinput using libudev discovery and evdev ioctl.

Primary discovery and hotplug use libudev (`udev` netlink socket + `ID_INPUT_MOUSE`),
with `fcntl.ioctl` capability verification (`EVIOCGID`, `EVIOCGPROP`, `EVIOCGBIT`)
and an `inotify` fallback when `libudev.so.1` is unavailable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import glob
import json
import os
import selectors
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

EV_KEY = 0x01
KEY_MAX = 0x2FF
BUS_VIRTUAL = 0x06

INPUT_PROP_DIRECT = 0x01
INPUT_PROP_BUTTONPAD = 0x02

TAP_THRESHOLD_SEC = 0.200
VALID_MODES = frozenset({"both", "hold", "tap"})

INPUT_EVENT_STRUCT = struct.Struct("llHHi")
INOTIFY_HEADER_STRUCT = struct.Struct("iIII")

IN_ATTRIB = 0x00000004
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000

IOC_READ = 2


def _ior(nr: int, size: int) -> int:
    return (IOC_READ << 30) | (size << 16) | (ord("E") << 8) | nr


EVIOCGID = _ior(0x02, 8)


def _eviocgprop(length: int) -> int:
    return _ior(0x09, length)


def _eviocgbit(ev_type: int, length: int) -> int:
    return _ior(0x20 + ev_type, length)


BUILTIN_KEY_CODES: dict[str, int] = {
    "BTN_MOUSE": 0x110,
    "BTN_LEFT": 0x110,
    "BTN_RIGHT": 0x111,
    "BTN_MIDDLE": 0x112,
    "BTN_SIDE": 0x113,
    "BTN_EXTRA": 0x114,
    "BTN_FORWARD": 0x115,
    "BTN_BACK": 0x116,
    "BTN_TASK": 0x117,
}

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Mouse Button Trigger",
        "missing_env": "Missing required environment variable '{name}'.",
        "invalid_mode": "VINPUT_TRIGGER_MODE must be one of: both, hold, tap (got {mode!r}).",
        "invalid_button": "Invalid VINPUT_TRIGGER_BUTTON value {value!r}.",
        "unknown_button": "Unknown Linux button/key name {name!r} in VINPUT_TRIGGER_BUTTON.",
        "library_load_failed": "Failed to load {env}={path!r}: {detail}",
        "permission_denied": "Permission denied opening {path}. Install 70-fcitx5-vinput.rules or add user to the 'input' group.",
        "unexpected_error": "Mouse button trigger error: {error}",
    },
    "zh_CN": {
        "title": "鼠标按键触发器",
        "missing_env": "缺少必需的环境变量 '{name}'。",
        "invalid_mode": "VINPUT_TRIGGER_MODE 必须是 both、hold 或 tap（当前为 {mode!r}）。",
        "invalid_button": "VINPUT_TRIGGER_BUTTON 配置无效：{value!r}。",
        "unknown_button": "VINPUT_TRIGGER_BUTTON 中存在未知的 Linux 按键名：{name!r}。",
        "library_load_failed": "加载 {env}={path!r} 失败：{detail}",
        "permission_denied": "无法读取鼠标设备 {path}（权限不足）。请安装 70-fcitx5-vinput.rules 或将用户加入 'input' 组。",
        "unexpected_error": "鼠标按键触发器异常：{error}",
    },
}

FALLBACK_LOCALE = "en_US"


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
    return FALLBACK_LOCALE


def get_message(key: str, **kwargs: object) -> str:
    """Retrieve and format localized string with fallback."""
    loc = detect_locale()
    template = MESSAGES.get(loc, {}).get(key) or MESSAGES[FALLBACK_LOCALE].get(key, key)
    return template.format(**kwargs) if kwargs else template


def notify_error(key: str, **kwargs: object) -> None:
    """Write a single-line JSONL desktop notification to stderr and flush."""
    payload = {"title": get_message("title"), "body": get_message(key, **kwargs)}
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


class VInputError(Exception):
    """Base exception carrying exit code and localized message key."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, exit_code: int | None = None, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(VInputError):
    """Raised when configuration or setup prevents startup."""

    exit_code = EXIT_USAGE_ERROR


@dataclass(frozen=True)
class ClientConfig:
    """Validated runtime configuration for the mouse button trigger."""

    button_codes: frozenset[int]
    mode: str
    device_path: str
    capture_device: str


def _load_shared_library(
    env_var: str,
    lib_name: str,
    sonames: tuple[str, ...],
    *,
    use_errno: bool = False,
) -> ctypes.CDLL:
    """Load a shared library honoring an explicit override env var before system lookup."""
    custom_path = os.getenv(env_var, "").strip() if env_var else ""
    if custom_path:
        try:
            return ctypes.CDLL(custom_path, use_errno=use_errno)
        except OSError as exc:
            raise ConfigError("library_load_failed", env=env_var, path=custom_path, detail=str(exc)) from exc

    candidates: list[str] = []
    discovered = ctypes.util.find_library(lib_name)
    if discovered:
        candidates.append(discovered)
    for soname in sonames:
        if soname not in candidates:
            candidates.append(soname)

    last_err: OSError | None = None
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate, use_errno=use_errno)
        except OSError as exc:
            last_err = exc
    raise last_err or OSError(f"Cannot load {lib_name}")


def _load_libevdev() -> Any | None:
    try:
        lib = _load_shared_library("VINPUT_TRIGGER_LIBEVDEV_PATH", "evdev", ("libevdev.so.2", "libevdev.so"))
        lib.libevdev_event_code_from_name.argtypes = [ctypes.c_uint, ctypes.c_char_p]
        lib.libevdev_event_code_from_name.restype = ctypes.c_int
    except OSError:
        return None
    return lib


def resolve_button_code(token: str, libevdev: Any | None = None) -> int:
    """Resolve a button name (e.g. BTN_SIDE) or integer code into a Linux EV_KEY code."""
    cleaned = token.strip()
    if not cleaned:
        raise ConfigError("invalid_button", value=token)

    try:
        code = int(cleaned, 0)
    except ValueError:
        code = -1

    if code >= 0:
        if code > KEY_MAX:
            raise ConfigError("invalid_button", value=token)
        return code

    upper = cleaned.upper()
    if upper in BUILTIN_KEY_CODES:
        return BUILTIN_KEY_CODES[upper]

    if libevdev is not None:
        resolved = int(libevdev.libevdev_event_code_from_name(EV_KEY, upper.encode("ascii", errors="ignore")))
        if resolved >= 0:
            return resolved

    raise ConfigError("unknown_button", name=token)


def parse_button_codes(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Linux button names or numeric keycodes."""
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens:
        raise ConfigError("invalid_button", value=raw)

    libevdev: Any | None = None
    codes: set[int] = set()
    for token in tokens:
        upper = token.upper()
        if upper not in BUILTIN_KEY_CODES and not token.lstrip("+-").isdigit() and not upper.startswith("0X"):
            if libevdev is None:
                libevdev = _load_libevdev()
        codes.add(resolve_button_code(token, libevdev=libevdev))
    return frozenset(codes)


def load_config() -> ClientConfig:
    """Parse and validate trigger configuration from environment variables."""
    raw_button = os.getenv("VINPUT_TRIGGER_BUTTON", "BTN_SIDE").strip() or "BTN_SIDE"
    button_codes = parse_button_codes(raw_button)

    mode = (os.getenv("VINPUT_TRIGGER_MODE", "both").strip() or "both").lower()
    if mode not in VALID_MODES:
        raise ConfigError("invalid_mode", mode=mode)

    device_path = os.getenv("VINPUT_TRIGGER_DEVICE_PATH", "auto").strip() or "auto"
    capture_device = os.getenv("VINPUT_TRIGGER_CAPTURE_DEVICE", "").strip()
    return ClientConfig(
        button_codes=button_codes,
        mode=mode,
        device_path=device_path,
        capture_device=capture_device,
    )


def send_command(cmd: dict[str, Any]) -> None:
    """Write a single-line JSONL command to stdout and flush."""
    sys.stdout.write(json.dumps(cmd, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_start(ime: bool = True, capture_device: str = "") -> None:
    """Send 'start' command to initiate recording session."""
    payload: dict[str, Any] = {"type": "start", "ime": ime, "mode": "normal"}
    if capture_device:
        payload["capture_device"] = capture_device
    send_command(payload)


def send_stop(ime: bool = True) -> None:
    """Send 'stop' command to finish recording."""
    send_command({"type": "stop", "ime": ime})


def _test_bit(bit: int, buf: bytes | bytearray) -> bool:
    byte_index = bit // 8
    if byte_index >= len(buf):
        return False
    return bool((buf[byte_index] >> (bit % 8)) & 1)


def _has_matching_key_bits(fd: int, button_codes: frozenset[int]) -> bool:
    key_buf = bytearray((KEY_MAX // 8) + 1)
    fcntl.ioctl(fd, _eviocgbit(EV_KEY, len(key_buf)), key_buf, True)
    return any(_test_bit(code, key_buf) for code in button_codes)


def _is_touchpad_or_direct_prop(fd: int) -> bool:
    prop_buf = bytearray(4)
    fcntl.ioctl(fd, _eviocgprop(len(prop_buf)), prop_buf, True)
    return _test_bit(INPUT_PROP_DIRECT, prop_buf) or _test_bit(INPUT_PROP_BUTTONPAD, prop_buf)


def probe_evdev_fd(fd: int, button_codes: frozenset[int], check_pointer_props: bool) -> bool:
    """Validate an open evdev file descriptor via ioctl."""
    id_buf = bytearray(8)
    fcntl.ioctl(fd, EVIOCGID, id_buf, True)
    bustype, _vid, _pid, _ver = struct.unpack("HHHH", id_buf)
    if bustype == BUS_VIRTUAL:
        return False

    if check_pointer_props and _is_touchpad_or_direct_prop(fd):
        return False

    return _has_matching_key_bits(fd, button_codes)


def open_matching_evdev(path: str, button_codes: frozenset[int], check_pointer_props: bool) -> int | None:
    """Open an evdev node and return its fd if it passes capability checks."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if probe_evdev_fd(fd, button_codes, check_pointer_props=check_pointer_props):
            return fd
    except OSError:
        os.close(fd)
        return None
    os.close(fd)
    return None


class DeviceWatcher(Protocol):
    """Protocol for pluggable input device discovery and hotplug backends."""

    @property
    def fd(self) -> int: ...

    @property
    def check_pointer_props(self) -> bool: ...

    def scan_candidates(self) -> list[str]: ...

    def read_changes(self) -> tuple[list[str], list[str]]: ...

    def close(self) -> None: ...


class UdevDeviceWatcher:
    """Primary device discovery and hotplug watcher backed by libudev.so.1."""

    def __init__(self, device_path: str) -> None:
        self._device_path = device_path
        self._lib = self._load_libudev()
        self._ctx = self._lib.udev_new()
        if not self._ctx:
            raise OSError("udev_new failed")

        self._mon = self._lib.udev_monitor_new_from_netlink(self._ctx, b"udev")
        if not self._mon:
            self._lib.udev_unref(self._ctx)
            raise OSError("udev_monitor_new_from_netlink failed")

        if self._lib.udev_monitor_filter_add_match_subsystem_devtype(self._mon, b"input", None) < 0:
            self.close()
            raise OSError("udev_monitor_filter_add_match_subsystem_devtype failed")

        if self._lib.udev_monitor_enable_receiving(self._mon) < 0:
            self.close()
            raise OSError("udev_monitor_enable_receiving failed")

        self._fd = int(self._lib.udev_monitor_get_fd(self._mon))
        os.set_blocking(self._fd, False)

    @staticmethod
    def _load_libudev() -> Any:
        lib = _load_shared_library("VINPUT_TRIGGER_LIBUDEV_PATH", "udev", ("libudev.so.1", "libudev.so"))

        lib.udev_new.restype = ctypes.c_void_p
        lib.udev_unref.argtypes = [ctypes.c_void_p]

        lib.udev_enumerate_new.argtypes = [ctypes.c_void_p]
        lib.udev_enumerate_new.restype = ctypes.c_void_p
        lib.udev_enumerate_unref.argtypes = [ctypes.c_void_p]
        lib.udev_enumerate_add_match_subsystem.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udev_enumerate_add_match_sysname.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udev_enumerate_scan_devices.argtypes = [ctypes.c_void_p]
        lib.udev_enumerate_get_list_entry.argtypes = [ctypes.c_void_p]
        lib.udev_enumerate_get_list_entry.restype = ctypes.c_void_p

        lib.udev_list_entry_get_next.argtypes = [ctypes.c_void_p]
        lib.udev_list_entry_get_next.restype = ctypes.c_void_p
        lib.udev_list_entry_get_name.argtypes = [ctypes.c_void_p]
        lib.udev_list_entry_get_name.restype = ctypes.c_char_p

        lib.udev_device_new_from_syspath.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udev_device_new_from_syspath.restype = ctypes.c_void_p
        lib.udev_device_unref.argtypes = [ctypes.c_void_p]
        lib.udev_device_get_devnode.argtypes = [ctypes.c_void_p]
        lib.udev_device_get_devnode.restype = ctypes.c_char_p
        lib.udev_device_get_action.argtypes = [ctypes.c_void_p]
        lib.udev_device_get_action.restype = ctypes.c_char_p
        lib.udev_device_get_property_value.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udev_device_get_property_value.restype = ctypes.c_char_p

        lib.udev_monitor_new_from_netlink.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.udev_monitor_new_from_netlink.restype = ctypes.c_void_p
        lib.udev_monitor_unref.argtypes = [ctypes.c_void_p]
        lib.udev_monitor_filter_add_match_subsystem_devtype.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.udev_monitor_enable_receiving.argtypes = [ctypes.c_void_p]
        lib.udev_monitor_get_fd.argtypes = [ctypes.c_void_p]
        lib.udev_monitor_get_fd.restype = ctypes.c_int
        lib.udev_monitor_receive_device.argtypes = [ctypes.c_void_p]
        lib.udev_monitor_receive_device.restype = ctypes.c_void_p
        return lib

    @property
    def fd(self) -> int:
        return self._fd

    @property
    def check_pointer_props(self) -> bool:
        return False

    def _prop_is_one(self, dev: int, key: bytes) -> bool:
        val = self._lib.udev_device_get_property_value(dev, key)
        return val == b"1"

    def _matches_udev_device(self, dev: int) -> str | None:
        node_raw = self._lib.udev_device_get_devnode(dev)
        if not node_raw:
            return None
        node = node_raw.decode("utf-8", errors="replace")
        if not node.startswith("/dev/input/event"):
            return None

        if self._device_path != "auto":
            target_real = os.path.realpath(self._device_path)
            if os.path.realpath(node) == target_real:
                return node
            return None

        is_mouse = self._prop_is_one(dev, b"ID_INPUT_MOUSE")
        is_trigger = self._prop_is_one(dev, b"FCITX5_VINPUT_TRIGGER")
        if not (is_mouse or is_trigger):
            return None

        excluded = (
            self._prop_is_one(dev, b"ID_INPUT_KEYBOARD")
            or self._prop_is_one(dev, b"ID_INPUT_TOUCHPAD")
            or self._prop_is_one(dev, b"ID_INPUT_TABLET")
        )
        return None if excluded else node

    def scan_candidates(self) -> list[str]:
        candidates: list[str] = []
        enum = self._lib.udev_enumerate_new(self._ctx)
        if not enum:
            return candidates
        try:
            self._lib.udev_enumerate_add_match_subsystem(enum, b"input")
            self._lib.udev_enumerate_add_match_sysname(enum, b"event*")
            self._lib.udev_enumerate_scan_devices(enum)
            entry = self._lib.udev_enumerate_get_list_entry(enum)
            while entry:
                syspath = self._lib.udev_list_entry_get_name(entry)
                dev = self._lib.udev_device_new_from_syspath(self._ctx, syspath)
                if dev:
                    try:
                        node = self._matches_udev_device(dev)
                        if node:
                            candidates.append(node)
                    finally:
                        self._lib.udev_device_unref(dev)
                entry = self._lib.udev_list_entry_get_next(entry)
        finally:
            self._lib.udev_enumerate_unref(enum)
        candidates.sort()
        return candidates

    def read_changes(self) -> tuple[list[str], list[str]]:
        added: list[str] = []
        removed: list[str] = []
        while True:
            dev = self._lib.udev_monitor_receive_device(self._mon)
            if not dev:
                break
            try:
                action_raw = self._lib.udev_device_get_action(dev)
                action = action_raw.decode("ascii", errors="replace") if action_raw else ""
                node_raw = self._lib.udev_device_get_devnode(dev)
                node = node_raw.decode("utf-8", errors="replace") if node_raw else ""
                if action == "remove" and node.startswith("/dev/input/event"):
                    removed.append(node)
                elif action in ("add", "change"):
                    matched = self._matches_udev_device(dev)
                    if matched:
                        added.append(matched)
            finally:
                self._lib.udev_device_unref(dev)
        return added, removed

    def close(self) -> None:
        if getattr(self, "_mon", None):
            self._lib.udev_monitor_unref(self._mon)
            self._mon = None
        if getattr(self, "_ctx", None):
            self._lib.udev_unref(self._ctx)
            self._ctx = None


class InotifyDeviceWatcher:
    """Fallback hotplug watcher using libc inotify when libudev.so.1 is absent."""

    @staticmethod
    def _load_libc() -> Any:
        try:
            proc_libc = ctypes.CDLL(None, use_errno=True)
            if hasattr(proc_libc, "inotify_init1") and hasattr(proc_libc, "inotify_add_watch"):
                return proc_libc
        except OSError:
            return _load_shared_library("", "c", ("libc.so.6",), use_errno=True)
        return _load_shared_library("", "c", ("libc.so.6",), use_errno=True)

    def __init__(self, device_path: str, input_dir: str = "/dev/input") -> None:
        self._device_path = device_path
        self._input_dir = input_dir
        libc = self._load_libc()
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int

        self._fd = int(libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC))
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

        wd = int(libc.inotify_add_watch(self._fd, input_dir.encode("utf-8"), IN_CREATE | IN_ATTRIB | IN_DELETE))
        if wd < 0:
            err = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(err, os.strerror(err))

    @property
    def fd(self) -> int:
        return self._fd

    @property
    def check_pointer_props(self) -> bool:
        return self._device_path == "auto"

    def scan_candidates(self) -> list[str]:
        if self._device_path != "auto":
            resolved = os.path.realpath(self._device_path)
            return [resolved] if os.path.exists(resolved) else []
        paths = glob.glob(os.path.join(self._input_dir, "event*"))
        return sorted(p for p in paths if os.path.basename(p)[5:].isdigit())

    def _parse_inotify_buffer(self, raw: bytes) -> tuple[list[str], list[str]]:
        added: list[str] = []
        removed: list[str] = []
        offset = 0
        while offset + INOTIFY_HEADER_STRUCT.size <= len(raw):
            _wd, mask, _cookie, length = INOTIFY_HEADER_STRUCT.unpack_from(raw, offset)
            offset += INOTIFY_HEADER_STRUCT.size
            name_bytes = raw[offset : offset + length].split(b"\x00", 1)[0]
            offset += length
            name = name_bytes.decode("utf-8", errors="replace")
            if not name.startswith("event") or not name[5:].isdigit():
                continue
            path = os.path.join(self._input_dir, name)
            if mask & IN_DELETE:
                removed.append(path)
            elif mask & (IN_CREATE | IN_ATTRIB):
                added.append(path)
        return added, removed

    def read_changes(self) -> tuple[list[str], list[str]]:
        added: list[str] = []
        removed: list[str] = []
        while True:
            try:
                raw = os.read(self._fd, 4096)
            except BlockingIOError:
                break
            if not raw:
                break
            chunk_added, chunk_removed = self._parse_inotify_buffer(raw)
            added.extend(chunk_added)
            removed.extend(chunk_removed)
        if self._device_path != "auto":
            target = os.path.realpath(self._device_path)
            added = [p for p in added if os.path.realpath(p) == target]
            removed = [p for p in removed if os.path.realpath(p) == target]
        return added, removed

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def create_watcher(device_path: str) -> DeviceWatcher:
    """Create a udev-backed device watcher, falling back to inotify if libudev is absent."""
    try:
        return UdevDeviceWatcher(device_path)
    except OSError:
        return InotifyDeviceWatcher(device_path)


class TriggerStateMachine:
    """Tap / Hold / Both state machine synchronized with daemon status broadcasts."""

    def __init__(self, mode: str, capture_device: str = "", tap_threshold_sec: float = TAP_THRESHOLD_SEC) -> None:
        self.mode = mode
        self.capture_device = capture_device
        self.tap_threshold_sec = tap_threshold_sec
        self.daemon_status = "idle"
        self.local_recording = False
        self.tap_locked = False
        self.suppress_next_release = False
        self.press_time: float | None = None
        self.active_key: tuple[str, int] | None = None

    def _is_recording(self) -> bool:
        return self.local_recording or self.daemon_status == "recording"

    def _is_idle(self) -> bool:
        return not self.local_recording and self.daemon_status == "idle"

    def on_status(self, status: str) -> None:
        """Synchronize local trigger state from daemon status broadcasts."""
        self.daemon_status = status
        if status == "recording":
            self.local_recording = True
        elif status == "idle":
            self.local_recording = False
            self.tap_locked = False
            self.press_time = None
            self.suppress_next_release = False
            self.active_key = None

    def _handle_press(self, device_key: str, code: int, now: float) -> list[dict[str, Any]]:
        if self.active_key is not None and self.active_key != (device_key, code):
            return []

        if self.mode == "tap":
            self.active_key = (device_key, code)
            if self._is_recording():
                self.local_recording = False
                self.tap_locked = False
                return [{"type": "stop", "ime": True}]
            if self._is_idle():
                self.local_recording = True
                self.tap_locked = True
                return [self._build_start_cmd()]
            return []

        if self.mode == "hold":
            if self._is_idle():
                self.active_key = (device_key, code)
                self.press_time = now
                self.local_recording = True
                return [self._build_start_cmd()]
            return []

        # mode == "both"
        if self.tap_locked and self._is_recording():
            self.active_key = (device_key, code)
            self.tap_locked = False
            self.local_recording = False
            self.suppress_next_release = True
            return [{"type": "stop", "ime": True}]

        if self._is_idle():
            self.active_key = (device_key, code)
            self.press_time = now
            self.tap_locked = False
            self.local_recording = True
            return [self._build_start_cmd()]
        return []

    def _handle_release(self, device_key: str, code: int, now: float) -> list[dict[str, Any]]:
        if self.active_key != (device_key, code):
            return []
        self.active_key = None

        if self.mode == "tap":
            return []

        if self.suppress_next_release:
            self.suppress_next_release = False
            return []

        pressed_at = self.press_time
        self.press_time = None
        if pressed_at is None:
            return []

        if self.mode == "hold":
            if self._is_recording():
                self.local_recording = False
                return [{"type": "stop", "ime": True}]
            return []

        # mode == "both"
        if (now - pressed_at) < self.tap_threshold_sec:
            self.tap_locked = True
            return []

        if self._is_recording():
            self.local_recording = False
            return [{"type": "stop", "ime": True}]
        return []

    def _build_start_cmd(self) -> dict[str, Any]:
        cmd: dict[str, Any] = {"type": "start", "ime": True, "mode": "normal"}
        if self.capture_device:
            cmd["capture_device"] = self.capture_device
        return cmd

    def on_button_event(self, device_key: str, code: int, value: int, now: float) -> list[dict[str, Any]]:
        """Process a target EV_KEY event (1=press, 0=release, 2=repeat) and return commands."""
        if value == 2:
            return []
        if value == 1:
            return self._handle_press(device_key, code, now)
        if value == 0:
            return self._handle_release(device_key, code, now)
        return []

    def on_device_removed(self, device_key: str) -> list[dict[str, Any]]:
        """Release held state cleanly if the active input device disconnects."""
        if self.active_key is None or self.active_key[0] != device_key:
            return []
        self.active_key = None
        self.press_time = None
        if self.mode in ("hold", "both") and not self.tap_locked and self._is_recording():
            self.local_recording = False
            return [{"type": "stop", "ime": True}]
        return []


_STATE_MACHINE: TriggerStateMachine | None = None


def on_daemon_broadcast(event: dict[str, Any]) -> None:
    """Handle incoming JSONL broadcast events from vinput-daemon."""
    if event.get("type") == "status" and _STATE_MACHINE is not None:
        _STATE_MACHINE.on_status(str(event.get("status", "")))


class MouseTriggerRunner:
    """Single-threaded selector loop multiplexing stdin, hotplug watcher, and evdev fds."""

    def __init__(self, cfg: ClientConfig, watcher: DeviceWatcher, state: TriggerStateMachine) -> None:
        self.cfg = cfg
        self.watcher = watcher
        self.state = state
        self.selector = selectors.DefaultSelector()
        self.open_devices: dict[int, tuple[str, bytearray]] = {}
        self.path_to_fd: dict[str, int] = {}
        self._stdin_buf = bytearray()
        self._stdin_fd = sys.stdin.fileno()

    def _emit_commands(self, cmds: list[dict[str, Any]]) -> None:
        for cmd in cmds:
            send_command(cmd)

    def _try_register_device(self, path: str) -> str | None:
        if path in self.path_to_fd:
            return None
        try:
            fd = open_matching_evdev(
                path,
                self.cfg.button_codes,
                check_pointer_props=self.watcher.check_pointer_props,
            )
        except PermissionError:
            return "permission"
        except OSError:
            return None

        if fd is None:
            return None

        self.open_devices[fd] = (path, bytearray())
        self.path_to_fd[path] = fd
        self.selector.register(fd, selectors.EVENT_READ, data=("evdev", fd))
        return "opened"

    def _unregister_fd(self, fd: int) -> None:
        entry = self.open_devices.pop(fd, None)
        if entry is None:
            return
        path, _buf = entry
        self.path_to_fd.pop(path, None)
        try:
            self.selector.unregister(fd)
        except (KeyError, ValueError):
            # Fd may already be detached during shutdown.
            return
        finally:
            os.close(fd)
        self._emit_commands(self.state.on_device_removed(path))

    def _unregister_path(self, path: str) -> None:
        fd = self.path_to_fd.get(path)
        if fd is not None:
            self._unregister_fd(fd)

    def _drain_stdin(self) -> bool:
        while True:
            try:
                chunk = os.read(self._stdin_fd, 4096)
            except BlockingIOError:
                return True
            except OSError:
                return False

            if not chunk:
                return False
            self._stdin_buf.extend(chunk)
            self._process_stdin_lines()

    def _process_stdin_lines(self) -> None:
        while b"\n" in self._stdin_buf:
            line_bytes, _, rest = self._stdin_buf.partition(b"\n")
            self._stdin_buf = bytearray(rest)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                on_daemon_broadcast(event)

    def _handle_evdev_read(self, fd: int) -> None:
        entry = self.open_devices.get(fd)
        if entry is None:
            return
        path, buf = entry
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EBADF):
                    self._unregister_fd(fd)
                return
            if not chunk:
                self._unregister_fd(fd)
                return
            buf.extend(chunk)

        self._dispatch_evdev_buffer(path, buf)

    def _dispatch_evdev_buffer(self, path: str, buf: bytearray) -> None:
        now = time.monotonic()
        size = INPUT_EVENT_STRUCT.size
        offset = 0
        while offset + size <= len(buf):
            _sec, _usec, ev_type, code, value = INPUT_EVENT_STRUCT.unpack_from(buf, offset)
            offset += size
            if ev_type == EV_KEY and code in self.cfg.button_codes:
                self._emit_commands(self.state.on_button_event(path, code, value, now))
        if offset:
            del buf[:offset]

    def _handle_watcher_events(self) -> None:
        added, removed = self.watcher.read_changes()
        for path in removed:
            self._unregister_path(path)
        for path in added:
            self._try_register_device(path)

    def _startup_scan(self) -> None:
        candidates = self.watcher.scan_candidates()
        first_perm_denied: str | None = None
        for path in candidates:
            outcome = self._try_register_device(path)
            if outcome == "permission" and first_perm_denied is None:
                first_perm_denied = path

        if not self.open_devices and first_perm_denied is not None:
            raise VInputError("permission_denied", exit_code=EXIT_USAGE_ERROR, path=first_perm_denied)

    def run_loop(self) -> int:
        os.set_blocking(self._stdin_fd, False)
        self.selector.register(self._stdin_fd, selectors.EVENT_READ, data=("stdin", self._stdin_fd))
        self.selector.register(self.watcher.fd, selectors.EVENT_READ, data=("watcher", self.watcher.fd))

        try:
            self._startup_scan()
            running = True
            while running:
                for key, _events in self.selector.select():
                    kind, target_fd = key.data
                    if kind == "stdin":
                        if not self._drain_stdin():
                            running = False
                            break
                    elif kind == "watcher":
                        self._handle_watcher_events()
                    elif kind == "evdev":
                        self._handle_evdev_read(target_fd)
        finally:
            self.close()
        return EXIT_SUCCESS

    def close(self) -> None:
        for fd in list(self.open_devices):
            self._unregister_fd(fd)
        self.selector.close()
        self.watcher.close()


def run() -> int:
    """Main entry flow for the mouse button trigger."""
    global _STATE_MACHINE
    cfg = load_config()
    _STATE_MACHINE = TriggerStateMachine(mode=cfg.mode, capture_device=cfg.capture_device)
    watcher = create_watcher(cfg.device_path)
    runner = MouseTriggerRunner(cfg=cfg, watcher=watcher, state=_STATE_MACHINE)
    return runner.run_loop()


def main() -> int:
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("unexpected_error", error=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
