'use strict';

// browser-overlay-control-v0: isolated-world in-page control overlay.
//
// This file is the ONLY overlay-related script that runs with chrome.*
// access (ISOLATED world, unlike oracle.js's page-context MAIN world) --
// it never touches the page-context Oracle global or diepAPI, never
// simulates game input, and never opens its own socket connection to
// anything. All it does is: render a
// Shadow DOM panel, relay typed text to background/bridge.js over a
// chrome.runtime port, and render whatever background/bridge.js relays
// back (bot_status / overlay_command_result / overlay_key_event). See
// CLAUDE.md's browser-overlay-control-v0 section (deep.eye.oh side) for
// the full design rationale, in particular why command entry never takes
// real DOM focus.
//
// Because this is a plain isolated-world content script attached to the
// page's normal DOM, oracle.js's hooked CanvasRenderingContext2D methods
// (MAIN world, canvas draw calls only) never see it -- no exclusion mask
// is needed or added (see AGENTS.md / CLAUDE.md's exclusion-region note).

(function overlayMain() {
  if (window.__deepEyeOverlayInstalled) {
    return; // a content script must never double-install on re-injection
  }
  window.__deepEyeOverlayInstalled = true;

  const MAX_LOG_LINES = 200;
  const MAX_HISTORY = 50;

  // ---------------------------------------------------------------------
  // Pure helpers (unit-testable without chrome.*/DOM -- see
  // tests/overlay.test.js for what IS unit tested)
  // ---------------------------------------------------------------------

  // Classifies one submitted line of overlay input text. Never invents a
  // fourth kind: ordinary text always reaches the bot as overlay_command,
  // a leading '/' is always local-only, a leading '!' is always refused
  // outright (never sent anywhere, never executed) -- the overlay must
  // never become a shell.
  function classifyInput(text) {
    const trimmed = text.trim();
    if (!trimmed) {
      return { kind: 'empty' };
    }
    if (trimmed.startsWith('!')) {
      return { kind: 'shell_refused', text: trimmed };
    }
    if (trimmed.startsWith('/')) {
      return { kind: 'local', text: trimmed.slice(1).trim() };
    }
    return { kind: 'bot_command', text };
  }

  function formatStatusLine(status) {
    if (!status || typeof status !== 'object') {
      return 'bot: no status yet';
    }
    // Renders exactly the fields browser_farming.py's push_status()
    // actually produces -- see CLAUDE.md/browser_farming.py's
    // run_farming_loop -- never an invented field.
    const parts = [
      status.connected ? 'connected' : 'disconnected',
      status.pausedByCommand ? 'paused' : 'running',
      `target=${status.target ?? 'none'}`,
    ];
    if (status.held && typeof status.held === 'object') {
      parts.push(`moving=${!!status.held.moving}`, `shooting=${!!status.held.shooting}`);
    }
    if (typeof status.snapshotAgeS === 'number') {
      parts.push(`age=${status.snapshotAgeS.toFixed(2)}s`);
    }
    if (typeof status.bulletSpeedPxS === 'number') {
      const confidence = typeof status.bulletSpeedConfidence === 'number'
        ? ` (${status.bulletSpeedConfidence.toFixed(2)})`
        : '';
      parts.push(`bullet=${status.bulletSpeedPxS.toFixed(0)}px/s${confidence}`);
    }
    return parts.join(' | ');
  }

  // Exposed for tests/overlay.test.js only (mirrors bridge.js's/oracle.js's
  // own __deepEye*Internals convention) -- never used by the overlay
  // itself at runtime beyond this file.
  globalThis.__deepEyeOverlayInternals = { classifyInput, formatStatusLine };

  // Do not install the imperative DOM/chrome.runtime half of this content
  // script in a sandbox with no `chrome`/`document.body` -- mirrors
  // bridge.js's own `typeof chrome !== 'undefined'` auto-start guard.
  if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.connect) {
    return;
  }

  // ---------------------------------------------------------------------
  // Imperative glue: Shadow DOM overlay, chrome.runtime port, physical
  // key-event relay -- exercised by the live smoke procedure, not unit
  // tests (mirrors bridge.js's createBridge()).
  // ---------------------------------------------------------------------

  let port = null;
  let overlayOpen = false;
  let focused = false;
  let buffer = '';
  const history = [];
  let historyIndex = -1;
  let lastStatus = null;
  let statusRenderScheduled = false;

  function ensurePort() {
    if (port) {
      return port;
    }
    port = chrome.runtime.connect({ name: 'deepEyeOverlay' });
    port.onMessage.addListener(handlePortMessage);
    port.onDisconnect.addListener(() => {
      port = null;
    });
    return port;
  }

  function sendPort(message) {
    try {
      ensurePort().postMessage(message);
    } catch (_error) {
      // Best-effort only -- mirrors browser_bridge.py's own _send(): a
      // disconnected/reloading background page must never throw into the
      // caller (see push_status/send_command_result's doc comments).
    }
  }

  function handlePortMessage(message) {
    if (!message || typeof message !== 'object') {
      return;
    }
    if (message.type === 'bot_status') {
      lastStatus = message;
      scheduleStatusRender();
    } else if (message.type === 'overlay_command_result') {
      appendLog(message.status === 'ok' ? 'ok' : message.status, `${message.text} -> ${message.message}`);
    } else if (message.type === 'overlay_key_event') {
      handleKeyEvent(message);
    }
  }

  // Throttled telemetry rendering: bot_status arrives already throttled
  // bot-side (see browser_farming.py's STATUS_PRINT_EVERY_N_TICKS), but
  // this coalesces any burst of updates into at most one paint per
  // animation frame rather than repainting per message.
  function scheduleStatusRender() {
    if (statusRenderScheduled) {
      return;
    }
    statusRenderScheduled = true;
    requestAnimationFrame(() => {
      statusRenderScheduled = false;
      renderStatus();
    });
  }

  function handleKeyEvent(event) {
    switch (event.kind) {
      case 'char':
        if (typeof event.value === 'string') {
          buffer += event.value;
          renderBuffer();
        }
        break;
      case 'backspace':
        buffer = buffer.slice(0, -1);
        renderBuffer();
        break;
      case 'enter':
        submitBuffer();
        break;
      case 'escape':
        setFocused(false);
        break;
      case 'up':
        navigateHistory(-1);
        break;
      case 'down':
        navigateHistory(1);
        break;
      case 'tilde':
        // The physical hook consumed the backtick while it had it
        // suppressed (see physical_keyboard_hook.py's translate_key) --
        // the browser's own keydown listener below never sees it in this
        // mode, so this relay IS the toggle-closed path while focused.
        closeOverlay();
        break;
      default:
        break;
    }
  }

  function navigateHistory(direction) {
    if (history.length === 0) {
      return;
    }
    const next = historyIndex + direction;
    if (next < 0 || next >= history.length) {
      return;
    }
    historyIndex = next;
    buffer = history[historyIndex];
    renderBuffer();
  }

  function submitBuffer() {
    const text = buffer;
    buffer = '';
    renderBuffer();
    const classified = classifyInput(text);
    if (classified.kind === 'empty') {
      return;
    }
    history.push(text);
    if (history.length > MAX_HISTORY) {
      history.shift();
    }
    historyIndex = history.length;

    if (classified.kind === 'shell_refused') {
      appendLog('refused', `shell commands are refused: ${classified.text}`);
    } else if (classified.kind === 'local') {
      runLocalCommand(classified.text);
    } else {
      appendLog('sent', text);
      sendPort({ type: 'overlay_command', text: classified.text });
    }
  }

  function runLocalCommand(rest) {
    const [verb, ...args] = rest.split(/\s+/).filter(Boolean);
    switch ((verb || '').toLowerCase()) {
      case 'clear':
        clearLog();
        break;
      case 'close':
        closeOverlay();
        break;
      case 'help':
        appendLog('local', 'local commands: /clear /close /help -- anything else is sent to the bot, !... is refused');
        break;
      default:
        appendLog('local', `unknown local command: /${rest}`);
        break;
    }
    void args;
  }

  // ---------------------------------------------------------------------
  // Shadow DOM construction
  // ---------------------------------------------------------------------

  let shadowRoot = null;
  let logEl = null;
  let bufferEl = null;
  let statusEl = null;
  let panelEl = null;

  function buildOverlay() {
    const host = document.createElement('div');
    host.id = 'deep-eye-oh-overlay-host';
    host.style.all = 'initial';
    // A closed shadow root keeps the page's own scripts from reading or
    // mutating this UI -- oracle.js/diepAPI never need to, and the game
    // page is otherwise untrusted with respect to this UI's state.
    shadowRoot = host.attachShadow({ mode: 'closed' });

    const style = document.createElement('style');
    style.textContent = `
      :host { all: initial; }
      .panel {
        position: fixed; left: 12px; bottom: 12px; z-index: 2147483647;
        width: 420px; max-width: calc(100vw - 24px);
        font: 12px/1.4 ui-monospace, Consolas, monospace;
        background: rgba(12, 14, 18, 0.88); color: #d8e0ea;
        border: 1px solid rgba(255,255,255,0.15); border-radius: 6px;
        padding: 8px; display: none; box-shadow: 0 4px 16px rgba(0,0,0,0.4);
      }
      .panel.open { display: block; }
      .status { color: #8fd0ff; margin-bottom: 6px; white-space: pre-wrap; word-break: break-word; }
      .log { max-height: 160px; overflow-y: auto; margin-bottom: 6px; }
      .log div { white-space: pre-wrap; word-break: break-word; }
      .log .sent { color: #d8e0ea; }
      .log .ok { color: #7be08a; }
      .log .rejected, .log .unsupported { color: #e0a85a; }
      .log .refused { color: #e06060; }
      .log .local { color: #a0a8b8; }
      .inputRow { display: flex; align-items: center; }
      .prompt { color: #7be08a; margin-right: 4px; }
      .buffer { flex: 1; white-space: pre-wrap; word-break: break-word; }
      .cursor { display: inline-block; width: 6px; background: #d8e0ea; margin-left: 1px; }
      .unfocused .cursor { visibility: hidden; }
      .hint { color: #6a7280; margin-top: 4px; }
    `;

    panelEl = document.createElement('div');
    panelEl.className = 'panel';

    statusEl = document.createElement('div');
    statusEl.className = 'status';
    statusEl.textContent = 'bot: no status yet';

    logEl = document.createElement('div');
    logEl.className = 'log';

    const inputRow = document.createElement('div');
    inputRow.className = 'inputRow unfocused';
    const prompt = document.createElement('span');
    prompt.className = 'prompt';
    prompt.textContent = '>';
    bufferEl = document.createElement('span');
    bufferEl.className = 'buffer';
    const cursor = document.createElement('span');
    cursor.className = 'cursor';
    cursor.textContent = ' ';
    inputRow.appendChild(prompt);
    inputRow.appendChild(bufferEl);
    inputRow.appendChild(cursor);
    inputRow.id = 'deep-eye-oh-overlay-input-row';

    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'click to type · text → bot · /cmd → local · !x refused · ` closes';

    panelEl.appendChild(statusEl);
    panelEl.appendChild(logEl);
    panelEl.appendChild(inputRow);
    panelEl.appendChild(hint);
    shadowRoot.appendChild(style);
    shadowRoot.appendChild(panelEl);

    inputRow.addEventListener('click', () => setFocused(true));

    (document.documentElement || document.body).appendChild(host);
  }

  function renderBuffer() {
    if (!bufferEl) {
      return;
    }
    bufferEl.textContent = buffer;
  }

  function renderStatus() {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = formatStatusLine(lastStatus);
  }

  function appendLog(kind, text) {
    if (!logEl) {
      return;
    }
    const line = document.createElement('div');
    line.className = kind;
    line.textContent = text;
    logEl.appendChild(line);
    while (logEl.childNodes.length > MAX_LOG_LINES) {
      logEl.removeChild(logEl.firstChild);
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function clearLog() {
    if (logEl) {
      logEl.textContent = '';
    }
  }

  function setOverlayVisible(visible) {
    overlayOpen = visible;
    if (panelEl) {
      panelEl.classList.toggle('open', visible);
    }
  }

  function setFocused(next) {
    if (focused === next) {
      return;
    }
    focused = next;
    const inputRow = shadowRoot && shadowRoot.getElementById('deep-eye-oh-overlay-input-row');
    if (inputRow) {
      inputRow.classList.toggle('unfocused', !next);
    }
    // Drives PhysicalKeyboardCapture's lifecycle bot-side (see
    // physical_keyboard_hook.py) -- this is the ONLY focus signal this
    // overlay ever sends; no DOM element is ever given real focus() (see
    // module docstring for why).
    sendPort({ type: 'overlay_focus', focused: next });
  }

  function openOverlay() {
    setOverlayVisible(true);
    setFocused(true);
  }

  function closeOverlay() {
    setFocused(false);
    setOverlayVisible(false);
  }

  buildOverlay();

  // Global backtick-open/close listener. This only ever fires when the
  // physical hook is NOT active (i.e. when the overlay does not currently
  // have focus) -- while focused, physical keystrokes (backtick included)
  // are suppressed at the OS level and never reach this DOM listener at
  // all (see handleKeyEvent's 'tilde' case for that path instead).
  // KeyboardEvent.code identifies the PHYSICAL key regardless of the
  // active keyboard layout (e.g. Cyrillic), unlike event.key which would
  // report the layout's mapped character (e.g. 'ё').
  window.addEventListener('keydown', (event) => {
    if (event.code !== 'Backquote' || focused) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (overlayOpen) {
      closeOverlay();
    } else {
      openOverlay();
    }
  }, true);
})();
