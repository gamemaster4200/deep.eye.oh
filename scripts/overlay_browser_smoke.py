"""overlay-control-center-v0: automated dev/integration smoke for the
Control Center overlay, end to end -- real Chrome for Testing, the real
unpacked deep_eye_oh_ext extension (oracle.js/lifecycle.js/overlay.js/
background/bridge.js, unmodified), a real BrowserBridgeServer, and the
real overlay_dev_backend.py, all wired together exactly as they would be
for genuine dev use. Not part of pytest/validate.ps1 -- this launches a
real browser process and is meant to be run manually.

Scope, deliberately: this proves the Control Center's OWN wiring (the
part explicitly excluded from unit tests -- Shadow DOM/chrome.runtime
port/physical keydown listener "imperative glue", see overlay.js's and
bridge.test.js's own doc comments) actually works live. It does NOT
touch diep.io, farming, targeting, or Controller actuation: the page
navigated to is a local HTTPS stub, reached by mapping the diep.io
hostname to 127.0.0.1 via Chrome's own --host-resolver-rules test flag
(so the extension's exact "https://diep.io/*" match pattern is exercised
for real, without ever contacting the real game) -- see
_launch_chrome_against_local_stub()'s doc comment. overlay_dev_backend.py
is the real, already-merged mock backend -- this script does not
reimplement any dispatch/protocol logic of its own.

One thing this script CANNOT prove, by construction, and does not claim
to: the literal physical-hardware-vs-SendInput distinction
(WH_KEYBOARD_LL's LLKHF_INJECTED flag) requires an actual human keystroke
on actual hardware. Every keystroke this script "types" -- whether fed
via BrowserBridgeServer.push_key_event() (simulating what
physical_keyboard_hook.py would have produced from a real keystroke) or
via a CDP-dispatched synthetic DOM KeyboardEvent for the backtick toggle
-- is not real hardware input, so it cannot exercise
PhysicalKeyboardCapture's own physical-vs-injected branch point itself.
See physical_keyboard_hook.py's own test suite (test_hook_proc_passes_
through_injected_events et al.) for that branch's coverage from the
Windows-API side, and this script's final report for the one residual
manual-hardware check.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.sync.client import connect as ws_connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deep_eye_oh import browser_runtime, paths  # noqa: E402
from deep_eye_oh.overlay_dev_backend import run_overlay_dev_backend  # noqa: E402
from deep_eye_oh.physical_keyboard_hook import KeyEvent  # noqa: E402

BRIDGE_PORT = 8765  # must match background/bridge.js's DEFAULT_BRIDGE_PORT
STUB_HOST = "diep.io"


class _Fail(AssertionError):
    """A smoke-check assertion failed -- caught at top level to print a
    clean PASS/FAIL report instead of a raw traceback for the expected
    failure modes."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise _Fail(message)


# ---------------------------------------------------------------------------
# Local HTTPS stub for https://diep.io/* -- see module doc comment.
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        body = b"<!doctype html><html><head><title>overlay-smoke-stub</title></head><body></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # keep stdout clean
        pass


def _generate_self_signed_cert(cert_dir: Path) -> tuple[Path, Path]:
    cert_path = cert_dir / "stub.crt"
    key_path = cert_dir / "stub.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "2", "-subj", f"/CN={STUB_HOST}",
            "-addext", f"subjectAltName=DNS:{STUB_HOST}",
        ],
        check=True, capture_output=True, text=True,
    )
    return cert_path, key_path


def _start_stub_server(cert_path: Path, key_path: Path) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Minimal CDP client -- just enough JSON-RPC-over-WebSocket to evaluate one
# expression and read devtools target lists. Deliberately not a general
# framework (see module doc comment): no Playwright/Puppeteer/Selenium is
# installed in this environment, and this slice needs exactly one thing
# from a real browser -- dispatching one DOM keydown -- everything else is
# observed over the real WebSocket bridge instead.
# ---------------------------------------------------------------------------


