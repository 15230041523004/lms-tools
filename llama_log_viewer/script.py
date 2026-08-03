"""
llama_log_viewer — TextGen extension: live TextGen stdout/stderr (launcher tee).
VERSION 1.7.1

Long-poll: server holds the request until the log file grows (or timeout ~12s).
Client re-issues one request after each response (no 1s setInterval stampede).
Poll outputs ONLY the textbox. Footer is static HTML + client timestamps.
Layout: fill viewport so footer sits flush at window bottom (rare, throttled).
"""
from __future__ import annotations

import base64
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import gradio as gr

VERSION = "1.7.1"
# Server long-poll hold; client uses this + slack as max wait before re-arm.
LONG_POLL_MAX_S = 12.0
LONG_POLL_SLICE_S = 0.4
# Client watchdog base = this value (plus slack in JS).
POLL_INTERVAL_S = LONG_POLL_MAX_S
params = {
    "display_name": "Llama Logs",
    "is_tab": True,
}

INITIAL_TAIL_BYTES = 512 * 1024
OLDER_CHUNK_BYTES = 256 * 1024
WINDOW_MAX_LINES = 8000
MAX_READ_BYTES = 16 * 1024 * 1024
RETRY_COUNT = 3
RETRY_SLEEP = 0.2

# CSI / OSC and bare ESC sequences from rich/colorama console output
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")

LogUpdate = Union[str, Any]  # str | gr.update()

# Server-side view state (not gr.State — avoids poll re-render of State component)
_VIEW_LOCK = threading.Lock()
_VIEW: Dict[str, Any] = {}


def _strip_ansi(s: str) -> str:
    if not s:
        return s
    return _ANSI_RE.sub("", s)


@dataclass
class ResolvedLogPath:
    path: Optional[Path]
    source: str
    warning: Optional[str] = None
    error: Optional[str] = None


def _is_absolute(p: str) -> bool:
    try:
        return Path(p).is_absolute()
    except Exception:
        return False


def _sidecar_path() -> Path:
    return Path(__file__).with_name("log_path.txt")


def _structural_log_path() -> Path:
    script_path = Path(__file__).resolve()
    user_data = script_path.parents[2]
    try:
        logs_dir = script_path.parents[3] / "logs"
        candidates = list(logs_dir.glob("textgen-*.log"))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    return user_data / "textgen_console.log"


def _read_sidecar() -> Tuple[Optional[str], Optional[str]]:
    sidecar = _sidecar_path()
    if not sidecar.is_file():
        return None, None
    try:
        value = sidecar.read_text(encoding="utf-8-sig", errors="replace").strip()
    except Exception as e:
        return None, str(e)
    return (value or None), None


def resolve_log_path() -> ResolvedLogPath:
    value, sidecar_err = _read_sidecar()
    if value:
        if not _is_absolute(value):
            return ResolvedLogPath(
                path=_structural_log_path(),
                source="structural",
                warning=f"log_path.txt is not absolute (ignored): {value!r}",
            )
        return ResolvedLogPath(path=Path(value), source="log_path.txt")

    env = (os.environ.get("LLAMA_ARG_LOG_FILE") or "").strip()
    if env:
        if not _is_absolute(env):
            return ResolvedLogPath(
                path=None,
                source="LLAMA_ARG_LOG_FILE",
                error=f"LLAMA_ARG_LOG_FILE is not absolute (rejected): {env!r}.",
            )
        path = Path(env)
        if not path.parent.exists():
            return ResolvedLogPath(
                path=path,
                source="LLAMA_ARG_LOG_FILE",
                error=f"Log parent directory does not exist: {path.parent}.",
            )
        return ResolvedLogPath(path=path, source="LLAMA_ARG_LOG_FILE")

    if sidecar_err:
        return ResolvedLogPath(
            path=_structural_log_path(),
            source="structural",
            warning=f"Failed to read log_path.txt: {sidecar_err}",
        )
    return ResolvedLogPath(path=_structural_log_path(), source="structural")


def setup():
    return None


def _winerror(exc: BaseException) -> Optional[int]:
    return getattr(exc, "winerror", None)


def _open_retry(path: Path, mode: str = "rb"):
    last = None
    for attempt in range(RETRY_COUNT):
        try:
            return open(path, mode)
        except OSError as e:
            last = e
            we = _winerror(e)
            if we == 5:
                raise
            if we in (32, 33) or isinstance(e, PermissionError):
                if attempt + 1 < RETRY_COUNT:
                    time.sleep(RETRY_SLEEP)
                    continue
            raise
    if last:
        raise last
    raise OSError("open failed")


def _default_state() -> Dict[str, Any]:
    return {
        "path": None,
        "file_size": -1,
        "file_mtime": None,
        "read_start": 0,
        "read_end": 0,
        "text": "",
        "view_clear_at": 0,
        "pending": b"",
        "empty_msg_shown": False,
        "has_more_above": False,
        "content_ts": None,
        "last_note": None,
    }


def _get_view() -> Dict[str, Any]:
    if not _VIEW:
        return _default_state()
    return dict(_VIEW)


def _set_view(state: Dict[str, Any]) -> None:
    _VIEW.clear()
    _VIEW.update(state)


def _trim_window(state: Dict[str, Any]) -> None:
    text = state.get("text") or ""
    lines = text.splitlines(keepends=True)
    if len(lines) <= WINDOW_MAX_LINES:
        return
    drop = len(lines) - WINDOW_MAX_LINES
    state["text"] = "".join(lines[drop:])
    state["has_more_above"] = True


def _decode_chunk(data: bytes, drop_partial_first: bool) -> str:
    text = data.decode("utf-8", errors="replace")
    if not text:
        return ""
    if drop_partial_first and data:
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    return _strip_ansi(text)


