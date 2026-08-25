'use strict';

// browser-lifecycle-v0: isolated-world content script (document_start,
// https://diep.io/*). Observes diep.io's OWN pre-game/lobby/death DOM,
// classifies which lifecycle state we're in, forwards small JSON
// snapshots to the background bridge (background/bridge.js), and applies
// at most one narrowly-whitelisted lobby/death UI action per tick using
// the config the bridge already cached from Python (see
// requestLifecycleConfig below).
//
// This is the ONE narrow, explicitly reviewed exception to this
// extension's read-only invariant (see AGENTS.md): it may set the
// player-name input, select the configured game mode, and click the
// known Play/respawn controls. It NEVER performs gameplay movement/aim/
// shoot/upgrade input (that stays exclusively in deep.eye.oh's own
// Controller, driven by real OS-level input, never DOM events), and it
// NEVER interacts with CAPTCHA controls in any way -- CAPTCHA is
// detection-only, gated entirely on read-only DOM observation.
//
// Every selector/behavior below is grounded in live reconnaissance
// against the real https://diep.io/ site (Chrome for Testing + raw CDP --
// see this slice's PR description), not guessed:
//   * diep.io's own UI lives under #screen-holder, which has exactly one
//     `.screen.active` child at a time: #loading-screen, #home-screen
//     (the lobby), #in-game-screen, #game-over-screen, plus an
//     #status-message-screen for blocking status text.
//   * #home-screen additionally carries an `x-state` attribute and a
//     `loading-error` class modifier while not yet actionable (observed
//     live after a "kicked due to inactivity" disconnect) -- this is
//     already covered by simply waiting for #spawn-button to become
//     enabled, so it does not need its own top-level lifecycle state.
//   * The real CAPTCHA/anti-bot provider is Cloudflare Turnstile (not
//     Google reCAPTCHA -- an unrelated, always-invisible ad-network
//     iframe is also present in the DOM and is NOT what gates play),
//     triggered server-side on a join attempt. Once rendered, Turnstile
//     always injects an iframe sourced from challenges.cloudflare.com --
//     a stable, external, non-spoofable signal, used here for detection
//     only.
//   * No blocking ad banner was ever observed live -- #ad-holders stayed
//     invisible throughout every captured state, and no dismiss control
//     was ever needed to reach the mode selector / name input / Play
//     button. No generic ad-dismiss logic is implemented here; if a real
//     blocking ad is ever found live, add its exact, specific dismiss
//     control the same way every other action here was added -- never a
//     generic "click anything that looks like a close button" rule.

const LIFECYCLE_STATES = Object.freeze({
  UNKNOWN: 'UNKNOWN',
  LOADING: 'LOADING',
  CAPTCHA_REQUIRED: 'CAPTCHA_REQUIRED',
  LOBBY: 'LOBBY',
  ENTERING_GAME: 'ENTERING_GAME',
  PLAYING: 'PLAYING',
  DEAD: 'DEAD',
});

const POLL_INTERVAL_MS = 150; // ~6-7Hz -- within the requested 5-10Hz band
const ACTION_COOLDOWN_MS = 600; // debounce between applying any one DOM action
const CAPTCHA_IFRAME_SELECTOR = 'iframe[src^="https://challenges.cloudflare.com/"]';

// --- Pure helpers (unit-testable without a real DOM -- see
// tests/lifecycle.test.js, which loads this source into a vm sandbox with
// small fake `document`-like objects, mirroring tests/bridge.test.js) -----

function hasClass(el, cls) {
  return !!(el && typeof el.className === 'string' && el.className.split(/\s+/).includes(cls));
}

function isVisible(el) {
  if (!el || typeof el.getBoundingClientRect !== 'function') {
    return false;
  }
  const rect = el.getBoundingClientRect();
  return !!rect && rect.width > 0 && rect.height > 0;
}

// Collects raw DOM signals into one small plain object -- the only
// function here that touches `doc` directly. Accepts a `doc` parameter
// (rather than reading the global `document`) so tests can pass a fake.
function collectSignals(doc) {
  const holder = doc.getElementById('screen-holder');
  const homeScreen = doc.getElementById('home-screen');
  const gameOverScreen = doc.getElementById('game-over-screen');
  const inGameScreen = doc.getElementById('in-game-screen');
  const loadingScreen = doc.getElementById('loading-screen');
  const spawnButton = doc.getElementById('spawn-button');
  const nicknameInput = doc.getElementById('spawn-nickname');
  const modeSelected = doc.querySelector && doc.querySelector('#gamemode-selector .selected');
  const captchaIframe = doc.querySelector && doc.querySelector(CAPTCHA_IFRAME_SELECTOR);

  const anyScreenActive = [homeScreen, gameOverScreen, inGameScreen, loadingScreen].some((s) => hasClass(s, 'active'));

  return {
    screenHolderPresent: !!holder,
    anyScreenActive,
    homeActive: hasClass(homeScreen, 'active'),
    homeLoadingError: hasClass(homeScreen, 'loading-error'),
    gameOverActive: hasClass(gameOverScreen, 'active'),
    inGameActive: hasClass(inGameScreen, 'active'),
    loadingActive: hasClass(loadingScreen, 'active'),
    spawnButtonDisabled: !spawnButton || !!spawnButton.disabled,
    captchaVisible: isVisible(captchaIframe),
    selectedMode: modeSelected && typeof modeSelected.getAttribute === 'function' ? modeSelected.getAttribute('data-value') : null,
    nicknameValue: nicknameInput ? (nicknameInput.value ?? null) : null,
    nicknameReadonly: !!(nicknameInput && nicknameInput.readOnly),
  };
}

