// ZeroAgent Web2 browser bridge adapter.
// HTTP is the command/data channel. WebSocket only carries small state events.
(() => {
  'use strict';

  const listeners = new Map();
  let ws = null;
  let cachedBridgeReady = null;
  const bridgeBase = `${location.protocol}//${location.hostname}:14168`;
  const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:14168/ws`;

  function on(channel, cb) {
    if (typeof cb !== 'function') return () => {};
    if (!listeners.has(channel)) listeners.set(channel, new Set());
    listeners.get(channel).add(cb);
    if (channel === 'bridge-ready' && cachedBridgeReady) {
      try { cb(cachedBridgeReady); } catch (err) { console.error('[za-web listener] replay bridge-ready', err); }
    }
    return () => listeners.get(channel)?.delete(cb);
  }

  function emit(channel, payload) {
    if (channel === 'bridge-ready') cachedBridgeReady = payload;
    const set = listeners.get(channel);
    if (!set) return;
    for (const cb of Array.from(set)) {
      try { cb(payload); } catch (err) { console.error('[za-web listener]', channel, err); }
    }
  }

  async function http(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    const init = Object.assign({}, options, { headers });
    if (init.body && typeof init.body !== 'string') {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      init.body = JSON.stringify(init.body);
    }
    const res = await fetch(`${bridgeBase}${path}`, init);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
    if (!res.ok) {
      const err = new Error((data && (data.error || data.message)) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function connectWs() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    try {
      ws = new WebSocket(wsUrl);
      ws.addEventListener('open', () => emit('bridge-log', 'WS state channel connected'));
      ws.addEventListener('message', (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.type === 'bridge-ready') {
          emit('bridge-ready', msg);
        } else if (msg.type === 'session-state') {
          emit('bridge-notification', msg);
        } else if (msg.type === 'bridge-log') {
          emit('bridge-log', msg.payload || msg);
        } else if (msg.type === 'bridge-error') {
          emit('bridge-error', msg.payload || msg);
        }
      });
      ws.addEventListener('close', () => emit('bridge-closed', { reason: 'ws-closed' }));
      ws.addEventListener('error', () => emit('bridge-error', { type: 'ws-error', message: 'WebSocket state channel error' }));
    } catch (err) {
      emit('bridge-error', { type: 'ws-error', message: err.message || String(err) });
    }
  }

  async function rpc(method, params = {}) {
    switch (method) {
      case 'app/status':
        return http('/status');
      case 'app/config/get':
        return http('/config');
      case 'app/config/save':
        return http('/config', { method: 'POST', body: params || {} });
      case 'get/model-profiles':
        return http('/model-profiles');
      case 'slash/commands':
        return http('/slash/commands');
      case 'slash/resolve':
        return http('/slash/resolve', { method: 'POST', body: params || {} });
      case 'scheduler/status':
        return http('/scheduler');
      case 'scheduler/start':
        return http('/scheduler/start', { method: 'POST', body: params || {} });
      case 'history/sessions':
        return http(`/history/sessions?limit=${encodeURIComponent(params.limit ?? 10)}`);
      case 'history/resume':
        return http('/history/resume', { method: 'POST', body: params || {} });
      case 'session/new':
        return http('/session/new', { method: 'POST', body: params || {} });
      case 'session/prompt': {
        const sid = params.sessionId || params.id || params.bridgeSessionId;
        if (!sid) throw new Error('session/prompt missing sessionId');
        return http(`/session/${encodeURIComponent(sid)}/prompt`, { method: 'POST', body: params || {} });
      }
      case 'session/poll': {
        const sid = params.sessionId || params.id || params.bridgeSessionId;
        if (!sid) throw new Error('session/poll missing sessionId');
        const after = params.afterId ?? params.after ?? 0;
        const limit = params.limit ?? 200;
        return http(`/session/${encodeURIComponent(sid)}/messages?after=${encodeURIComponent(after)}&limit=${encodeURIComponent(limit)}`);
      }
      case 'session/cancel': {
        const sid = params.sessionId || params.id || params.bridgeSessionId;
        if (!sid) throw new Error('session/cancel missing sessionId');
        return http(`/session/${encodeURIComponent(sid)}/cancel`, { method: 'POST', body: params || {} });
      }
      case 'app/path/open':
        return http('/path/open', { method: 'POST', body: params || {} });
      case 'app/path/selectProjectRoot':
        return http('/config');
      case 'list_continuable_sessions':
        return { sessions: [] };
      case 'restore_session':
        throw new Error('restore_session is not implemented in web2 bridge');
      default:
        throw new Error(`Unknown RPC method: ${method}`);
    }
  }

  window.zeroAgent = {
    platform: navigator.platform.toLowerCase().includes('mac') ? 'darwin' : 'win32',
    startBridge: async () => { connectWs(); return http('/status'); },
    stopBridge: async () => ({ ok: true }),
    checkStatus: () => rpc('app/status', {}),
    getConfig: () => rpc('app/config/get', {}),
    saveConfig: (cfg) => rpc('app/config/save', cfg || {}),
    getModelProfiles: () => rpc('get/model-profiles', {}),
    getSlashCommands: () => rpc('slash/commands', {}),
    resolveSlash: (command, args = '') => rpc('slash/resolve', { command, args }),
    listResumeSessions: (limit = 10) => rpc('history/sessions', { limit }),
    resumeSession: (sessionId, index) => rpc('history/resume', { sessionId, index }),
    selectProjectRoot: () => rpc('app/path/selectProjectRoot', {}),
    openConfig: () => rpc('app/path/open', { kind: 'config' }),
    pollSession: (sessionId, afterId = 0) => rpc('session/poll', { sessionId, afterId }),
    rpc,
    onBridgeMessage: (cb) => on('bridge-message', cb),
    onBridgeNotification: (cb) => on('bridge-notification', cb),
    onBridgeError: (cb) => on('bridge-error', cb),
    onBridgeClosed: (cb) => on('bridge-closed', cb),
    onBridgeReady: (cb) => on('bridge-ready', cb),
    onBridgeLog: (cb) => on('bridge-log', cb),
    onOpenSearch: (cb) => on('open-search', cb)
  };

  connectWs();
  http('/status').then(status => emit('bridge-ready', status)).catch(err => emit('bridge-error', { type: 'http-error', message: err.message || String(err) }));
})();