def _load_tail(path: Path, state: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Load last INITIAL_TAIL_BYTES (respecting view_clear_at)."""
    try:
        with _open_retry(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            size = st.st_size
            mtime = st.st_mtime
            clear_at = int(state.get("view_clear_at") or 0)
            if clear_at > size:
                clear_at = size
            usable = max(0, size - clear_at)
            if usable == 0:
                state.update(
                    {
                        "path": str(path),
                        "file_size": size,
                        "file_mtime": mtime,
                        "read_start": clear_at,
                        "read_end": size,
                        "text": "",
                        "pending": b"",
                        "empty_msg_shown": False,
                        "has_more_above": clear_at > 0,
                    }
                )
                return "", state, None

            take = min(INITIAL_TAIL_BYTES, usable, MAX_READ_BYTES)
            start = size - take
            if start < clear_at:
                start = clear_at
            fh.seek(start)
            raw = fh.read(size - start)
            drop_partial = start > clear_at
            text = _decode_chunk(raw, drop_partial_first=drop_partial)
            state.update(
                {
                    "path": str(path),
                    "file_size": size,
                    "file_mtime": mtime,
                    "read_start": start,
                    "read_end": size,
                    "text": text,
                    "pending": b"",
                    "empty_msg_shown": False,
                    "has_more_above": start > clear_at,
                }
            )
            _trim_window(state)
            return state["text"], state, None
    except OSError as e:
        we = _winerror(e)
        if we in (32, 33) or (we is None and isinstance(e, PermissionError)):
            return state.get("text") or "", state, "Файл занят другим процессом."
        if we == 5:
            return state.get("text") or "", state, f"Нет прав на чтение: {path}"
        return state.get("text") or "", state, f"Ошибка чтения: {e}"
    except Exception as e:
        return state.get("text") or "", state, f"Ошибка чтения: {e}"


def _mark_content(state: Dict[str, Any], mtime: Optional[float]) -> None:
    state["content_ts"] = time.time()
    state["file_mtime"] = mtime


def _poll_body(state: Dict[str, Any]) -> Tuple[LogUpdate, Dict[str, Any]]:
    """Core poll logic. Returns (textbox value or gr.update(), new state)."""
    resolved = resolve_log_path()
    if resolved.path is None:
        note = resolved.error or "Не удалось определить путь к логу."
        if state.get("empty_msg_shown") and state.get("text") == note:
            return gr.update(), state
        state = {**state, "empty_msg_shown": True, "file_mtime": None, "text": note}
        _mark_content(state, None)
        return note, state

    path = resolved.path
    path_str = str(path)

    if state.get("path") not in (None, path_str):
        state = _default_state()

    if not path.is_file():
        msg = (
            "Файл вывода TextGen ещё не создан.\n"
            "Запустите TextGen через textgen-8060s.ps1 — лог появится сразу."
        )
        if state.get("empty_msg_shown") and (state.get("text") in (msg, "")):
            return gr.update(), state
        state.update(
            {
                "empty_msg_shown": True,
                "path": path_str,
                "file_size": -1,
                "file_mtime": None,
                "text": msg,
            }
        )
        _mark_content(state, None)
        return msg, state

    try:
        with _open_retry(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            size = st.st_size
            mtime = st.st_mtime

            # recreate / shrink
            if state.get("file_size", -1) >= 0 and size < int(state.get("read_end") or 0):
                state = _default_state()
                state["path"] = path_str
                text, state, err = _load_tail(path, state)
                _mark_content(state, mtime)
                if err:
                    state["last_note"] = err
                return text, state

            # no growth → do not re-render textbox
            if (
                state.get("path") == path_str
                and int(state.get("file_size", -1)) == size
                and state.get("text") is not None
                and state.get("empty_msg_shown") is False
                and int(state.get("read_end", -1)) == size
            ):
                state["file_mtime"] = mtime
                return gr.update(), state

            # first paint
            if state.get("file_size", -1) < 0 or (
                not state.get("text") and int(state.get("read_end") or 0) == 0 and size > 0
            ):
                if int(state.get("view_clear_at") or 0) >= size and size > 0:
                    state.update(
                        {
                            "path": path_str,
                            "file_size": size,
                            "file_mtime": mtime,
                            "read_start": size,
                            "read_end": size,
                            "text": "",
                            "empty_msg_shown": False,
                        }
                    )
                    return gr.update(), state

                text, state, err = _load_tail(path, state)
                _mark_content(state, mtime)
                if err:
                    state["last_note"] = err
                return text, state

            # append-only delta
            read_end = int(state.get("read_end") or 0)
            clear_at = int(state.get("view_clear_at") or 0)
            if read_end < clear_at:
                read_end = clear_at

            if size <= read_end:
                state["file_size"] = size
                state["file_mtime"] = mtime
                state["read_end"] = size
                return gr.update(), state

            available = size - read_end
            note = ""
            if available > MAX_READ_BYTES:
                seek_pos = size - MAX_READ_BYTES
                note = (
                    f"Часть данных пропущена (>{MAX_READ_BYTES // (1024 * 1024)} MiB за тик). "
                    "Показан последний фрагмент."
                )
            else:
                seek_pos = read_end

            fh.seek(seek_pos)
            raw = fh.read(size - seek_pos)

            pending = state.get("pending") or b""
            if not isinstance(pending, (bytes, bytearray)):
                pending = b""
            if seek_pos != read_end:
                pending = b""

            data = bytes(pending) + raw
            if data and not data.endswith((b"\n", b"\r")):
                last_nl = max(data.rfind(b"\n"), data.rfind(b"\r"))
                if last_nl != -1:
                    complete, pending = data[: last_nl + 1], data[last_nl + 1 :]
                else:
                    complete, pending = b"", data
            else:
                complete, pending = data, b""

            delta = _strip_ansi(complete.decode("utf-8", errors="replace"))
            state["file_size"] = size
            state["file_mtime"] = mtime
            state["read_end"] = size
            state["path"] = path_str
            state["pending"] = pending
            state["empty_msg_shown"] = False

            if not delta:
                return gr.update(), state

            state["text"] = (state.get("text") or "") + delta
            _trim_window(state)
            _mark_content(state, mtime)
            if note:
                state["last_note"] = note
            return state["text"], state

    except OSError as e:
        we = _winerror(e)
        if we in (32, 33) or (we is None and isinstance(e, PermissionError)):
            note = "Файл занят другим процессом."
        elif we == 5:
            note = f"Нет прав на чтение: {path}"
        else:
            note = f"Ошибка чтения: {e}"
        if state.get("last_note") == note:
            return gr.update(), state
        state["last_note"] = note
        return gr.update(), state
    except Exception as e:
        note = f"Ошибка: {e}"
        if state.get("last_note") == note:
            return gr.update(), state
        state["last_note"] = note
        return gr.update(), state


def _file_sig(path_str: Optional[str]) -> Tuple[bool, int]:
    """Return (exists, size). size=-1 if missing/unreadable."""
    if not path_str:
        return False, -1
    try:
        p = Path(path_str)
        if not p.is_file():
            return False, -1
        return True, int(p.stat().st_size)
    except OSError:
        return False, -1


def _wait_for_log_change(
    path_str: Optional[str],
    prev_exists: bool,
    prev_size: int,
    max_s: float = LONG_POLL_MAX_S,
    slice_s: float = LONG_POLL_SLICE_S,
) -> None:
    """
    Block until log path appears/disappears/grows/shrinks, or max_s elapses.
    Runs WITHOUT _VIEW_LOCK so load_older / UI stay responsive.
    """
    deadline = time.monotonic() + max(0.5, float(max_s))
    while time.monotonic() < deadline:
        time.sleep(slice_s)
        if path_str:
            exists, size = _file_sig(path_str)
            if exists != prev_exists or size != prev_size:
                return
            continue
        # No path yet — wait until resolve finds a file
        resolved = resolve_log_path()
        if resolved.path is not None and resolved.path.is_file():
            return


def poll_tick() -> LogUpdate:
    """
    Long-poll: read once; if nothing new, wait for file change (or timeout), read again.
    Single textbox output only.
    """
    with _VIEW_LOCK:
        state = _get_view()
        result, state = _poll_body(state)
        _set_view(state)
        if isinstance(result, str):
            # Content (or message) changed — return immediately
            return result
        path_str = state.get("path")
        exists, size = _file_sig(path_str if isinstance(path_str, str) else None)
        # Prefer size from state when file known
        if exists and int(state.get("file_size", -1)) >= 0:
            size = int(state.get("file_size"))
        prev_exists, prev_size = exists, size

    _wait_for_log_change(path_str if isinstance(path_str, str) else None, prev_exists, prev_size)

    with _VIEW_LOCK:
        state = _get_view()
        result, state = _poll_body(state)
        _set_view(state)
        return result


def _load_older_body(state: Dict[str, Any]) -> Tuple[LogUpdate, Dict[str, Any]]:
    resolved = resolve_log_path()
    if resolved.path is None or not resolved.path.is_file():
        return gr.update(), state

    path = resolved.path
    read_start = int(state.get("read_start") or 0)
    clear_at = int(state.get("view_clear_at") or 0)
    if read_start <= clear_at:
        state["has_more_above"] = False
        return gr.update(), state

    try:
        with _open_retry(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            mtime = st.st_mtime
            chunk_end = read_start
            chunk_start = max(clear_at, chunk_end - OLDER_CHUNK_BYTES)
            if chunk_start >= chunk_end:
                state["has_more_above"] = False
                return gr.update(), state

            fh.seek(chunk_start)
            raw = fh.read(chunk_end - chunk_start)
            drop_partial = chunk_start > clear_at
            older = _decode_chunk(raw, drop_partial_first=drop_partial)
            if not older:
                state["read_start"] = chunk_start
                state["has_more_above"] = chunk_start > clear_at
                return gr.update(), state

            state["text"] = older + (state.get("text") or "")
            state["read_start"] = chunk_start
            state["has_more_above"] = chunk_start > clear_at
            state["file_mtime"] = mtime
            _trim_window(state)
            # Do not bump content_ts for history prepend
            return state["text"], state
    except OSError:
        return gr.update(), state


def load_older() -> LogUpdate:
    """Prepend an older chunk when user scrolls to top. Textbox only."""
    with _VIEW_LOCK:
        state = _get_view()
        result, state = _load_older_body(state)
        _set_view(state)
        return result


def custom_css():
    return """
/* Fill tab: header + log + footer flush to window bottom (JS sets exact px) */
#llama-log-viewer-root {
    box-sizing: border-box !important;
    width: 100% !important;
    max-width: 100% !important;
    height: calc(100vh - 64px) !important;
    max-height: calc(100vh - 64px) !important;
    min-height: 280px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}