def _http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for_devtools(port: int, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        try:
            _http_json(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
            return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise _Fail(f"Chrome DevTools endpoint on port {port} never came up: {last_error}")


def _find_targets(port: int) -> list[dict]:
    return _http_json(f"http://127.0.0.1:{port}/json/list", timeout=2.0)


class CDPSession:
    """One WebSocket connection to a single devtools target, with just
    enough request/response correlation and event buffering to drive a
    handful of Runtime.* calls."""

    def __init__(self, ws_url: str) -> None:
        self._ws = ws_connect(ws_url, max_size=None, open_timeout=10)
        self._next_id = 1
        self._events: list[dict] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise _Fail(f"CDP {method} error: {data['error']}")
                return data.get("result", {})
            if "method" in data:
                self._events.append(data)
        raise _Fail(f"no CDP response for {method} within {timeout}s")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 -- best-effort teardown only
            pass


# ---------------------------------------------------------------------------
# Bridge-side observation -- monkeypatch the REAL BrowserBridgeServer/
# PhysicalKeyboardCapture instances overlay_dev_backend constructs, purely
# to record what they already do (never changing their behavior).
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.drained_commands: list = []
        self.sent_results: list = []
        self.pushed_statuses: list = []
        self.hook_start_calls = 0
        self.hook_stop_calls = 0
        self.bridge = None
        self.ready = threading.Event()

    def on_ready(self, bridge) -> None:
        self.bridge = bridge

        orig_pop = bridge.pop_commands

        def pop_wrapper():
            cmds = orig_pop()
            self.drained_commands.extend(cmds)
            return cmds

        bridge.pop_commands = pop_wrapper

        orig_send_result = bridge.send_command_result

        def send_result_wrapper(command, result):
            self.sent_results.append((command, result))
            return orig_send_result(command, result)

        bridge.send_command_result = send_result_wrapper

        orig_push_status = bridge.push_status

        def push_status_wrapper(status):
            self.pushed_statuses.append(status)
            return orig_push_status(status)

        bridge.push_status = push_status_wrapper

        orig_hook_start = bridge._physical_keyboard.start
        orig_hook_stop = bridge._physical_keyboard.stop

        def hook_start_wrapper():
            # Record AFTER the real call completes, not before -- stop()
            # can block on Thread.join(timeout=2.0), so recording first
            # would let a poller observe "stopped" while the real
            # underlying call (and its Windows hook teardown) is still
            # in flight.
            result = orig_hook_start()
            self.hook_start_calls += 1
            return result

        def hook_stop_wrapper(timeout=None):
            result = orig_hook_stop(timeout) if timeout is not None else orig_hook_stop()
            self.hook_stop_calls += 1
            return result

        bridge._physical_keyboard.start = hook_start_wrapper
        bridge._physical_keyboard.stop = hook_stop_wrapper

        self.ready.set()


def _poll(predicate, timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def type_and_submit(bridge, text: str) -> None:
    for ch in text:
        bridge.push_key_event(KeyEvent(kind="char", value=ch))
        time.sleep(0.005)
    bridge.push_key_event(KeyEvent(kind="enter"))


# ---------------------------------------------------------------------------
# Main smoke sequence
# ---------------------------------------------------------------------------


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, fn) -> None:
        try:
            fn()
            results.append((name, True, ""))
            print(f"PASS  {name}")
        except _Fail as exc:
            results.append((name, False, str(exc)))
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- surfaced in the report either way
            results.append((name, False, f"unexpected {type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: unexpected {type(exc).__name__}: {exc}")

    tmp = Path(tempfile.mkdtemp(prefix="overlay-smoke-"))
    cert_path, key_path = _generate_self_signed_cert(tmp)
    httpd = _start_stub_server(cert_path, key_path)
    stub_port = httpd.server_address[1]
    print(f"stub https://{STUB_HOST}:{stub_port}/ serving from {tmp}")

    recorder = Recorder()
    backend_thread = threading.Thread(
        target=run_overlay_dev_backend,
        kwargs=dict(
            port=BRIDGE_PORT, tick_interval_s=0.02, status_push_every_n_ticks=15,
            max_ticks=3000, on_ready=recorder.on_ready,
        ),
        daemon=True,
    )
    backend_thread.start()
    _check(recorder.ready.wait(timeout=5.0), "overlay_dev_backend did not start in time")
    bridge = recorder.bridge

    chrome_process = None
    cdp = None
    try:
        chrome_exe = browser_runtime.find_or_download_chrome()
        extension_dir = paths.resolve_extension_dir()
        profile = tmp / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        browser_runtime._prepare_profile_for_launch(profile)

        devtools_port = 9333
        argv = browser_runtime.build_chrome_argv(
            chrome_exe, extension_dir, profile=profile, url=f"https://{STUB_HOST}:{stub_port}/"
        )
        # Test-only additions, inserted before the trailing URL: redirect
        # ONLY DNS resolution of the diep.io hostname to our local stub
        # (see module doc comment -- the extension's real
        # "https://diep.io/*" match pattern is what's under test, but
        # nothing here ever reaches the real diep.io servers), tolerate
        # our self-signed cert, run headless (this launch doesn't need a
        # visible window), and expose a devtools port for the one CDP call
        # this script makes.
        extra_flags = [
            f"--remote-debugging-port={devtools_port}",
            f"--host-resolver-rules=MAP {STUB_HOST} 127.0.0.1",
            "--ignore-certificate-errors",
            "--headless=new",
            "--disable-gpu",
        ]
        argv[-1:-1] = extra_flags
        chrome_process = subprocess.Popen(argv)
        print(f"launched Chrome for Testing pid={chrome_process.pid}")

        _wait_for_devtools(devtools_port)

        def _extension_loaded() -> bool:
            targets = _find_targets(devtools_port)
            return any(t.get("type") == "service_worker" and t.get("url", "").startswith("chrome-extension://") for t in targets)

        record("extension loads (background service worker present)", lambda: _check(
            _poll(_extension_loaded, timeout_s=15.0), "no chrome-extension:// service_worker target appeared"
        ))

        record("bridge connection establishes (has_connected())", lambda: _check(
            _poll(bridge.has_connected, timeout_s=15.0), "BrowserBridgeServer.has_connected() never went True"
        ))

        def _get_page_ws_url() -> str:
            targets = _find_targets(devtools_port)
            pages = [t for t in targets if t.get("type") == "page" and t.get("url", "").startswith(f"https://{STUB_HOST}")]
            _check(len(pages) >= 1, f"no page target for https://{STUB_HOST}/* found (targets: {targets})")
            return pages[0]["webSocketDebuggerUrl"]

        page_ws_url = _get_page_ws_url()
        cdp = CDPSession(page_ws_url)
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        def dispatch_backquote_keydown() -> None:
            expr = (
                "window.dispatchEvent(new KeyboardEvent('keydown', "
                "{code: 'Backquote', key: '`', bubbles: true, cancelable: true})); true"
            )
            out = cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            _check("exceptionDetails" not in out, f"JS exception dispatching Backquote keydown: {out.get('exceptionDetails')}")
            _check(out.get("result", {}).get("value") is True, "Backquote keydown dispatch did not return true")

        def open_overlay_via_backquote(expected_start_calls: int, timeout_s: float = 5.0) -> None:
            dispatch_backquote_keydown()
            ok = _poll(lambda: recorder.hook_start_calls == expected_start_calls, timeout_s=timeout_s)
            if not ok:
                print(f"    [diag] devtools targets: {_find_targets(devtools_port)}")
                print(f"    [diag] bridge._connection: {bridge._connection!r}")
                print(f"    [diag] bridge.has_connected(): {bridge.has_connected()}")
            _check(
                ok,
                f"PhysicalKeyboardCapture.start() call count did not reach {expected_start_calls} "
                f"(got {recorder.hook_start_calls}) -- overlay content script toggle listener, "
                "overlay_focus relay, or the bridge's overlay_focus handling did not fire correctly",
            )
            _check(bridge._physical_keyboard.active, "hook reports inactive right after start() was observed")

        record(
            "overlay content script installed + Backquote toggle opens overlay + overlay_focus starts PhysicalKeyboardCapture (real Windows hook)",
            lambda: open_overlay_via_backquote(expected_start_calls=1),
        )

        def run_typed_command(text: str, expected_status: str, expected_message_contains: str | None = None) -> None:
            before = len(recorder.sent_results)
            type_and_submit(bridge, text)
            _check(_poll(lambda: len(recorder.sent_results) > before, timeout_s=3.0), f"no overlay_command_result observed for {text!r}")
            command, result = recorder.sent_results[-1]
            _check(command.text == text, f"expected drained command text {text!r}, got {command.text!r}")
            _check(result.status == expected_status, f"{text!r}: expected status {expected_status!r}, got {result.status!r}")
            if expected_message_contains is not None:
                _check(expected_message_contains in result.message, f"{text!r}: expected {expected_message_contains!r} in message {result.message!r}")

        def run_locally_handled(text: str) -> None:
            before = len(recorder.drained_commands)
            type_and_submit(bridge, text)
            time.sleep(0.5)  # give a wrongly-forwarded command a chance to arrive
            _check(len(recorder.drained_commands) == before, f"{text!r} reached the backend as overlay_command -- must be handled locally/refused, never forwarded")

        record("ordinary text -> backend command, pause", lambda: run_typed_command("pause", "ok", "paused"))
        record("ordinary text -> backend command, resume", lambda: run_typed_command("resume", "ok", "resumed"))
        record("unsupported command response", lambda: run_typed_command("mode farm", "unsupported"))
        record("/help handled locally, never reaches backend", lambda: run_locally_handled("/help"))
        record("/clear handled locally, never reaches backend", lambda: run_locally_handled("/clear"))
        record("!... refused locally, never reaches backend", lambda: run_locally_handled("!rm -rf /"))

        def close_via_local_command() -> None:
            before_stop = recorder.hook_stop_calls
            type_and_submit(bridge, "/close")
            _check(_poll(lambda: recorder.hook_stop_calls > before_stop, timeout_s=5.0), "PhysicalKeyboardCapture.stop() was not observed after /close")
            _check(_poll(lambda: not bridge._physical_keyboard.active, timeout_s=5.0), "hook still active after /close")

        record("/close handled locally and stops PhysicalKeyboardCapture", close_via_local_command)

        record(
            "reopen via Backquote does not duplicate the toggle listener (start-call count increments by exactly 1)",
            lambda: open_overlay_via_backquote(expected_start_calls=2),
        )

        def close_via_tilde_relay() -> None:
            before_stop = recorder.hook_stop_calls
            bridge.push_key_event(KeyEvent(kind="tilde"))
            _check(_poll(lambda: recorder.hook_stop_calls > before_stop, timeout_s=5.0), "PhysicalKeyboardCapture.stop() was not observed after a relayed tilde keystroke")
            _check(_poll(lambda: not bridge._physical_keyboard.active, timeout_s=5.0), "hook still active after tilde-relay close")

        record("physical-hook 'tilde' relay closes overlay and stops PhysicalKeyboardCapture", close_via_tilde_relay)

        def bot_status_reaches_wire() -> None:
            before = len(recorder.pushed_statuses)
            _check(_poll(lambda: len(recorder.pushed_statuses) > before, timeout_s=3.0), "no bot_status push observed")
            status = recorder.pushed_statuses[-1]
            _check(status.get("connected") is True, f"bot_status.connected was not True: {status}")

        record("bot_status telemetry pushed over the bridge (connected=true)", bot_status_reaches_wire)

        def dropped_connection_stops_hook_and_reconnects() -> None:
            open_overlay_via_backquote(expected_start_calls=3)
            old_connection = bridge._connection
            _check(old_connection is not None, "no active bridge connection to drop")
            before_stop = recorder.hook_stop_calls
            old_connection.close()
            _check(
                _poll(lambda: recorder.hook_stop_calls > before_stop, timeout_s=5.0),
                "dropping the connection did not stop PhysicalKeyboardCapture",
            )
            _check(not bridge._physical_keyboard.active, "hook still active immediately after connection drop")
            _check(
                _poll(lambda: bridge._connection is not None and bridge._connection is not old_connection, timeout_s=10.0),
                "background/bridge.js did not reconnect within its own backoff window",
            )

        record(
            "dropped WebSocket connection stops PhysicalKeyboardCapture, and the extension reconnects on its own",
            dropped_connection_stops_hook_and_reconnects,
        )

        def reopen_after_reconnect() -> None:
            open_overlay_via_backquote(expected_start_calls=4)
            bridge.push_key_event(KeyEvent(kind="tilde"))
            _check(_poll(lambda: not bridge._physical_keyboard.active, timeout_s=5.0), "hook still active after final close")

        record("overlay still opens/closes correctly after a reconnect (no duplicated state)", reopen_after_reconnect)

    finally:
        if cdp is not None:
            cdp.close()
        if bridge is not None:
            try:
                bridge._physical_keyboard.stop()
            except Exception:  # noqa: BLE001 -- last-resort safety net only
                pass
            bridge.stop()
        if chrome_process is not None:
            browser_runtime.terminate_chrome(chrome_process)
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    print(f"\n{passed}/{len(results)} smoke checks passed.")
    if failed:
        print("FAILURES:")
        for name, _, message in failed:
            print(f"  - {name}: {message}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
