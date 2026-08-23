(() => {
  'use strict';

  const elements = {
    diepDetected: document.getElementById('diep-detected'),
    oracleReady: document.getElementById('oracle-ready'),
    squareCount: document.getElementById('square-count'),
    message: document.getElementById('message'),
    diagFillsSeen: document.getElementById('diag-fills-seen'),
    diagAccepted: document.getElementById('diag-accepted'),
    diagRejected: document.getElementById('diag-rejected'),
    diagCached: document.getElementById('diag-cached'),
    diagTopReasons: document.getElementById('diag-top-reasons'),
    diagTopTopology: document.getElementById('diag-top-topology'),
  };

  async function activeTab() {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tabs[0] || tabs[0].id === undefined) {
      throw new Error('No active tab is available.');
    }
    return tabs[0];
  }

  async function runInPage(func, args = []) {
    const tab = await activeTab();
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: 'MAIN',
      func,
      args,
    });
    if (!results[0]) {
      throw new Error('The diep.io page did not return a result.');
    }
    return results[0].result;
  }

  function setBoolean(element, value) {
    element.textContent = value ? 'Yes' : 'No';
    element.className = value ? 'yes' : 'no';
  }

  // Generic top-N formatter for any {key: count} diagnostics histogram --
  // used for rejectionReasons and, since it needs no knowledge of what a
  // "key" means, equally for squareColorTopologyHistogram's subpath
  // vertex-count signatures (e.g. "6,1").
  function topEntries(histogram, count = 3) {
    if (!histogram || typeof histogram !== 'object') {
      return [];
    }
    return Object.entries(histogram)
      .filter(([, value]) => Number(value) > 0)
      .sort(([, a], [, b]) => b - a)
      .slice(0, count)
      .map(([name, value]) => `${name}: ${value}`);
  }

  async function updateStatus() {
    try {
      const status = await runInPage(() => {
        const oracle = window.deepEyeOracle;
        let shapes;
        let diag;
        try {
          shapes = oracle?.shapes();
        } catch (_error) {
          shapes = undefined;
        }
        try {
          diag = oracle?.diagnostics();
        } catch (_error) {
          diag = undefined;
        }
        return {
          diepDetected: location.origin === 'https://diep.io',
          oracleReady: Boolean(oracle?.isReady()),
          squareCount: Array.isArray(shapes) ? shapes.length : null,
          diagnostics: diag,
        };
      });
      setBoolean(elements.diepDetected, status.diepDetected);
      setBoolean(elements.oracleReady, status.oracleReady);
      elements.squareCount.textContent = status.squareCount ?? '—';

      const squareColor = status.diagnostics?.squareColor;
      elements.diagFillsSeen.textContent = squareColor?.fillsSeen ?? '—';
      elements.diagAccepted.textContent = squareColor?.accepted ?? '—';
      elements.diagRejected.textContent = squareColor?.rejected ?? '—';
      elements.diagCached.textContent = status.diagnostics?.cache?.currentlyCached ?? '—';
      const reasons = topEntries(status.diagnostics?.rejectionReasons);
      elements.diagTopReasons.textContent = `Top rejection reasons: ${reasons.length ? reasons.join(', ') : 'none'}`;
      const topology = topEntries(status.diagnostics?.squareColorTopologyHistogram);
      elements.diagTopTopology.textContent = `Top subpath topology: ${topology.length ? topology.join(', ') : 'none'}`;
    } catch (_error) {
      setBoolean(elements.diepDetected, false);
      setBoolean(elements.oracleReady, false);
      elements.squareCount.textContent = '—';
      elements.diagFillsSeen.textContent = '—';
      elements.diagAccepted.textContent = '—';
      elements.diagRejected.textContent = '—';
      elements.diagCached.textContent = '—';
      elements.diagTopReasons.textContent = 'Top rejection reasons: —';
      elements.diagTopTopology.textContent = 'Top subpath topology: —';
    }
  }

  const COPY_LABELS = { snapshot: 'Snapshot', shapes: 'Shapes', diagnostics: 'Diagnostics' };

  async function copyOracleMethod(method) {
    try {
      const value = await runInPage((methodName) => {
        const oracle = window.deepEyeOracle;
        if (!oracle || typeof oracle[methodName] !== 'function') {
          throw new Error('Browser Oracle is unavailable.');
        }
        return oracle[methodName]();
      }, [method]);
      await navigator.clipboard.writeText(JSON.stringify(value, null, 2));
      elements.message.textContent = `${COPY_LABELS[method] ?? method} copied.`;
    } catch (error) {
      elements.message.textContent = error.message;
    }
  }

  document.getElementById('copy-snapshot').addEventListener('click', () => {
    void copyOracleMethod('snapshot');
  });
  document.getElementById('copy-shapes').addEventListener('click', () => {
    void copyOracleMethod('shapes');
  });
  document.getElementById('copy-diagnostics').addEventListener('click', () => {
    void copyOracleMethod('diagnostics');
  });
  document.getElementById('refresh-tab').addEventListener('click', async () => {
    try {
      const tab = await activeTab();
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: 'MAIN',
        func: () => location.origin === 'https://diep.io',
      });
      if (!results[0]?.result) {
        throw new Error('The active tab is not diep.io.');
      }
      await chrome.tabs.reload(tab.id);
      window.close();
    } catch (error) {
      elements.message.textContent = error.message;
    }
  });
  document.getElementById('reload-extension').addEventListener('click', () => {
    chrome.runtime.reload();
  });

  void updateStatus();
  window.setInterval(() => void updateStatus(), 1000);
})();