/* Hide Gradio poll progress flash */
#llama-log-viewer-root .progress-text,
#llama-log-viewer-root .meta-text,
#llama-log-viewer-root .eta-bar,
#llama-log-viewer-root .pending,
#llama-log-viewer-root .progress-level,
#llama-log-viewer-root .progress-level-inner,
#llama-log-viewer-root .generating,
#llama-log-viewer-output .progress-text,
#llama-log-viewer-output .meta-text,
#llama-log-viewer-output .eta-bar,
#llama-log-viewer-output .pending,
#llama-log-viewer-output .progress-level {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    opacity: 0 !important;
    overflow: hidden !important;
    pointer-events: none !important;
    border: 0 !important;
}
/* Native HTML header */
#llama-log-header-wrap,
#llama-log-header-wrap.block {
    flex: 0 0 auto !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    min-height: 0 !important;
    overflow: visible !important;
}
#llama-log-header {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: space-between !important;
    width: 100% !important;
    box-sizing: border-box !important;
    margin: 0 !important;
    padding: 0 2px !important;
    min-height: 1.5rem !important;
    gap: 0.75rem !important;
}
#llama-log-header .llama-log-title {
    font-weight: 600 !important;
    font-size: 0.95rem !important;
    line-height: 1.3 !important;
    margin: 0 !important;
    padding: 0 !important;
    flex: 1 1 auto !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
