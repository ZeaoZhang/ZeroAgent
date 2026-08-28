'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'app.js');
const adapterPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'za-web.js');

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  toggle(name, force) { const next = force === undefined ? !this.values.has(name) : !!force; if (next) this.add(name); else this.remove(name); return next; }
  contains(name) { return this.values.has(name); }
}
class FakeElement {
  constructor(tagName) { this.tagName = tagName.toUpperCase(); this.children = []; this.parentNode = null; this.className = ''; this.classList = new FakeClassList(); this.dataset = {}; this.style = {}; this.attributes = {}; this.listeners = new Map(); this.textContent = ''; this.innerHTML = ''; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  removeChild(node) { const i = this.children.indexOf(node); if (i >= 0) this.children.splice(i, 1); node.parentNode = null; return node; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  scrollTo() {}
  focus() {}
}

function makeAppContext() {
  const elements = new Map();
  const document = {
    body: new FakeElement('body'),
    createElement: (tag) => new FakeElement(tag),
    createDocumentFragment: () => new FakeElement('fragment'),
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll: () => [],
  };
  for (const id of ['messages', 'agent-list', 'right-drawer', 'session-title', 'current-model', 'token-usage']) elements.set(id, new FakeElement('div'));
  const context = { console, Date, Math, JSON, Set, Map, RegExp, String, Number, Boolean, Array, Object, Error, TypeError, Promise, setTimeout, clearTimeout, performance: { now: () => 1000 }, navigator: { platform: 'test' }, localStorage: { getItem: () => null, setItem: () => {} }, document, window: {} };
  context.globalThis = context;
  return context;
}

const source = fs.readFileSync(appPath, 'utf8');
const marker = source.indexOf('// ─── Bridge events');
const panelStart = source.indexOf('// ─── Agent Panel');
const initMarker = source.indexOf('// ─── Init');
assert.ok(marker > 0 && panelStart > marker && initMarker > panelStart);
const context = makeAppContext();
vm.runInNewContext(source.slice(0, marker) + source.slice(panelStart, initMarker) + `
renderMessage = () => null;
renderSessionList = () => null;
updateModelStatus = () => null;
setBusy = () => null;
pollSessionMessages = () => null;
showError = (message) => { globalThis.__error = message; };
messagesEl = document.getElementById('messages');
globalThis.__testExports = {
  state, createLocalSession, hydrateBridgeSessions, handleNotification,
  renderAgentPanel, setActiveSession, cancelAgent, getSessionRuntime,
  setAgentElements: (list, drawer) => { agentList = list; rightDrawer = drawer; },
};
`, context, { filename: appPath });
const t = context.__testExports;
const agentList = context.document.getElementById('agent-list');
const rightDrawer = context.document.getElementById('right-drawer');
t.setAgentElements(agentList, rightDrawer);

async function appTests() {
  const hydratedAgent = { id: 'agent-aaaaaaaaaaaa', name: 'ZeroAgent', type: 'session-agent', status: 'running', created_at: 1000, updated_at: 1000, reason: '' };
  context.window.zeroAgent = { rpc: async (method) => method === 'session/poll' ? { messages: [{ id: 1, role: 'user', content: 'hello' }] } : { agents: [] } };
  await t.hydrateBridgeSessions({ sessions: [{ id: 'bridge-a', title: 'A', subAgents: [hydratedAgent] }] });
  const sessionA = [...t.state.sessions.values()][0];
  assert.deepEqual(t.state.activeAgents.get(sessionA.id), [hydratedAgent]);

  t.handleNotification({ type: 'session-state', sessionId: 'bridge-a', subAgents: [{ ...hydratedAgent, status: 'completed', reason: 'done' }] });
  assert.equal(t.state.activeAgents.get(sessionA.id)[0].status, 'completed');
  assert.equal(t.state.activeAgents.get(sessionA.id).length, 1);

  t.state.activeId = sessionA.id;
  t.renderAgentPanel();
  assert.equal(agentList.children.length, 1);
  assert.match(agentList.children[0].innerHTML, /ZeroAgent/);
  assert.match(agentList.children[0].innerHTML, /session-agent/);
  assert.match(agentList.children[0].innerHTML, /completed/);
  assert.match(agentList.children[0].innerHTML, /Runtime:/);
  assert.equal(rightDrawer.classList.contains('collapsed'), false);

  const sessionB = t.createLocalSession('local-b', 'B', 'bridge-b');
  const calls = [];
  const pending = [];
  context.window.zeroAgent.rpc = (method, params) => {
    if (method !== 'session/agents') return Promise.resolve({ agents: [] });
    calls.push(params.sessionId);
    return new Promise((resolve) => pending.push({ sid: params.sessionId, resolve }));
  };
  t.setActiveSession(sessionA.id);
  t.setActiveSession(sessionB.id);
  t.setActiveSession(sessionA.id);
  assert.deepEqual(calls, ['bridge-a', 'bridge-b', 'bridge-a']);
  pending[0].resolve({ agents: [{ id: 'agent-old', status: 'failed' }] });
  pending[2].resolve({ agents: [{ id: 'agent-new', status: 'waiting' }] });
  pending[1].resolve({ agents: [{ id: 'agent-b', status: 'completed' }] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(t.state.activeAgents.get(sessionA.id)[0].id, 'agent-new');
  assert.equal(t.state.activeAgents.get(sessionB.id)[0].id, 'agent-b');

  context.window.zeroAgent.rpc = async (method, params) => {
    assert.equal(method, 'session/agent/cancel');
    assert.equal(params.sessionId, 'bridge-a');
    assert.equal(params.agentId, 'agent-new');
    return { agents: [{ id: 'agent-new', status: 'cancelled', reason: 'user_cancelled' }] };
  };
  await t.cancelAgent('bridge-a', 'agent-new');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(t.state.activeAgents.get(sessionA.id)[0].status, 'cancelled');
}

async function adapterTests() {
  const calls = [];
  const windowObj = { location: { protocol: 'http:', hostname: '127.0.0.1', hash: '', pathname: '/', search: '' }, history: { replaceState() {} } };
  class FakeWebSocket { static get CONNECTING() { return 0; } static get OPEN() { return 1; } constructor() { this.readyState = 0; } addEventListener() {} }
  const ctx = { window: windowObj, location: windowObj.location, document: {}, navigator: { platform: 'MacIntel' }, WebSocket: FakeWebSocket, console, fetch: async (url, init = {}) => { calls.push({ url, init }); return { ok: true, status: 200, statusText: 'OK', text: async () => '{}' }; } };
  ctx.globalThis = ctx;
  vm.runInNewContext(fs.readFileSync(adapterPath, 'utf8'), ctx, { filename: adapterPath });
  calls.length = 0;
  await windowObj.zeroAgent.rpc('session/agents', { sessionId: 'bridge/a' });
  assert.equal(calls[0].url, 'http://127.0.0.1:14168/session/bridge%2Fa/agents');
  assert.equal(calls[0].init.method, undefined);
  await windowObj.zeroAgent.rpc('session/agent/cancel', { sessionId: 'bridge/a', agentId: 'agent/1' });
  assert.equal(calls[1].url, 'http://127.0.0.1:14168/session/bridge%2Fa/agents/agent%2F1/cancel');
  assert.equal(calls[1].init.method, 'POST');
}

Promise.all([appTests(), adapterTests()]).then(() => console.log('frontend_agents.test.js OK')).catch((error) => { console.error(error); process.exit(1); });
