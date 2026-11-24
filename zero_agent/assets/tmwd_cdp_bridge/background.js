// background.js - Cookie + CDP Bridge
chrome.runtime.onInstalled.addListener(() => {
  console.log('CDP Bridge installed');
  // Strip CSP headers to allow eval/inline scripts
  chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [9999],
    addRules: [{
      id: 9999, priority: 1,
      action: { type: 'modifyHeaders', responseHeaders: [
        { header: 'content-security-policy', operation: 'remove' },
        { header: 'content-security-policy-report-only', operation: 'remove' }
      ]},
      condition: { urlFilter: '*', resourceTypes: ['main_frame', 'sub_frame'] }
    }]
  });
});

// One extension instance serves both drivers. ZeroAgent uses 18765/18766,
// dsh-browser-control uses 18865 for both WS and HTTP.
const WS_TARGETS = [
  { ws: 'ws://127.0.0.1:18765', http: 'http://127.0.0.1:18766' },
  { ws: 'ws://127.0.0.1:18865', http: 'http://127.0.0.1:18865' },
];
const socketsByUrl = new Map();
const EXT_BRIDGE_MESSAGE = 'DSH_BROWSER_BRIDGE';
const EXT_BRIDGE_TIMEOUT_MS = 25000;

function openSockets() {
  return [...socketsByUrl.values()].filter(
    socket => socket && socket.readyState === WebSocket.OPEN,
  );
}

function wsSend(payload) {
  const text = JSON.stringify(payload);
  for (const socket of openSockets()) {
    try { socket.send(text); } catch (e) { /* socket went away */ }
  }
}