#llama-log-header .llama-log-as-label {
    display: inline-flex !important;
    align-items: center !important;
    gap: 0.4rem !important;
    margin: 0 !important;
    padding: 0 !important;
    cursor: pointer !important;
    user-select: none !important;
    white-space: nowrap !important;
    font-size: 0.9rem !important;
    flex: 0 0 auto !important;
}
#llama-log-header #llama-log-autoscroll-cb {
    margin: 0 !important;
    cursor: pointer !important;
}
/* Critical: Gradio wraps Textbox in .form with inline flex-grow:0 */
#llama-log-viewer-root > .form,
#llama-log-viewer-root .form:has(#llama-log-viewer-output) {
    flex: 1 1 auto !important;
    flex-grow: 1 !important;
    min-height: 0 !important;
    height: auto !important;
    max-height: none !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    align-self: stretch !important;
}
/* Log: take all free space between header and footer */
#llama-log-viewer-output,
#llama-log-viewer-output.block {
    box-sizing: border-box !important;
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
    min-width: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    overflow: hidden !important;
    flex: 1 1 auto !important;
    height: 100% !important;
}
#llama-log-viewer-output > .label-wrap {
    display: none !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}
#llama-log-viewer-output label.container,
#llama-log-viewer-output .wrap,
#llama-log-viewer-output .input-container {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    height: 100% !important;
    max-height: none !important;
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    flex-direction: column !important;
}
#llama-log-viewer-output textarea {
    box-sizing: border-box !important;
    display: block !important;
    width: 100% !important;
    flex: 1 1 auto !important;
    height: 100% !important;
    min-height: 120px !important;
    max-height: none !important;
    overflow: auto !important;
    resize: none !important;
    white-space: pre-wrap !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace !important;
    margin: 0 !important;
}
/* Static footer (not a Gradio poll output — never re-rendered by clicks) */
#llama-log-footer-wrap,
#llama-log-footer-wrap.block {
    flex: 0 0 auto !important;
    flex-grow: 0 !important;
    flex-shrink: 0 !important;
    margin: 0 !important;
    margin-top: 0 !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    min-height: 0 !important;
    overflow: visible !important;
    align-self: stretch !important;
}
#llama-log-footer-static {
    font-size: 0.85em !important;
    opacity: 0.85 !important;
    line-height: 1.25 !important;
    margin: 0 !important;
    padding: 2px 2px 0 2px !important;
}
#llama-log-load-older,
#llama-log-poll,
#llama-log-inline-probe,
#llama-log-load-older.block,
#llama-log-poll.block,
#llama-log-inline-probe.block {
    position: absolute !important;
    left: -10000px !important;
    top: 0 !important;
    width: 1px !important;
    height: 1px !important;
    min-height: 0 !important;
    overflow: hidden !important;
    opacity: 0.01 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    flex: 0 0 0 !important;
}
"""


def _client_boot_js() -> str:
    """Full client poll/autoscroll bootstrap. TextGen does not call custom_js() — inject via HTML."""
    interval_ms = int(POLL_INTERVAL_S * 1000)
    js = r"""
