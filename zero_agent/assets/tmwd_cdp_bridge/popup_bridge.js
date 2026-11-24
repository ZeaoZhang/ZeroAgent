// Isolated-world bridge for window.open() calls that the page makes *after*
// the action that triggered them has already returned (setTimeout / await
// chains, "click → request → open detail page"). The injected page runtime
// lives in the MAIN world and cannot call chrome.* APIs, so it posts the
// blocked destination with window.postMessage; this script forwards it to the
// service worker, which opens a real tab through chrome.tabs.create.
//
// content script in every frame at document_start. Rate limited so a runaway
// page loop cannot open unlimited tabs.
(function () {
  const SOURCE = 'dsh-browser-control';
  const MAX_PER_SECOND = 6;
  let windowStart = 0;
  let count = 0;

  function allow() {
    const now = Date.now();
    if (now - windowStart > 1000) {
      windowStart = now;
      count = 0;
    }
    count += 1;
    return count <= MAX_PER_SECOND;
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE || data.type !== 'dsh_popup') return;
    const href = typeof data.href === 'string' ? data.href : '';
    if (!/^(https?|file|chrome-extension):/i.test(href)) return;
    if (!allow()) return;
    try {
      chrome.runtime.sendMessage(
        { cmd: 'open_popup', href: href, target: String(data.target || '_blank') },
        function () { void chrome.runtime.lastError; },
      );
    } catch (e) {
      /* extension was reloaded — the page will reconnect on its next call */
    }
  });
})();