async function handleExtMessage(msg, sender) {
  if (msg.cmd === 'status') {
    const opened = openSockets();
    return { ok: true, data: {
      state: opened.length > 0 ? 'connected' : 'connecting',
      ws: opened.length > 0,
      connections: opened.length,
      servers: WS_TARGETS.map(target => target.http),
    } };
  }
  if (msg.cmd === 'cookies') return await handleCookies(msg, sender);
  if (msg.cmd === 'open_popup') return await handleOpenPopup(msg, sender);
  if (msg.cmd === 'download') return await handleDownload(msg);
  if (msg.cmd === 'ext_eval' || msg.cmd === 'ext_cdp') return await handleExtBridge(msg);
  if (msg.cmd === 'cdp') return await handleCDP(msg, sender);
  if (msg.cmd === 'cdpEvents') return await handleCdpEvents(msg, sender);
  if (msg.cmd === 'intercept') return await handleIntercept(msg);
  if (msg.cmd === 'batch') return await handleBatch(msg, sender);
  if (msg.cmd === 'tabs') {
    try {
      if (msg.method === 'create') {
        if (msg.dedupe) {
          const existing = popupTabOf(String(msg.url || ''));
          if (existing) return { ok: true, data: { ...existing, deduped: true } };
        }
        const tab = await chrome.tabs.create({
          url: msg.url,
          active: msg.active !== undefined ? msg.active : false,
          index: msg.index,
          windowId: msg.windowId,
          openerTabId: msg.openerTabId
        });
        const waitMs = Math.max(0, Math.min(Number(msg.waitMs) || 5000, 15000));
        const ready = await waitForTabReady(tab.id, waitMs);
        const data = ready || tabSnapshot(tab);
        if (msg.dedupe) notePopup(String(msg.url || ''), data);
        return { ok: true, data };
      }
      if (msg.method === 'switch' || msg.method === 'activate') {
        const tab = await chrome.tabs.update(msg.tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        return { ok: true, data: true };
      }
      if (msg.method === 'navigate') {
        if (!msg.url) return { ok: false, error: 'url is required' };
        const tab = await chrome.tabs.update(msg.tabId, { url: msg.url });
        return { ok: true, data: tabSnapshot(tab) };
      }
      if (msg.method === 'close') {
        const ids = Array.isArray(msg.tabIds) ? msg.tabIds : (msg.tabId !== undefined ? [msg.tabId] : []);
        if (ids.length === 0) return { ok: false, error: 'tabId or tabIds is required' };
        const closed = [];
        const errors = [];
        for (const tabId of ids) {
          try {
            await chrome.tabs.remove(tabId);
            closed.push(tabId);
          } catch (e) {
            errors.push({ tabId, error: e.message });
          }
        }
        return { ok: true, data: { closed, errors } };
      } else {
        const tabs = await chrome.tabs.query({});
        const data = (msg.includeAll ? tabs : tabs.filter(t => isScriptable(t.url))).map(tabSnapshot);
        return { ok: true, data };
      }
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'management') {
    try {
      if (msg.method === 'list') {
        const all = await chrome.management.getAll();
        return { ok: true, data: all.map(e => ({ id: e.id, name: e.name, enabled: e.enabled, type: e.type, version: e.version })) };
      }
      if (msg.method === 'reload') {
        chrome.alarms.create('tmwd-self-reload', { when: Date.now() + 200 });
        return { ok: true };
      }
      if (msg.method === 'disable') {
        await chrome.management.setEnabled(msg.extId, false);
        return { ok: true };
      }
      if (msg.method === 'enable') {
        await chrome.management.setEnabled(msg.extId, true);
        return { ok: true };
      }
      return { ok: false, error: 'Unknown method: ' + msg.method };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'contentSettings') {
    try {
      const type = msg.type || 'automaticDownloads';
      const setting = msg.setting || 'allow';
      const pattern = msg.pattern || '<all_urls>';
      await chrome.contentSettings[type].set({
        primaryPattern: pattern,
        setting: setting
      });
      return { ok: true };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  return { ok: false, error: 'Unknown cmd: ' + msg.cmd };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleExtMessage(msg, sender).then(sendResponse);
  return true;
});

async function handleCookies(msg, sender) {
  try {
    let url = msg.url || sender.tab?.url;
    if (!url && msg.tabId) {
      const tab = await chrome.tabs.get(msg.tabId);
      url = tab.url;
    }
    if (!url || !/^https?:/i.test(url)) {
      return { ok: false, error: '当前标签页不是 http(s) 页面（chrome:// 等无法读取 cookies）' };
    }
    const origin = (url.match(/^https?:\/\/[^\/]+/) || [url])[0];
    const all = await chrome.cookies.getAll({ url });
    const part = await chrome.cookies.getAll({ url, partitionKey: { topLevelSite: origin } }).catch(() => []);
    const merged = [...all];
    for (const c of part) {
      if (!merged.some(x => x.name === c.name && x.domain === c.domain)) merged.push(c);
    }
    return { ok: true, data: merged };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// Persistent debugger attachments. A tab stays attached across CDP calls so
// concurrent monitors on different tabs cannot evict each other.
const cdpAttachedTabs = new Set();
const cdpEventBuffers = new Map();
const cdpLocks = new Map();
const CDP_EVENT_LIMIT = 1000;

// Serialize attach/send/detach sequences per tab. Two one-shot commands on the
// same tab must not detach each other while the other command is in flight.
function withCdpLock(tabId, task) {
  const previous = cdpLocks.get(tabId) || Promise.resolve();
  const current = previous.catch(() => {}).then(task);
  cdpLocks.set(tabId, current);
  current.finally(() => {
    if (cdpLocks.get(tabId) === current) cdpLocks.delete(tabId);
  }).catch(() => {});
  return current;
}

async function ensureCdpAttached(tabId) {
  if (cdpAttachedTabs.has(tabId)) return false;
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
  } catch (e) {
    if (!/Another debugger is already attached/i.test(e.message || '')) throw e;
    // The worker may have restarted while this extension still owns the
    // attachment. Probe before treating it as ours; DevTools attachments fail.
    try {
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
        expression: '1', returnByValue: true,
      });
    } catch (_) {
      throw new Error(`Another debugger is already attached to tab ${tabId}`);
    }
  }
  cdpAttachedTabs.add(tabId);
  return true;
}

async function detachCdp(tabId) {
  try { await chrome.debugger.detach({ tabId }); } catch (_) {}
  cdpAttachedTabs.delete(tabId);
}

function tabSnapshot(tab) {
  return {
    id: tab.id,
    url: tab.url,
    title: tab.title,
    active: tab.active,
    windowId: tab.windowId,
    index: tab.index,
    status: tab.status,
    pinned: tab.pinned,
    lastAccessed: tab.lastAccessed,
  };
}

async function waitForTabReady(tabId, timeoutMs) {
  const deadline = Date.now() + Math.max(0, timeoutMs);
  let last = null;
  while (Date.now() < deadline) {
    try { last = await chrome.tabs.get(tabId); } catch (_) { return null; }
    if (last && last.status === 'complete' && last.url) return tabSnapshot(last);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return last ? tabSnapshot(last) : null;
}

function recordCdpEvent(source, method, params) {
  const tabId = source?.tabId;
  if (typeof tabId !== 'number') return;
  const key = String(tabId);
  const event = { tabId, method, params: params || {}, ts: Date.now() };
  const buffer = cdpEventBuffers.get(key) || [];
  buffer.push(event);
  if (buffer.length > CDP_EVENT_LIMIT) buffer.splice(0, buffer.length - CDP_EVENT_LIMIT);
  cdpEventBuffers.set(key, buffer);
  const payload = { type: 'cdp_event', ...event };
  for (const socket of openSockets()) {
    try { socket.send(JSON.stringify(payload)); } catch (_) {}
  }
}

async function handleCdpEvents(msg, sender) {
  const rawTabId = msg.tabId !== undefined ? msg.tabId : sender?.tab?.id;
  const tabId = rawTabId !== undefined && rawTabId !== null ? String(rawTabId) : null;
  let events = [];
  if (tabId !== null) {
    const numeric = Number(tabId);
    events = (cdpEventBuffers.get(tabId) || []).map(e => ({
      ...e, tabId: Number.isNaN(numeric) ? tabId : numeric,
    }));
  } else {
    for (const [id, buffer] of cdpEventBuffers.entries()) {
      const numeric = Number(id);
      events.push(...buffer.map(e => ({ ...e, tabId: Number.isNaN(numeric) ? id : numeric })));
    }
  }
  if (Array.isArray(msg.methods) && msg.methods.length) {
    const allowed = new Set(msg.methods);
    events = events.filter(e => allowed.has(e.method));
  }
  const limit = Math.max(1, Math.min(Number(msg.limit) || 200, CDP_EVENT_LIMIT));
  events = events.slice(-limit);
  if (msg.clear) {
    if (tabId !== null) cdpEventBuffers.delete(tabId);
    else cdpEventBuffers.clear();
  }
  return { ok: true, data: events };
}

// --- Unattended Fetch interception ---------------------------------------
// Rules are stored per tab and survive service-worker suspension. Requests
// that do not match any rule are continued automatically so a tab never hangs.
const interceptRules = new Map();
const interceptsReady = (async () => {
  try {
    const stored = await chrome.storage.session.get('intercepts');
    for (const [tabId, rules] of Object.entries(stored?.intercepts || {})) {
      if (Array.isArray(rules)) interceptRules.set(String(tabId), rules);
    }
  } catch (_) {}
})();

async function saveIntercepts() {
  try {
    const data = {};
    for (const [tabId, rules] of interceptRules.entries()) data[tabId] = rules;
    await chrome.storage.session.set({ intercepts: data });
  } catch (_) {}
}

async function restoreIntercepts() {
  await interceptsReady;
  for (const [tabId, rules] of interceptRules.entries()) {
    if (!Array.isArray(rules) || rules.length === 0) continue;
    const numeric = Number(tabId);
    if (!Number.isInteger(numeric)) continue;
    const needsResponse = rules.some(rule =>
      rule.response_status !== undefined
      || rule.response_headers !== undefined
      || rule.response_body !== undefined
    );
    const patterns = [{ urlPattern: '*', requestStage: 'Request' }];
    if (needsResponse) patterns.push({ urlPattern: '*', requestStage: 'Response' });
    try {
      await withCdpLock(numeric, async () => {
        await ensureCdpAttached(numeric);
        await chrome.debugger.sendCommand({ tabId: numeric }, 'Fetch.enable', { patterns });
      });
    } catch (_) {}
  }
}

function normalizeInterceptRule(rule) {
  if (!rule || typeof rule !== 'object') return null;
  const out = {};
  if (rule.id !== undefined) out.id = String(rule.id);
  if (rule.url_contains) out.url_contains = String(rule.url_contains);
  if (rule.url_regex) {
    try { new RegExp(String(rule.url_regex)); } catch (_) { return null; }
    out.url_regex = String(rule.url_regex);
  }
  if (rule.method) out.method = String(rule.method).toUpperCase();
  if (rule.request_headers && typeof rule.request_headers === 'object' && !Array.isArray(rule.request_headers)) {
    out.request_headers = rule.request_headers;
  }
  if (rule.response_status !== undefined) {
    const status = Number(rule.response_status);
    if (Number.isFinite(status)) out.response_status = status;
  }
  if (rule.response_headers && typeof rule.response_headers === 'object' && !Array.isArray(rule.response_headers)) {
    out.response_headers = rule.response_headers;
  }
  if (rule.response_body !== undefined) out.response_body = String(rule.response_body);
  if (rule.response_body_base64) out.response_body_base64 = true;
  if (rule.abort) out.abort = true;
  if (rule.abort_reason) out.abort_reason = String(rule.abort_reason);
  if (rule.delay_ms !== undefined) {
    const delay = Number(rule.delay_ms) || 0;
    out.delay_ms = Math.max(0, Math.min(delay, 60000));
  }
  return out;
}

function matchInterceptRule(rules, request, stage) {
  const url = String(request?.url || '');
  const method = String(request?.method || '').toUpperCase();
  for (const rule of rules) {
    if (rule.method && rule.method !== method) continue;
    if (rule.url_contains && !url.includes(rule.url_contains)) continue;
    if (rule.url_regex) {
      try { if (!new RegExp(rule.url_regex).test(url)) continue; } catch (_) { continue; }
    }
    const hasRequestAction = !!(rule.abort || rule.request_headers || rule.delay_ms !== undefined);
    const hasResponseAction = rule.response_status !== undefined
      || rule.response_body !== undefined
      || rule.response_headers !== undefined;
    // Let a response-only rule wait for the Response stage instead of
    // consuming the Request stage and blocking a later matching rule.
    if (stage === 'request' && hasResponseAction && !hasRequestAction) continue;
    if (stage === 'response' && !hasResponseAction) continue;
    return rule;
  }
  return null;
}

function applyRequestHeaderChanges(original, changes) {
  const map = new Map();
  for (const [name, value] of Object.entries(original || {})) {
    map.set(name.toLowerCase(), { name, value: String(value) });
  }
  for (const [name, value] of Object.entries(changes || {})) {
    const key = name.toLowerCase();
    if (value === null || value === undefined || value === '') map.delete(key);
    else map.set(key, { name, value: String(value) });
  }
  return [...map.values()];
}

function applyResponseHeaderChanges(original, changes) {
  const map = new Map();
  for (const header of original || []) {
    if (!header || !header.name) continue;
    map.set(String(header.name).toLowerCase(), {
      name: header.name, value: String(header.value ?? ''),
    });
  }
  for (const [name, value] of Object.entries(changes || {})) {
    const key = name.toLowerCase();
    if (value === null || value === undefined || value === '') map.delete(key);
    else map.set(key, { name, value: String(value) });
  }
  return [...map.values()];
}

function textToBase64(text) {
  const bytes = new TextEncoder().encode(String(text));
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function base64ByteLength(value) {
  if (typeof value !== 'string') return null;
  const clean = value.replace(/\s+/g, '');
  if (!clean) return 0;
  const padding = clean.endsWith('==') ? 2 : (clean.endsWith('=') ? 1 : 0);
  return Math.max(0, Math.floor(clean.length * 3 / 4) - padding);
}

function recordIntercept(tabId, params, rule, action) {
  recordCdpEvent({ tabId }, 'ZeroAgent.intercept', {
    requestId: params.requestId,
    url: params.request?.url,
    method: params.request?.method,
    ruleId: rule?.id || null,
    action,
    responseStatus: params.responseStatusCode,
  });
}

async function handleFetchPaused(tabId, params) {
  await interceptsReady;
  const rules = interceptRules.get(String(tabId));
  if (!rules) return; // Manual Fetch.enable via web_cdp keeps manual semantics.
  const requestId = params.requestId;
  if (!requestId) return;
  const isResponseStage = params.responseStatusCode !== undefined
    || params.responseErrorReason !== undefined
    || Array.isArray(params.responseHeaders);
  const rule = matchInterceptRule(
    rules, params.request || {}, isResponseStage ? 'response' : 'request',
  );
  try {
    if (!rule) {
      await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest', { requestId });
      recordIntercept(tabId, params, null, 'auto_continue');
      return;
    }
    if (rule.delay_ms) await new Promise(resolve => setTimeout(resolve, rule.delay_ms));
    if (rule.abort) {
      await chrome.debugger.sendCommand({ tabId }, 'Fetch.failRequest', {
        requestId, errorReason: rule.abort_reason || 'Aborted',
      });
      recordIntercept(tabId, params, rule, 'abort');
      return;
    }
    if (!isResponseStage) {
      const changes = rule.request_headers || {};
      if (Object.keys(changes).length > 0) {
        const headers = applyRequestHeaderChanges(params.request?.headers, changes);
        await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest', { requestId, headers });
      } else {
        await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest', { requestId });
      }
      recordIntercept(tabId, params, rule, 'continue_request');
      return;
    }
    const modifiesResponse = rule.response_status !== undefined
      || rule.response_body !== undefined
      || (rule.response_headers && Object.keys(rule.response_headers).length > 0);
    if (!modifiesResponse) {
      await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest', { requestId });
      recordIntercept(tabId, params, rule, 'continue_response');
      return;
    }
    if (rule.response_body === undefined) {
      // Header/status-only changes can pass the original body through,
      // avoiding a re-encode of compressed responses.
      const continueParams = { requestId };
      if (rule.response_status !== undefined) {
        continueParams.responseCode = Number(rule.response_status);
      }
      if (rule.response_headers !== undefined) {
        continueParams.responseHeaders = applyResponseHeaderChanges(
          params.responseHeaders, rule.response_headers,
        );
      }
      try {
        await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueResponse', continueParams);
        recordIntercept(tabId, params, rule, 'continue_response_modified');
        return;
      } catch (_) {
        // Older Chrome: fall through to getResponseBody + fulfillRequest.
      }
    }
    let body = rule.response_body;
    let bodyBase64 = !!rule.response_body_base64;
    if (body === undefined) {
      const original = await chrome.debugger.sendCommand({ tabId }, 'Fetch.getResponseBody', { requestId });
      body = original.body || '';
      bodyBase64 = !!original.base64Encoded;
    }
    if (!bodyBase64 && body !== undefined) body = textToBase64(body);
    let headers = applyResponseHeaderChanges(params.responseHeaders, rule.response_headers);
    if (body !== undefined) {
      const overrideNames = new Set(
        Object.keys(rule.response_headers || {}).map(name => name.toLowerCase()),
      );
      headers = headers.filter(header => {
        const name = String(header.name).toLowerCase();
        if (name === 'content-length') return false;
        // A replaced body is no longer the original encoded payload.
        if (name === 'content-encoding' && !overrideNames.has('content-encoding')) return false;
        return true;
      });
      const length = base64ByteLength(body);
      if (length !== null) headers.push({ name: 'Content-Length', value: String(length) });
    }
    const responseCode = rule.response_status !== undefined
      ? Number(rule.response_status)
      : (params.responseStatusCode || 200);
    await chrome.debugger.sendCommand({ tabId }, 'Fetch.fulfillRequest', {
      requestId, responseCode, responseHeaders: headers, body: body || '',
    });
    recordIntercept(tabId, params, rule, 'fulfill');
  } catch (e) {
    try {
      await chrome.debugger.sendCommand({ tabId }, 'Fetch.continueRequest', { requestId });
    } catch (_) {}
    recordIntercept(tabId, params, rule, `error: ${e.message || String(e)}`);
  }
}

async function handleIntercept(msg) {
  const tabId = Number(msg.tabId);
  if (!Number.isInteger(tabId)) return { ok: false, error: 'no tabId' };
  await interceptsReady;
  const key = String(tabId);
  const action = String(msg.action || (msg.disable ? 'clear' : 'apply')).toLowerCase();
  if (action === 'list') {
    return { ok: true, data: interceptRules.get(key) || [] };
  }
  if (action === 'clear' || action === 'disable') {
    interceptRules.delete(key);
    await saveIntercepts();
    try {
      await withCdpLock(tabId, async () => {
        const attachedHere = await ensureCdpAttached(tabId);
        try {
          await chrome.debugger.sendCommand({ tabId }, 'Fetch.disable', {});
        } finally {
          if (attachedHere) await detachCdp(tabId);
        }
      });
    } catch (_) {}
    return { ok: true, data: [] };
  }
  const rules = Array.isArray(msg.rules)
    ? msg.rules.map(normalizeInterceptRule).filter(Boolean)
    : [];
  if (Array.isArray(msg.rules) && msg.rules.length > 0 && rules.length === 0) {
    return { ok: false, error: 'no valid intercept rules' };
  }
  interceptRules.set(key, rules);
  await saveIntercepts();
  const needsResponse = rules.some(rule =>
    rule.response_status !== undefined
    || rule.response_headers !== undefined
    || rule.response_body !== undefined
  );
  const patterns = [{ urlPattern: '*', requestStage: 'Request' }];
  if (needsResponse) patterns.push({ urlPattern: '*', requestStage: 'Response' });
  try {
    await withCdpLock(tabId, async () => {
      await ensureCdpAttached(tabId);
      await chrome.debugger.sendCommand({ tabId }, 'Fetch.enable', { patterns });
    });
  } catch (e) {
    interceptRules.delete(key);
    await saveIntercepts();
    throw e;
  }
  return { ok: true, data: { tabId, rules, patterns } };
}

// --- async window.open bridge ---------------------------------------------
// popup_bridge.js forwards blocked window.open() destinations that happen
// after an action returned (setTimeout/await chains). Remember them briefly so
// a duplicate open does not create a second tab.
const RECENT_POPUP_MS = 3000;
const recentPopups = new Map(); // href -> { at, tab }

function popupTabOf(href) {
  const hit = recentPopups.get(href);
  if (!hit) return null;
  if (Date.now() - hit.at > RECENT_POPUP_MS) {
    recentPopups.delete(href);
    return null;
  }
  return hit.tab;
}

function notePopup(href, tab) {
  if (!href || !tab) return;
  recentPopups.set(href, { at: Date.now(), tab });
  if (recentPopups.size > 20) {
    const now = Date.now();
    for (const [key, value] of recentPopups) {
      if (now - value.at > RECENT_POPUP_MS) recentPopups.delete(key);
    }
  }
}

async function handleOpenPopup(msg, sender) {
  try {
    const href = String(msg.href || '');
    if (!/^(https?|file|chrome-extension):/i.test(href)) {
      return { ok: false, error: '拒绝打开不支持的地址: ' + href };
    }
    const existing = popupTabOf(href);
    if (existing) return { ok: true, data: { ...existing, deduped: true } };
    const tab = await chrome.tabs.create({
      url: href,
      active: false,
      openerTabId: sender.tab?.id,
    });
    const ready = await waitForTabReady(tab.id, 5000);
    const data = ready || tabSnapshot(tab);
    notePopup(href, data);
    wsSend({ type: 'newTabs', tabs: [data] });
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// --- downloads -------------------------------------------------------------
function downloadSummary(item) {
  if (!item) return null;
  return {
    id: item.id,
    url: item.url || item.finalUrl || '',
    final_url: item.finalUrl || '',
    filename: item.filename || '',
    bytes: Number(item.bytesReceived) || Number(item.fileSize) || 0,
    file_size: Number(item.fileSize) || 0,
    state: item.state || '',
    mime: item.mime || '',
    error: item.error || '',
    exists: item.exists !== false,
    by_extension: item.byExtensionId === chrome.runtime.id,
    start_time: item.startTime || '',
    end_time: item.endTime || '',
  };
}

async function latestDownloads(since) {
  const startedAfter = new Date(Math.max(0, since - 1500)).toISOString();
  let items = [];
  try {
    items = await chrome.downloads.search({ startedAfter, orderBy: ['-startTime'], limit: 10 });
  } catch (e) { /* fall through */ }
  if (!items || !items.length) {
    try { items = await chrome.downloads.search({ orderBy: ['-id'], limit: 5 }); } catch (e) { items = []; }
  }
  return items || [];
}

async function waitForDownload(since, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    const items = await latestDownloads(since);
    const fresh = items.filter((item) => !item.startTime || Date.parse(item.startTime) >= since - 100);
    if (fresh.length) {
      last = fresh[0];
      if (last.state && last.state !== 'in_progress') return downloadSummary(last);
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  return last ? downloadSummary(last) : null;
}

async function downloadsDir() {
  try {
    const items = await chrome.downloads.search({ orderBy: ['-startTime'], limit: 1 });
    const file = items && items[0] && items[0].filename;
    if (!file) return null;
    const match = String(file).match(/^(.*)[/\\][^/\\]*$/);
    return match ? match[1] : null;
  } catch (e) {
    return null;
  }
}

async function handleDownload(msg) {
  try {
    const method = msg.method || 'list';
    const timeoutMs = Math.min(Math.max(Number(msg.timeoutMs) || 20000, 1000), 120000);
    if (method === 'list') {
      const items = await chrome.downloads.search({
        orderBy: ['-startTime'],
        limit: Math.min(Math.max(Number(msg.limit) || 10, 1), 50),
      });
      return { ok: true, data: { downloads: (items || []).map(downloadSummary), dir: await downloadsDir() } };
    }
    if (method === 'start') {
      const url = String(msg.url || '');
      if (!/^(https?|file|blob|data):/i.test(url)) {
        return { ok: false, error: '只支持 http(s)/file/blob/data 下载地址: ' + url };
      }
      const since = Date.now();
      const id = await chrome.downloads.download({
        url,
        filename: msg.filename ? String(msg.filename) : undefined,
        saveAs: false,
        conflictAction: 'uniquify',
      });
      const download = await waitForDownload(since, timeoutMs);
      if (!download) return { ok: false, error: '下载任务已创建（id=' + id + '），但 ' + timeoutMs + 'ms 内没有出现在下载列表中' };
      return { ok: true, data: { id, download, dir: await downloadsDir() } };
    }
    if (method === 'wait') {
      const since = Number(msg.since) || Date.now() - 60000;
      const download = await waitForDownload(since, timeoutMs);
      if (!download) return { ok: false, error: '未检测到新的下载任务（窗口 ' + timeoutMs + 'ms）' };
      return { ok: true, data: { download, dir: await downloadsDir() } };
    }
    if (method === 'dir') return { ok: true, data: { dir: await downloadsDir() } };
    return { ok: false, error: 'Unknown download method: ' + method };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// --- cross-extension bridge ------------------------------------------------
function sendMessageToExtension(extensionId, payload, timeoutMs = EXT_BRIDGE_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      finish({ ok: false, error: '通过目标扩展执行超时（' + Math.round(timeoutMs / 1000) + 's）' });
    }, timeoutMs);
    try {
      chrome.runtime.sendMessage(extensionId, payload, (response) => {
        const lastError = chrome.runtime.lastError;
        if (lastError) {
          finish({ ok: false, error: lastError.message || '目标扩展无响应' });
          return;
        }
        finish(response || { ok: false, error: '目标扩展没有返回响应' });
      });
    } catch (e) {
      finish({ ok: false, error: e.message });
    }
  });
}

function buildBridgeExpression(code) {
  const src = String(code == null ? '' : code).trim();
  if (/(^|[\n;])\s*return\b/.test(src) || src.includes(';')) {
    return '(async () => {\n' + src + '\n})()';
  }
  return '(async () => (' + src + '))()';
}

async function handleExtBridge(msg) {
  const extensionId = String(msg.extensionId || '');
  if (!/^[a-p]{32}$/.test(extensionId)) return { ok: false, error: '无效的 extensionId' };
  if (extensionId === chrome.runtime.id) return { ok: false, error: '不能桥接到自身扩展' };
  const payload = msg.cmd === 'ext_eval'
    ? { type: EXT_BRIDGE_MESSAGE, tabId: msg.tabId, expression: buildBridgeExpression(msg.code) }
    : { type: EXT_BRIDGE_MESSAGE, tabId: msg.tabId, method: msg.method, params: msg.params || {} };
  const response = await sendMessageToExtension(extensionId, payload, msg.timeoutMs);
  if (!response || response.ok !== true) {
    return { ok: false, error: (response && response.error) || '目标扩展未实现 DSH_BROWSER_BRIDGE 桥接' };
  }
  if (msg.cmd === 'ext_eval') {
    const cdp = response.result || {};
    if (cdp.exceptionDetails) {
      const detail = cdp.exceptionDetails;
      const text = (detail.exception && (detail.exception.description || detail.exception.value)) || detail.text || '目标页面 JS 异常';
      return { ok: false, error: String(text) };
    }
    return { ok: true, data: cdp.result ? cdp.result.value : undefined };
  }
  return { ok: true, data: response.result };
}

async function handleBatch(msg, sender) {
  const R = [];
  const ownAttachments = new Set();
  const persistentAttachments = new Set();
  const resolve$N = (params) => JSON.parse(JSON.stringify(params || {}).replace(/"\$(\d+)\.([^"]+)"/g,
    (_, i, path) => { let v = R[+i]; for (const k of path.split('.')) v = v[k]; return JSON.stringify(v); }));
  try {
    for (const c of msg.commands) {
      if (c.tabId === undefined && msg.tabId !== undefined) c.tabId = msg.tabId;
      if (c.cmd === 'cookies') {
        R.push(await handleCookies(c, sender));
      } else if (c.cmd === 'tabs') {
        const tabs = await chrome.tabs.query({});
        const data = (c.includeAll ? tabs : tabs.filter(t => isScriptable(t.url))).map(tabSnapshot);
        R.push({ ok: true, data });
      } else if (c.cmd === 'cdp') {
        const tabId = c.tabId || msg.tabId || sender.tab?.id;
        if (!tabId) { R.push({ ok: false, error: 'no tabId' }); continue; }
        const attachedHere = await ensureCdpAttached(tabId);
        if (attachedHere) ownAttachments.add(tabId);
        if (c.persistent) persistentAttachments.add(tabId);
        R.push(await chrome.debugger.sendCommand({ tabId }, c.method, resolve$N(c.params)));
      } else {
        R.push({ ok: false, error: 'unknown cmd: ' + c.cmd });
      }
    }
    return { ok: true, results: R };
  } catch (e) {
    return { ok: false, error: e.message, results: R };
  } finally {
    for (const tabId of ownAttachments) {
      if (!persistentAttachments.has(tabId)) await detachCdp(tabId);
    }
  }
}

async function handleCDP(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  return withCdpLock(tabId, async () => {
    try {
      if (msg.method === 'detach') {
        await detachCdp(tabId);
        return { ok: true, data: true };
      }
      const attachedHere = await ensureCdpAttached(tabId);
      if (msg.method === 'attach') return { ok: true, data: true };
      try {
        const result = await chrome.debugger.sendCommand({ tabId }, msg.method, msg.params || {});
        return { ok: true, data: result };
      } finally {
        if (attachedHere && !msg.persistent) await detachCdp(tabId);
      }
    } catch (e) {
      return { ok: false, error: e.message };
    }
  });
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  recordCdpEvent(source, method, params);
  if (method === 'Fetch.requestPaused' && typeof source?.tabId === 'number') {
    handleFetchPaused(source.tabId, params).catch(() => {});
  }
});
chrome.debugger.onDetach.addListener((source) => {
  if (typeof source?.tabId === 'number') cdpAttachedTabs.delete(source.tabId);
});

// http(s), local files, blank pages and extension pages are controllable.
// chrome://, devtools:// and view-source: remain outside the extension sandbox.
const isScriptable = url => !!url && (
  url === 'about:blank' || /^(https?|file|chrome-extension):/i.test(url)
);

// --- Shared page/CDP script builder core ---
function buildExecScript(code, errorHandler) {
  return `(async () => {
    function smartProcessResult(result) {
      if (result === null || result === undefined || typeof result !== 'object') return result;
      try { if (result.window === result && result.document) return '[Window: ' + (result.location?.href || 'about:blank') + ']'; } catch(_){}
      if (typeof jQuery !== 'undefined' && result instanceof jQuery) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result instanceof NodeList || result instanceof HTMLCollection) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result.nodeType === 1) return result.outerHTML;
      if (!Array.isArray(result) && typeof result === 'object' && 'length' in result && typeof result.length === 'number') {
        const firstElement = result[0];
        if (firstElement && firstElement.nodeType === 1) {
          const elements = []; const length = Math.min(result.length, 100);
          for (let i = 0; i < length; i++) { const elem = result[i]; if (elem && elem.nodeType === 1) elements.push(elem.outerHTML); } return elements;
        }
      }
      try { return JSON.parse(JSON.stringify(result, function(key, value) { if (typeof value === 'object' && value !== null) { if (value.nodeType === 1) return value.outerHTML; if (value === window || value === document) return '[Object]'; try { if (value.window === value && value.document) return '[Window]'; } catch(_){} } return value; })); } catch (e) { return '[无法序列化: ' + e.message + ']'; }
    }
    try {
      const jsCode = ${JSON.stringify(code)}.trim();
      const lines = jsCode.split(/\\r?\\n/).filter(l => l.trim());
      const lastLine = lines.length > 0 ? lines[lines.length - 1].trim() : '';
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      let r;
      function _air(c) { const ls = c.split(/\\r?\\n/); let i = ls.length - 1; while (i >= 0 && !ls[i].trim()) i--; if (i < 0) return c; const t = ls[i].trim(); if (/^(return |return;|return$|let |const |var |if |if\\(|for |for\\(|while |while\\(|switch|try |throw |class |function |async |import |export |\\/\\/|})/.test(t)) return c; ls[i] = ls[i].match(/^(\\s*)/)[1] + 'return ' + t; return ls.join('\\n'); }
      const hasTopLevelReturn = /(^|[\\n;])\\s*return\\b/.test(jsCode);
      if (lastLine.startsWith('return') || hasTopLevelReturn) {
        r = await (new AsyncFunction(jsCode))();
      } else {
        try { r = eval(jsCode); if (r instanceof Promise) r = await r; } catch (e) {
          if (e instanceof SyntaxError && (/return/i.test(e.message) || /await/i.test(e.message))) { r = await (new AsyncFunction(_air(jsCode)))(); } else throw e;
        }
      }
      return { ok: true, data: smartProcessResult(r) };
    } catch (e) {
      ${errorHandler}
    }
  })()`;
}

function buildPageScript(code) {
  return buildExecScript(code, `
      const errMsg = e.message || String(e);
      return { ok: false, error: { name: e.name || 'Error', message: errMsg, stack: e.stack || '' },
        csp: errMsg.includes('Refused to evaluate') || errMsg.includes('unsafe-eval') || errMsg.includes('Content Security Policy') };
  `);
}

function buildCdpScript(code) {
  return buildExecScript(code, `
      return { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } };
  `);
}

// --- WebSocket Clients (ZeroAgent 18765 + dsh 18865) ----------------------
function scheduleProbe() {
  chrome.alarms.create('tmwd-ws-probe', { delayInMinutes: 0.083 });
}

function scheduleKeepalive() {
  chrome.alarms.create('tmwd-ws-keepalive', { delayInMinutes: 0.4 });
}

async function isServerAlive(target) {
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 2000);
    await fetch(target.http, { signal: ctrl.signal });
    return true;
  } catch (e) {
    return false;
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'tmwd-self-reload') {
    chrome.runtime.reload();
    return;
  }
  if (alarm.name === 'tmwd-ws-keepalive') {
    const opened = openSockets();
    if (opened.length) {
      for (const socket of opened) {
        try { socket.send('{"type":"ping"}'); } catch (_) {}
      }
      scheduleKeepalive();
    } else {
      for (const socket of socketsByUrl.values()) {
        try { socket.close(); } catch (_) {}
      }
      socketsByUrl.clear();
      scheduleProbe();
    }
    return;
  }
  if (alarm.name === 'tmwd-ws-probe') {
    let pending = 0;
    for (const target of WS_TARGETS) {
      const existing = socketsByUrl.get(target.ws);
      if (existing && existing.readyState <= 1) continue;
      pending += 1;
      if (await isServerAlive(target)) connectWS(target);
    }
    // Keep probing only the targets that are still missing, so a driver that
    // starts later is picked up without disturbing an already connected one.
    if (pending > 0) scheduleProbe();
  }
});

