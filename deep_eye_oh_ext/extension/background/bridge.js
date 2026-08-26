'use strict';

// Thin bridge: pulls deepEyeOracle.snapshot() out of the diep.io tab's MAIN
// world at a fixed interval and forwards it as small structured JSON over a
// WebSocket to a local deep.eye.oh agent process. This is the only file in
// this extension permitted to touch the network or hold a background
// service-worker lifecycle -- extension/src/oracle.js (page-context,
// read-only observation) and extension/popup/popup.js are unaffected and
// remain free of any control/network primitives (see scripts/validate.ps1).
//
// Deliberately NOT a general-purpose message bus or protocol.
// browser-lifecycle-v0 narrows (does not remove) the prior "no inbound
// channel at all" invariant: on each connection this file now sends one
// `bridge_hello` and accepts back exactly one message TYPE in reply --
// `lifecycle_config` (validated player-name/game-mode only, see
// deep_eye_oh's browser_lifecycle.py) -- which it caches and exposes to
// src/lifecycle.js via a narrow chrome.runtime request/response, nothing
// else. It also forwards lifecycle.js's own read-only DOM observations
// outward as `lifecycle_snapshot`, alongside the existing `oracle_snapshot`.
// The agent process still cannot use this connection to make the extension
// run arbitrary code, execute a shell command, or perform gameplay input --
// see AGENTS.md.

const DEFAULT_BRIDGE_PORT = 8765;
const POLL_INTERVAL_MS = 100; // ~10Hz: fast enough for a simple farming loop
const MIN_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 8000;
const DIEP_URL_PATTERN = 'https://diep.io/*';
const BRIDGE_PROTOCOL_VERSION = 1;
const CAPABILITIES = Object.freeze(['oracle_snapshot', 'lifecycle_v0']);

// --- Pure helpers (unit-testable without chrome.*/WebSocket) --------------

function buildOutboundMessage(snapshot, tabId, polledAtMs) {
  return {
    type: 'oracle_snapshot',
    tabId,
    polledAtMs,
    snapshot,
  };
}

function buildBridgeHelloMessage() {
  return {
    type: 'bridge_hello',
    protocolVersion: BRIDGE_PROTOCOL_VERSION,
    capabilities: CAPABILITIES.slice(),
  };
}

function buildLifecycleSnapshotMessage(tabId, observedAtMs, snapshot) {
  return {
    type: 'lifecycle_snapshot',
    tabId,
    observedAtMs,
    snapshot,
  };
}

// Validates an inbound WebSocket message: the ONLY type ever accepted from
// Python is `lifecycle_config` with a string playerName/gameMode -- see
// module doc comment above. Returns the validated {playerName, gameMode}
// or null (never throws) so callers can log-and-drop exactly like every
// other malformed-input path in this extension.
function parseLifecycleConfigMessage(raw) {
  if (!raw || typeof raw !== 'object') {
    return null;
  }
  if (raw.type !== 'lifecycle_config') {
    return null;
  }
  if (typeof raw.playerName !== 'string' || raw.playerName.length < 1) {
    return null;
  }
  if (typeof raw.gameMode !== 'string' || raw.gameMode.length < 1) {
    return null;
  }
  return { playerName: raw.playerName, gameMode: raw.gameMode };
}

// Exponential backoff with a floor and cap; `attempt` is the number of
// consecutive failed/closed connections since the last successful one.
function nextReconnectDelayMs(attempt) {
  const delay = MIN_RECONNECT_DELAY_MS * (2 ** Math.max(0, attempt));
  return Math.min(delay, MAX_RECONNECT_DELAY_MS);
}

// Picks which diep.io tab to poll from a chrome.tabs.query() result:
// prefer the active tab in the current window (the one the operator is
// actually looking at and about to be controlled through), then fall back
// to any other matching tab, deterministically (lowest tab id) rather than
// whatever order chrome.tabs.query happened to return. Returns null if
// `tabs` is empty.
function pickTargetTab(tabs) {
  if (!Array.isArray(tabs) || tabs.length === 0) {
    return null;
  }
  const active = tabs.find((tab) => tab && tab.active);
  if (active) {
    return active;
  }
  return tabs.slice().sort((a, b) => (a.id ?? 0) - (b.id ?? 0))[0];
}

function bridgeUrl(port) {
  return `ws://127.0.0.1:${port}/`;
}

// --- Imperative glue (chrome.*/WebSocket -- exercised by live smoke test,
// not unit tests; see tests/bridge.test.js for what IS unit tested) -------