(function () {
  if (window.__llamaLogViewerBooted) {
    console.log('[llama_log_viewer]', 'already booted — skip re-entry');
    return;
  }
  window.__llamaLogViewerBooted = true;
  window.__llamaLogViewerCustomJsBoot = true;
  window.__llamaLogViewerInline = true;

  var TAG = '[llama_log_viewer]';
  var ROOT = '#llama-log-viewer-output textarea';
  var CB = '#llama-log-autoscroll-cb';
  var LOAD = '#llama-log-load-older';
  var POLL = '#llama-log-poll';
  /* Server holds ~INTERVAL_MS; client re-arms after each response (+ slack) */
  var LONG_POLL_MS = __INTERVAL_MS__;
  var LONG_POLL_SLACK_MS = 4000;
  var VERSION = '__VERSION__';
  /* set window.LLAMA_LOG_DEBUG = true for noisy console */
  var DEBUG = !!(window.LLAMA_LOG_DEBUG);
  var loadBusy = false;
  var pollBusy = false;
  var stickBottom = true;
  var suppressAutoscroll = false;
  var prevHeight = 0;
  var pollAttemptN = 0;
  var pollClickN = 0;
  var pollSkipN = 0;
  var bindAttemptN = 0;
  var lastText = null;
  var lastMidH = -1;
  var lastRootH = -1;
  var lastRootTop = -1;
  var parentsLoosened = false;
  var layoutPending = false;
  var layoutLastAt = 0;
  var LAYOUT_MIN_MS = 2000;
  var pollArmTimer = null;
  var pollWatchdog = null;
  var pollGen = 0;

  function ts() {
    try { return new Date().toISOString().slice(11, 23); } catch (e) { return '?'; }
  }
  function log() {
    if (!DEBUG) return;
    try {
      var args = Array.prototype.slice.call(arguments);
      args.unshift(TAG, ts());
      console.log.apply(console, args);
    } catch (e) {}
  }
  function warn() {
    try {
      var args = Array.prototype.slice.call(arguments);
      args.unshift(TAG, ts(), 'WARN');
      console.warn.apply(console, args);
    } catch (e) {}
  }
  function err() {
    try {
      var args = Array.prototype.slice.call(arguments);
      args.unshift(TAG, ts(), 'ERR');
      console.error.apply(console, args);
    } catch (e) {}
  }

  console.log('%c' + TAG + ' BOOT v' + VERSION + ' long-poll=' + LONG_POLL_MS + 'ms (quiet; LLAMA_LOG_DEBUG=true for noise)',
    'background:#0a0;color:#fff;font-size:12px;padding:2px 6px');

  function fmtLocal() {
    try {
      var d = new Date();
      function p(n) { return n < 10 ? '0' + n : '' + n; }
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
        ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    } catch (e) {
      return '—';
    }
  }

  function setFooterTimes(when) {
    try {
      var a = document.getElementById('llama-log-ts-content');
      var b = document.getElementById('llama-log-ts-file');
      if (a) a.textContent = when;
      if (b) b.textContent = when;
    } catch (e) {}
  }

  function syncFooterFromTextarea() {
    var el = ta();
    if (!el) return;
    var v = el.value || '';
    if (lastText === null) {
      lastText = v;
      if (v && v.trim()) setFooterTimes(fmtLocal());
      return;
    }
    if (v !== lastText) {
      lastText = v;
      setFooterTimes(fmtLocal());
      log('footer times updated (text changed)');
    }
  }

  function layoutFill(force) {
    try {
      var now = Date.now();
      if (!force && (now - layoutLastAt) < LAYOUT_MIN_MS) return;
      var root = document.getElementById('llama-log-viewer-root');
      var taEl = document.querySelector(ROOT);
      if (!root || !taEl) return;

      var rect = root.getBoundingClientRect();
      /* Tab not laid out yet */
      if (rect.width < 8 && rect.height < 8) return;

      var head = document.getElementById('llama-log-header-wrap');
      var footWrap = document.getElementById('llama-log-footer-wrap');
      var foot = document.getElementById('llama-log-footer-static') || footWrap;

      /* Flush to window bottom: only ~2px pad */
      var pad = 2;
      var rootTop = Math.round(rect.top);
      if (rootTop < 0) rootTop = 0;
      var rootH = Math.max(200, Math.floor(window.innerHeight - rootTop - pad));
      var headH = head ? Math.max(head.getBoundingClientRect().height, 22) : 26;
      var footH = foot ? Math.max(foot.getBoundingClientRect().height, 18) : 20;
      /* If footer height not measured yet (0), reserve minimal line */
      if (footH < 14) footH = 20;
      var midH = Math.max(100, rootH - headH - footH - 2);

      if (!force && midH === lastMidH && rootH === lastRootH && Math.abs(rootTop - lastRootTop) < 2) {
        layoutLastAt = now;
        return;
      }
      lastMidH = midH;
      lastRootH = rootH;
      lastRootTop = rootTop;
      layoutLastAt = now;

      /* Loosen Gradio parents once so our height is not clipped */
      if (!parentsLoosened) {
        parentsLoosened = true;
        try {
          var p = root.parentElement;
          var hops = 0;
          while (p && hops < 8 && p !== document.body && p !== document.documentElement) {
            p.style.setProperty('min-height', '0', 'important');
            p.style.setProperty('max-height', 'none', 'important');
            p.style.setProperty('overflow', 'visible', 'important');
            /* tab panels often need stretch */
            if (p.classList && (
              p.classList.contains('tabitem') ||
              p.classList.contains('gap') ||
              p.getAttribute('role') === 'tabpanel'
            )) {
              p.style.setProperty('height', 'auto', 'important');
              p.style.setProperty('flex', '1 1 auto', 'important');
            }
            p = p.parentElement;
            hops += 1;
          }
        } catch (eParents) {}
      }

      function setBox(el, h) {
        if (!el) return;
        el.style.setProperty('box-sizing', 'border-box', 'important');
        el.style.setProperty('flex', '1 1 auto', 'important');
        el.style.setProperty('flex-grow', '1', 'important');
        el.style.setProperty('min-height', '0', 'important');
        el.style.setProperty('height', h + 'px', 'important');
        el.style.setProperty('max-height', h + 'px', 'important');
        el.style.setProperty('overflow', 'hidden', 'important');
        el.style.setProperty('margin', '0', 'important');
        el.style.setProperty('padding', '0', 'important');
      }

      root.style.setProperty('height', rootH + 'px', 'important');
      root.style.setProperty('max-height', rootH + 'px', 'important');
      root.style.setProperty('min-height', rootH + 'px', 'important');
      root.style.setProperty('display', 'flex', 'important');
      root.style.setProperty('flex-direction', 'column', 'important');
      root.style.setProperty('overflow', 'hidden', 'important');
      root.style.setProperty('gap', '0', 'important');
      root.style.setProperty('padding', '0', 'important');
      root.style.setProperty('margin', '0', 'important');
      root.style.setProperty('box-sizing', 'border-box', 'important');

      var form = taEl.closest ? taEl.closest('.form') : null;
      var out = document.getElementById('llama-log-viewer-output');
      var label = taEl.closest ? taEl.closest('label') : null;
      setBox(form, midH);
      setBox(out, midH);
      setBox(label, midH);
      if (taEl.parentElement && taEl.parentElement !== label) setBox(taEl.parentElement, midH);

      taEl.style.setProperty('height', midH + 'px', 'important');
      taEl.style.setProperty('min-height', midH + 'px', 'important');
      taEl.style.setProperty('max-height', midH + 'px', 'important');
      taEl.style.setProperty('flex', '1 1 auto', 'important');
      taEl.style.setProperty('resize', 'none', 'important');
      taEl.style.setProperty('box-sizing', 'border-box', 'important');
      taEl.style.setProperty('margin', '0', 'important');
      taEl.rows = 4;

      if (head) {
        head.style.setProperty('flex', '0 0 auto', 'important');
        head.style.setProperty('margin', '0', 'important');
        head.style.setProperty('padding', '0', 'important');
      }
      if (footWrap) {
        footWrap.style.setProperty('flex', '0 0 auto', 'important');
        footWrap.style.setProperty('margin', '0', 'important');
        footWrap.style.setProperty('padding', '0', 'important');
        footWrap.style.setProperty('border', 'none', 'important');
      }
      if (foot && foot !== footWrap) {
        foot.style.setProperty('margin', '0', 'important');
        foot.style.setProperty('padding', '2px 2px 0 2px', 'important');
      }
      log('layoutFill midH=', midH, 'rootH=', rootH, 'top=', rootTop, 'footH=', footH);
    } catch (e) {
      log('layoutFill error', e);
    }
  }
  function layoutFillSoon(force) {
    if (layoutPending && !force) return;
    layoutPending = true;
    requestAnimationFrame(function () {
      layoutPending = false;
      layoutFill(!!force);
    });
  }
  /* If footer drifted from window bottom (>16px gap), re-fit — rare, throttled */
  function ensureFooterFlush() {
    try {
      var foot = document.getElementById('llama-log-footer-static')
        || document.getElementById('llama-log-footer-wrap');
      if (!foot) return;
      var bottom = foot.getBoundingClientRect().bottom;
      var gap = window.innerHeight - bottom;
      if (gap > 16 || gap < -6) {
        layoutFill(true);
      }
    } catch (e) {}
  }

  function dumpLlamaDom(reason) {
    try {
      var ids = ['llama-log-viewer-root', 'llama-log-poll', 'llama-log-load-older',
                 'llama-log-autoscroll', 'llama-log-viewer-output', 'llama-log-footer-static'];
      var report = { reason: reason };
      ids.forEach(function (id) {
        var el = document.getElementById(id);
        if (!el) { report[id] = null; return; }
        var st = window.getComputedStyle(el);
        var r = el.getBoundingClientRect();
        report[id] = {
          tag: el.tagName,
          cls: el.className && String(el.className).slice(0, 120),
          display: st.display,
          visibility: st.visibility,
          opacity: st.opacity,
          w: Math.round(r.width),
          h: Math.round(r.height),
          buttons: el.querySelectorAll ? el.querySelectorAll('button').length : -1,
          textareas: el.querySelectorAll ? el.querySelectorAll('textarea').length : -1
        };
      });
      report.allLlamaIds = Array.prototype.map.call(
        document.querySelectorAll('[id*="llama-log"]'),
        function (el) { return el.id + '<' + el.tagName + '>'; }
      );
      log('DOM dump', report);
      return report;
    } catch (e) {
      err('DOM dump failed', e);
      return null;
    }
  }

  function ta() {
    var el = document.querySelector(ROOT);
    if (!el) log('ta() MISS', ROOT);
    return el;
  }
  function autoscrollOn() {
    var el = document.querySelector(CB);
    return el ? !!el.checked : true;
  }
  function nearBottom(el, px) {
    return (el.scrollHeight - el.scrollTop - el.clientHeight) < (px || 96);
  }
  function scrollBottom() {
    var el = ta();
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    log('scrollBottom →', el.scrollTop, '/', el.scrollHeight);
  }
  function tabLooksActive() {
    var root = document.querySelector('#llama-log-viewer-root');
    if (!root) return true;
    var style = window.getComputedStyle(root);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return true;
  }
  function findClickable(sel) {
    var root = document.querySelector(sel);
    if (!root) {
      log('findClickable MISS', sel);
      return null;
    }
    if (root.tagName === 'BUTTON') {
      log('findClickable HIT button', sel);
      return root;
    }
    var inner = root.querySelector('button');
    if (inner) {
      log('findClickable HIT nested button', sel, root.tagName);
      return inner;
    }
    var roleBtn = root.querySelector('[role="button"]');
    if (roleBtn) {
      log('findClickable HIT role=button', sel);
      return roleBtn;
    }
    log('findClickable FALLBACK root', sel, root.tagName);
    return root;
  }
  function armNextPoll(delayMs) {
    if (pollArmTimer) {
      clearTimeout(pollArmTimer);
      pollArmTimer = null;
    }
    pollArmTimer = setTimeout(function () {
      pollArmTimer = null;
      tryPoll('loop');
    }, Math.max(0, delayMs || 0));
  }

  function endPollCycle(rearmMs, why) {
    if (pollWatchdog) {
      clearTimeout(pollWatchdog);
      pollWatchdog = null;
    }
    if (pollBusy) {
      pollBusy = false;
      log('endPollCycle', why || '', 'rearm=', rearmMs);
    }
    syncFooterFromTextarea();
    if (autoscrollOn() && stickBottom) scrollBottom();
    armNextPoll(rearmMs != null ? rearmMs : 120);
  }

  function tryPoll(reason) {
    pollAttemptN += 1;
    var n = pollAttemptN;
    if (pollBusy) {
      pollSkipN += 1;
      log('tryPoll#' + n, 'SKIP pollBusy reason=', reason || 'loop');
      return;
    }
    if (document.hidden || !tabLooksActive()) {
      pollSkipN += 1;
      log('tryPoll#' + n, 'SKIP inactive — retry 2s');
      armNextPoll(2000);
      return;
    }

    var btn = findClickable(POLL);
    if (!btn) {
      pollSkipN += 1;
      warn('tryPoll#' + n, 'NO POLL BUTTON');
      armNextPoll(2000);
      return;
    }

    pollBusy = true;
    pollClickN += 1;
    pollGen += 1;
    var myGen = pollGen;
    var prev = btn.style.pointerEvents;
    btn.style.pointerEvents = 'auto';
    log('tryPoll#' + n, 'LONG-POLL CLICK', {
      reason: reason || 'loop',
      clickN: pollClickN,
      holdMs: LONG_POLL_MS
    });
    try {
      btn.click();
    } catch (e) {
      err('tryPoll#' + n, 'click() THREW', e);
      btn.style.pointerEvents = prev;
      endPollCycle(1000, 'click-throw');
      return;
    }
    btn.style.pointerEvents = prev;

    /* Watchdog: server holds up to LONG_POLL_MS; re-arm after hold + slack */
    if (pollWatchdog) clearTimeout(pollWatchdog);
    pollWatchdog = setTimeout(function () {
      if (myGen !== pollGen) return;
      endPollCycle(80, 'watchdog');
    }, LONG_POLL_MS + LONG_POLL_SLACK_MS);
  }

  function onTextPossiblyUpdated() {
    syncFooterFromTextarea();
    if (suppressAutoscroll) {
      var t = ta();
      if (t) {
        var dh = t.scrollHeight - prevHeight;
        if (dh > 0) t.scrollTop = (t.scrollTop || 0) + dh;
        prevHeight = t.scrollHeight;
      }
      suppressAutoscroll = false;
      stickBottom = false;
      if (pollBusy) endPollCycle(80, 'text-load-older');
      ensureFooterFlush();
      return;
    }
    if (autoscrollOn() && stickBottom) scrollBottom();
    /* New content (or any value write) ends the long-poll cycle early */
    if (pollBusy) endPollCycle(80, 'text-updated');
    /* Gradio sometimes resets textarea height after update — fix only if gap */
    ensureFooterFlush();
  }

  function tryLoadOlder() {
    if (loadBusy) { log('tryLoadOlder SKIP loadBusy'); return; }
    var el = ta();
    if (!el || el.scrollTop >= 48) {
      log('tryLoadOlder SKIP', el ? ('scrollTop=' + el.scrollTop) : 'no-ta');
      return;
    }
    var btn = findClickable(LOAD);
    if (!btn) { warn('tryLoadOlder NO LOAD BUTTON'); return; }
    loadBusy = true;
    suppressAutoscroll = true;
    prevHeight = el.scrollHeight;
    log('tryLoadOlder CLICK', { scrollTop: el.scrollTop, prevHeight: prevHeight });
    var prev = btn.style.pointerEvents;
    btn.style.pointerEvents = 'auto';
    try { btn.click(); } catch (e) { err('tryLoadOlder click THREW', e); }
    btn.style.pointerEvents = prev;
    setTimeout(function () {
      loadBusy = false;
      log('tryLoadOlder loadBusy cleared');
      onTextPossiblyUpdated();
    }, 600);
  }

  function bind() {
    bindAttemptN += 1;
    var el = ta();
    if (!el) {
      if (bindAttemptN <= 3 || bindAttemptN % 10 === 0) {
        log('bind#' + bindAttemptN, 'textarea not found yet');
      }
      return;
    }
    if (el.dataset.llamaBound === '1') return;
    el.dataset.llamaBound = '1';
    stickBottom = nearBottom(el) || el.scrollTop === 0;
    log('bind#' + bindAttemptN, 'BOUND textarea', {
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
      stickBottom: stickBottom
    });
    layoutFill(true);
    syncFooterFromTextarea();

    el.addEventListener('scroll', function () {
      stickBottom = nearBottom(el);
      if (el.scrollTop < 48) tryLoadOlder();
    }, { passive: true });

    /* Lightweight MO — no layoutFill (that froze the UI in 1.6.9) */
    var mo = new MutationObserver(function () {
      requestAnimationFrame(onTextPossiblyUpdated);
    });
    mo.observe(el, { characterData: true, childList: true, subtree: true });

    try {
      var desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
      if (desc && desc.set) {
        var origSet = desc.set;
        Object.defineProperty(el, 'value', {
          get: function () { return desc.get.call(this); },
          set: function (v) {
            origSet.call(this, v);
            requestAnimationFrame(onTextPossiblyUpdated);
          },
          configurable: true
        });
        log('textarea value setter hooked');
      }
    } catch (e) {
      log('value hook failed', e);
    }
    log('MutationObserver attached (no layout spam)');
  }

  log('installing long-poll loop hold=' + LONG_POLL_MS + 'ms; layout flush-bottom');
  /* Bind retry only until bound — not every 400ms forever */
  var bindIv = setInterval(function () {
    bind();
    var el = ta();
    if (el && el.dataset.llamaBound === '1') {
      clearInterval(bindIv);
      layoutFill(true);
    }
  }, 500);
  window.addEventListener('resize', function () { layoutFillSoon(true); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      layoutFillSoon(true);
      if (!pollBusy) armNextPoll(200);
    }
  });
  /* Tab click often re-shows panel — re-measure after Gradio paints */
  document.addEventListener('click', function (ev) {
    try {
      var t = ev.target;
      if (!t) return;
      var s = (t.getAttribute && (t.getAttribute('role') || '')) + ' ' + (t.className || '');
      if (/tab/i.test(s) || (t.closest && t.closest('[role="tab"]'))) {
        setTimeout(function () { layoutFill(true); }, 80);
        setTimeout(function () { layoutFill(true); }, 300);
      }
    } catch (e) {}
  }, true);
  bind();
  layoutFill(true);
  /* Deferred fits: Gradio finishes tab layout late */
  setTimeout(function () { layoutFill(true); }, 200);
  setTimeout(function () { layoutFill(true); }, 700);
  setTimeout(function () { layoutFill(true); ensureFooterFlush(); }, 1500);
  setTimeout(function () {
    log('start long-poll loop');
    tryPoll('boot');
  }, 400);

  try {
    window.llamaLogViewerDebug = function () {
      dumpLlamaDom('manual');
      layoutFill(true);
      if (!pollBusy) tryPoll('manual-console');
      return {
        pollAttemptN: pollAttemptN,
        pollClickN: pollClickN,
        pollSkipN: pollSkipN,
        bindAttemptN: bindAttemptN,
        longPollMs: LONG_POLL_MS,
        pollBusy: pollBusy,
        version: VERSION,
        lastTextLen: lastText ? lastText.length : 0,
        booted: true
      };
    };
    log('window.llamaLogViewerDebug() registered');
  } catch (e) {
    err('failed to register llamaLogViewerDebug', e);
  }
})();
"""
    return (
        js.replace("__INTERVAL_MS__", str(interval_ms))
        .replace("__VERSION__", VERSION)
    )


def custom_js():
    # TextGen currently does NOT call this; kept for other hosts / future.
    body = _client_boot_js()
    return "() => { " + body + " }"


def _inline_boot_html() -> str:
    """Inject client boot via img onload (works when custom_js is ignored)."""
    b64 = base64.b64encode(_client_boot_js().encode("utf-8")).decode("ascii")
    gif = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    return (
        f'<img alt="" width="1" height="1" '
        f'style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0" '
        f'src="{gif}" '
        f'onload="try{{console.log(\'[llama_log_viewer] INLINE onload → eval boot v{VERSION}\');'
        f'eval(atob(\'{b64}\'));}}'
        f'catch(e){{console.error(\'[llama_log_viewer] INLINE eval failed\',e);}}" '
        f'onerror="console.error(\'[llama_log_viewer] INLINE onerror unexpected\')" />'
    )


def _click_kwargs():
    """
    Hide progress UI. queue=False is critical for long-poll:
    sleeping inside the handler must not block the global Gradio queue.
    """
    kw = {"show_progress": "hidden", "queue": False}
    try:
        import inspect

        sig = inspect.signature(gr.Button.click)
        params_ = sig.parameters
        if "show_progress" not in params_:
            kw.pop("show_progress", None)
        if "queue" not in params_:
            kw.pop("queue", None)
    except Exception:
        pass
    return kw


def ui():
    # Seed server-side view from disk once at UI build
    with _VIEW_LOCK:
        init_result, init_state = _poll_body(_default_state())
        if not isinstance(init_result, str):
            init_text = init_state.get("text") or " "
        else:
            init_text = _strip_ansi(init_result) or " "
        if init_state.get("content_ts") is None:
            init_state["content_ts"] = time.time()
        _set_view(init_state)

    header_html = """