async function handleWsExec(data, socket) {
  const tabId = data.tabId;
  console.log('[TMWD-WS] Exec request', data.id, 'on tab', tabId);
  socket.send(JSON.stringify({ type: 'ack', id: data.id }));
  if (!tabId) {
    socket.send(JSON.stringify({ type: 'error', id: data.id, error: 'No tabId provided' }));
    return;
  }
  const newTabIds = new Set();
  const onCreated = (tab) => { newTabIds.add(tab.id); };
  chrome.tabs.onCreated.addListener(onCreated);
  try {
    let res;
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: async (s) => await eval(s),
        args: [buildPageScript(data.code)]
      });
      res = Array.isArray(result) ? result[0]?.result : undefined;
      if (res === null || res === undefined) {
        console.log('[TMWD-WS] executeScript returned null/undefined, treating as CSP issue');
        res = { ok: false, error: { name: 'Error', message: 'executeScript returned null (possible CSP or context issue)', stack: '' }, csp: true };
      }
    } catch (e) {
      console.log('[TMWD-WS] scripting.executeScript failed:', e.message);
      res = { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' }, csp: true };
    }
    if (res && !res.ok && res.csp) {
      console.log('[TMWD-WS] CDP fallback for tab', tabId);
      const wrappedCode = buildCdpScript(data.code);
      const attachedHere = await ensureCdpAttached(tabId);
      try {
        const cdpRes = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
          expression: wrappedCode, awaitPromise: true, returnByValue: true
        });
        if (cdpRes.exceptionDetails) {
          const desc = cdpRes.exceptionDetails.exception?.description || 'CDP Error';
          res = { ok: false, error: { name: 'Error', message: desc, stack: desc } };
        } else {
          res = cdpRes.result.value;
        }
      } catch (cdpErr) {
        res = { ok: false, error: { name: 'Error', message: 'CDP fallback failed: ' + cdpErr.message, stack: '' } };
      } finally {
        if (attachedHere) await detachCdp(tabId);
      }
    }
    if (newTabIds.size === 0) await new Promise(r => setTimeout(r, 200));
    chrome.tabs.onCreated.removeListener(onCreated);
    const newTabs = [];
    for (const id of newTabIds) {
      try { const t = await chrome.tabs.get(id); newTabs.push({ id: t.id, url: t.url, title: t.title }); } catch (_) {}
    }
    if (res?.ok) {
      socket.send(JSON.stringify({ type: 'result', id: data.id, result: res.data, newTabs }));
    } else {
      console.log(res);
      socket.send(JSON.stringify({ type: 'error', id: data.id, error: res?.error || 'Unknown error', newTabs }));
    }
  } catch (e) {
    socket.send(JSON.stringify({ type: 'error', id: data.id, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } }));
  } finally {
    chrome.tabs.onCreated.removeListener(onCreated);
  }
}