// Precedence, most authoritative first -- CAPTCHA_REQUIRED, DEAD, LOBBY,
// ENTERING_GAME, PLAYING, LOADING, UNKNOWN (see PR description). Never
// classifies PLAYING merely because no known menu is visible -- that is
// exactly what ENTERING_GAME is for (a real, positively-observed
// transient: the screen-holder machinery exists but the exclusive
// `.screen.active` flag has briefly moved off every known screen, the
// live-observed gap between leaving #home-screen and #in-game-screen
// becoming active).
function classifyState(signals) {
  if (signals.captchaVisible) {
    return { state: LIFECYCLE_STATES.CAPTCHA_REQUIRED, reason: 'turnstile_iframe_visible' };
  }
  if (signals.gameOverActive) {
    return { state: LIFECYCLE_STATES.DEAD, reason: 'game_over_screen_active' };
  }
  if (signals.homeActive) {
    if (signals.spawnButtonDisabled) {
      return {
        state: LIFECYCLE_STATES.LOBBY,
        reason: signals.homeLoadingError ? 'home_screen_connection_error' : 'home_screen_not_ready',
      };
    }
    return { state: LIFECYCLE_STATES.LOBBY, reason: 'home_screen_ready' };
  }
  if (signals.screenHolderPresent && !signals.anyScreenActive) {
    return { state: LIFECYCLE_STATES.ENTERING_GAME, reason: 'no_known_screen_active' };
  }
  if (signals.inGameActive) {
    return { state: LIFECYCLE_STATES.PLAYING, reason: 'in_game_screen_active' };
  }
  if (signals.loadingActive) {
    return { state: LIFECYCLE_STATES.LOADING, reason: 'loading_screen_active' };
  }
  return { state: LIFECYCLE_STATES.UNKNOWN, reason: 'no_screen_holder' };
}

function classifyLifecycle(doc) {
  const signals = collectSignals(doc);
  const classification = classifyState(signals);
  return {
    state: classification.state,
    reason: classification.reason,
    selectedMode: signals.selectedMode,
    signals, // not sent over the bridge -- see buildLifecycleSnapshotMessage; kept for planAction/tests
  };
}

// Chooses AT MOST ONE permitted action per tick -- never more than one,
// never a gameplay action, never a CAPTCHA interaction. `config` is
// {playerName, gameMode} (or null if none has arrived from Python yet --
// in which case this never guesses a name/mode of its own and simply
// does nothing). `nowMs`/`lastActionAppliedAtMs` implement the
// debounce/cooldown: repeated ticks with already-matching signals (or a
// too-recent previous action) produce null, so the same action is never
// fired twice in a row for no new reason.
function planAction(classification, config, nowMs, lastActionAppliedAtMs) {
  if (!config) {
    return null;
  }
  if (lastActionAppliedAtMs !== null && nowMs - lastActionAppliedAtMs < ACTION_COOLDOWN_MS) {
    return null;
  }

  const signals = classification.signals;

  if (classification.state === LIFECYCLE_STATES.LOBBY) {
    if (signals.nicknameValue !== config.playerName && !signals.nicknameReadonly) {
      return { type: 'set_name', value: config.playerName };
    }
    if (signals.selectedMode !== config.gameMode) {
      // Two-step, spread across ticks (mirrors real reconnaissance: the
      // dropdown must actually be open in the DOM before an option div
      // exists to click -- see openModeDropdownAndSelect below):
      return signals.modeDropdownOpen
        ? { type: 'select_mode', value: config.gameMode }
        : { type: 'open_mode_dropdown' };
    }
    if (!signals.spawnButtonDisabled) {
      return { type: 'click_play' };
    }
    return null;
  }

  if (classification.state === LIFECYCLE_STATES.DEAD) {
    return { type: 'click_respawn' };
  }

  // CAPTCHA_REQUIRED / UNKNOWN / LOADING / ENTERING_GAME / PLAYING: never act.
  return null;
}

// --- Wire messages (pure, JSON-safe -- see tests/lifecycle.test.js) -------

function buildLifecycleSnapshotMessage(classification, tabIdIgnored, observedAtMs) {
  return {
    type: 'lifecycle_snapshot',
    observedAtMs,
    snapshot: {
      state: classification.state,
      reason: classification.reason,
      selectedMode: classification.selectedMode,
    },
  };
}