<div id="llama-log-header">
  <span class="llama-log-title">Лог TextGen / llama-server</span>
  <label class="llama-log-as-label" for="llama-log-autoscroll-cb">
    <input type="checkbox" id="llama-log-autoscroll-cb" checked />
    Автоскролл
  </label>
</div>
""".strip()

    footer_html = """
<div id="llama-log-footer-static">
  Обновлено: <b id="llama-log-ts-content">—</b>
  &nbsp;&nbsp;&nbsp;
  Запись в файл: <b id="llama-log-ts-file">—</b>
</div>
""".strip()

    with gr.Column(elem_id="llama-log-viewer-root"):
        gr.HTML(value=header_html, elem_id="llama-log-header-wrap")

        poll_btn = gr.Button("poll", elem_id="llama-log-poll")
        load_older_btn = gr.Button("load_older", elem_id="llama-log-load-older")

        try:
            log_box = gr.Textbox(
                value=init_text,
                lines=4,
                max_lines=4,
                label="log",
                show_label=False,
                interactive=False,
                elem_id="llama-log-viewer-output",
            )
        except TypeError:
            log_box = gr.Textbox(
                value=init_text,
                lines=4,
                max_lines=4,
                label="log",
                interactive=False,
                elem_id="llama-log-viewer-output",
            )

        # Static footer — NOT in any click outputs → never re-rendered by poll
        gr.HTML(value=footer_html, elem_id="llama-log-footer-wrap")
        gr.HTML(value=_inline_boot_html(), elem_id="llama-log-inline-probe")

    click_kw = _click_kwargs()
    # Single output: textbox only. No gr.State, no footer → no HTML flash on idle poll.
    try:
        poll_btn.click(
            fn=poll_tick,
            inputs=None,
            outputs=[log_box],
            **click_kw,
        )
    except TypeError:
        poll_btn.click(
            fn=poll_tick,
            inputs=[],
            outputs=[log_box],
        )
    try:
        load_older_btn.click(
            fn=load_older,
            inputs=None,
            outputs=[log_box],
            **click_kw,
        )
    except TypeError:
        load_older_btn.click(
            fn=load_older,
            inputs=[],
            outputs=[log_box],
        )