function connectWS(target) {
  const url = target.ws;
  const existing = socketsByUrl.get(url);
  if (existing && existing.readyState <= 1) return;
  console.log('[TMWD-WS] Connecting to', url);
  let socket;
  try {
    socket = new WebSocket(url);
  } catch (e) {
    console.error('[TMWD-WS] Constructor error:', e);
    socketsByUrl.delete(url);
    scheduleProbe();
    return;
  }
  socketsByUrl.set(url, socket);
  socket.onopen = async () => {
    if (socketsByUrl.get(url) !== socket) return;
    console.log('[TMWD-WS] Connected to', url);
    scheduleKeepalive();
    const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
    if (socketsByUrl.get(url) !== socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: 'ext_ready', tabs: tabs.map(tabSnapshot) }));
    console.log('[TMWD-WS] Sent ext_ready with', tabs.length, 'tabs');
  };
  socket.onmessage = async (event) => {
    if (socketsByUrl.get(url) !== socket) return;
    try {
      const data = JSON.parse(event.data);
      if (data.id && data.code) {
        let code = data.code;
        if (typeof code === 'string') {
          try { const p = JSON.parse(code); if (p && typeof p === 'object') code = p; } catch (_) {}
        }
        if (typeof code === 'object' && code !== null && code.cmd) {
          if (code.tabId === undefined && data.tabId !== undefined) code.tabId = data.tabId;
          const res = await handleExtMessage(code, {});
          socket.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        } else if (typeof code === 'string') {
          await handleWsExec(data, socket);
        } else if (typeof code === 'object' && code !== null) {
          const msg = code.tabId === undefined && data.tabId !== undefined ? { ...code, tabId: data.tabId } : code;
          const res = await handleExtMessage(msg, {});
          socket.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        }
      }
    } catch (e) {
      console.error('[TMWD-WS] message parse error', e);
    }
  };
  socket.onclose = () => {
    console.log('[TMWD-WS] Disconnected from', url);
    if (socketsByUrl.get(url) === socket) socketsByUrl.delete(url);
    scheduleProbe();
  };
  socket.onerror = (e) => {
    console.error('[TMWD-WS] Error:', e);
  };
}

function connectAll() {
  for (const target of WS_TARGETS) connectWS(target);
}

// Initial connect + wake-up hooks
connectAll();
interceptsReady.then(() => restoreIntercepts()).catch(() => {});
chrome.runtime.onStartup.addListener(() => connectAll());
chrome.runtime.onInstalled.addListener(() => connectAll());

// Sync tab list on changes
async function sendTabsUpdate() {
  const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
  wsSend({ type: 'tabs_update', tabs: tabs.map(tabSnapshot) });
}
chrome.tabs.onUpdated.addListener((_, changeInfo) => {
  if (changeInfo.status === 'complete' || changeInfo.url) sendTabsUpdate();
});
chrome.tabs.onRemoved.addListener((tabId) => {
  cdpAttachedTabs.delete(tabId);
  cdpEventBuffers.delete(String(tabId));
  if (interceptRules.delete(String(tabId))) saveIntercepts();
  sendTabsUpdate();
});
chrome.tabs.onCreated.addListener(() => sendTabsUpdate());
chrome.tabs.onActivated.addListener(() => sendTabsUpdate());