function createBridge(port = DEFAULT_BRIDGE_PORT) {
  let socket = null;
  let socketOpen = false;
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let pollTimer = null;
  let stopped = false;
  // The one piece of state Python is ever allowed to set here -- cached
  // per-connection-lifetime, re-requested via a fresh bridge_hello on
  // every (re)connect (see connect()'s 'open' handler) so a bridge
  // restarted on the Python side always re-delivers current config
  // rather than this extension coasting on a stale cached value forever.
  let cachedLifecycleConfig = null;

  function onMessageFromAgent(raw) {
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (_error) {
      return; // malformed inbound JSON -- dropped, same as any other bad input
    }
    const config = parseLifecycleConfigMessage(parsed);
    if (config) {
      cachedLifecycleConfig = config;
    }
    // Any other/unrecognized message type from the WebSocket is silently
    // ignored -- lifecycle_config is the ONLY inbound message type this
    // extension ever acts on (see module doc comment).
  }

  function onRuntimeMessage(message, sender, sendResponse) {
    if (!message || typeof message !== 'object') {
      return undefined;
    }
    if (message.type === 'get_lifecycle_config') {
      sendResponse({ config: cachedLifecycleConfig });
      return undefined; // synchronous response -- no need to keep the channel open
    }
    if (message.type === 'lifecycle_observed' && sender && sender.tab) {
      if (socketOpen && socket) {
        const outbound = buildLifecycleSnapshotMessage(sender.tab.id, message.observedAtMs, message.snapshot);
        socket.send(JSON.stringify(outbound));
      }
      return undefined;
    }
    return undefined;
  }

  function connect() {
    if (stopped) {
      return;
    }
    try {
      socket = new WebSocket(bridgeUrl(port));
    } catch (_error) {
      scheduleReconnect();
      return;
    }
    socket.addEventListener('open', () => {
      socketOpen = true;
      reconnectAttempt = 0;
      socket.send(JSON.stringify(buildBridgeHelloMessage()));
    });
    socket.addEventListener('message', (event) => {
      onMessageFromAgent(event.data);
    });
    socket.addEventListener('close', () => {
      socketOpen = false;
      socket = null;
      scheduleReconnect();
    });
    socket.addEventListener('error', () => {
      // 'close' always follows 'error' for a WebSocket; reconnect is
      // scheduled there, not here, to avoid a double-scheduled reconnect.
    });
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) {
      return;
    }
    const delay = nextReconnectDelayMs(reconnectAttempt);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  async function pollOnce() {
    if (stopped) {
      return;
    }
    try {
      const tabs = await chrome.tabs.query({ url: DIEP_URL_PATTERN });
      const tab = pickTargetTab(tabs);
      if (tab && socketOpen && socket) {
        const results = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: 'MAIN',
          func: () => (window.deepEyeOracle ? window.deepEyeOracle.snapshot() : null),
        });
        const snapshot = results && results[0] ? results[0].result : null;
        if (snapshot) {
          const message = buildOutboundMessage(snapshot, tab.id, Date.now());
          socket.send(JSON.stringify(message));
        }
      }
    } catch (_error) {
      // A closed tab, a navigated-away tab, or a transient executeScript
      // failure must not kill the poll loop -- just skip this tick. The
      // agent's own staleness check (see deep.eye.oh's browser_game_state)
      // is what makes a gap here fail closed downstream, not this catch.
    }
    pollTimer = setTimeout(pollOnce, POLL_INTERVAL_MS);
  }

  return {
    start() {
      stopped = false;
      connect();
      pollTimer = setTimeout(pollOnce, POLL_INTERVAL_MS);
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
        chrome.runtime.onMessage.addListener(onRuntimeMessage);
      }
    },
    stop() {
      stopped = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (socket) {
        socket.close();
        socket = null;
      }
      socketOpen = false;
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
        chrome.runtime.onMessage.removeListener(onRuntimeMessage);
      }
    },
  };
}

// Exposed for tests/bridge.test.js only (a plain global, mirroring
// deepEyeOracle's own approach of exposing itself for inspection) -- never
// used by the extension itself at runtime beyond this file. Registered
// unconditionally (cheap, side-effect-free) so a test harness can load this
// source without a chrome.* environment and exercise the pure helpers.
globalThis.__deepEyeBridgeInternals = {
  createBridge,
  buildOutboundMessage,
  buildBridgeHelloMessage,
  buildLifecycleSnapshotMessage,
  parseLifecycleConfigMessage,
  nextReconnectDelayMs,
  pickTargetTab,
  bridgeUrl,
  DEFAULT_BRIDGE_PORT,
  POLL_INTERVAL_MS,
  BRIDGE_PROTOCOL_VERSION,
  CAPABILITIES,
};

// Only auto-start in a real extension service-worker context (chrome.tabs
// present) -- never as a side effect of merely loading/parsing this source
// in a test harness (see tests/bridge.test.js).
if (typeof chrome !== 'undefined' && chrome.tabs && chrome.scripting) {
  const bridge = createBridge();
  bridge.start();
}