// --- Imperative glue (real chrome.*/DOM -- exercised by live smoke test,
// not unit tests; see bridge.js's createBridge for the same split) -------

function realClick(el) {
  if (!el) {
    return false;
  }
  const rect = el.getBoundingClientRect();
  const opts = {
    bubbles: true, cancelable: true, view: window,
    clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
    detail: 1,
  };
  // A plain el.click() and a bare MouseEvent dispatched on a WRAPPER
  // element do not reliably reach diep.io's real onClick handlers (live-
  // confirmed: the actual handler lives on the innermost element, e.g.
  // `.selected`/`.unselected[data-value]`, not their containing
  // `.selector` div -- events only bubble UP from the exact dispatch
  // target, never down into descendants). Callers must pass the exact
  // element that visually receives the click, matching what a human
  // would actually click.
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.dispatchEvent(new MouseEvent('click', opts));
  return true;
}

function setNativeInputValue(input, value) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function applyAction(action) {
  switch (action.type) {
    case 'set_name': {
      const input = document.getElementById('spawn-nickname');
      if (input && !input.readOnly) {
        setNativeInputValue(input, action.value);
      }
      return;
    }
    case 'open_mode_dropdown': {
      const selected = document.querySelector('#gamemode-selector .selected');
      realClick(selected);
      return;
    }
    case 'select_mode': {
      const option = document.querySelector(`#gamemode-selector .unselected[data-value="${action.value}"]`);
      realClick(option);
      return;
    }
    case 'click_play': {
      const btn = document.getElementById('spawn-button');
      if (btn && !btn.disabled) {
        realClick(btn);
      }
      return;
    }
    case 'click_respawn': {
      const btn = document.getElementById('game-over-continue');
      realClick(btn);
      return;
    }
    default:
      return;
  }
}

// --- chrome.runtime wiring: report snapshots, request cached config ------

function requestLifecycleConfig() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: 'get_lifecycle_config' }, (response) => {
        // chrome.runtime.lastError (e.g. background not ready yet) must
        // never throw here -- just resolve to "no config yet", same as
        // never having received one.
        void chrome.runtime.lastError;
        resolve(response && response.config ? response.config : null);
      });
    } catch (_error) {
      resolve(null);
    }
  });
}

function reportLifecycleSnapshot(message) {
  try {
    chrome.runtime.sendMessage({ type: 'lifecycle_observed', snapshot: message.snapshot });
  } catch (_error) {
    // A torn-down/reloading extension context must not crash this poll
    // loop -- just skip this tick's report, same spirit as bridge.js's
    // own pollOnce() catch.
  }
}

function startLifecyclePolling() {
  let cachedConfig = null;
  let lastActionAppliedAtMs = null;
  let stopped = false;

  requestLifecycleConfig().then((config) => {
    cachedConfig = config;
  });

  async function tick() {
    if (stopped) {
      return;
    }
    const classification = classifyLifecycle(document);
    // Re-derive whether the mode dropdown is currently open (has visible
    // `.unselected` option divs) for planAction's two-step mode-select --
    // a real-DOM-only signal, deliberately not part of collectSignals'
    // return value sent over the bridge (see buildLifecycleSnapshotMessage).
    classification.signals.modeDropdownOpen = !!document.querySelector('#gamemode-selector .unselected');

    reportLifecycleSnapshot(buildLifecycleSnapshotMessage(classification, null, Date.now()));

    if (cachedConfig === null) {
      cachedConfig = await requestLifecycleConfig();
    }

    const now = Date.now();
    const action = planAction(classification, cachedConfig, now, lastActionAppliedAtMs);
    if (action) {
      applyAction(action);
      lastActionAppliedAtMs = now;
    }

    if (!stopped) {
      setTimeout(tick, POLL_INTERVAL_MS);
    }
  }

  setTimeout(tick, POLL_INTERVAL_MS);

  return {
    stop() {
      stopped = true;
    },
  };
}

// Exposed for tests/lifecycle.test.js only (mirrors bridge.js's
// __deepEyeBridgeInternals convention) -- never used by this file's own
// runtime behavior beyond this assignment.
globalThis.__deepEyeLifecycleInternals = {
  LIFECYCLE_STATES,
  hasClass,
  isVisible,
  collectSignals,
  classifyState,
  classifyLifecycle,
  planAction,
  buildLifecycleSnapshotMessage,
  CAPTCHA_IFRAME_SELECTOR,
  ACTION_COOLDOWN_MS,
};

// Only auto-start in a real content-script context (chrome.runtime
// present) -- never as a side effect of merely loading/parsing this
// source in a test harness (see tests/lifecycle.test.js).
if (typeof chrome !== 'undefined' && chrome.runtime && typeof document !== 'undefined') {
  startLifecyclePolling();
}
