'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const slashCommandsMarker = source.indexOf('// ─── Slash commands');
assert.ok(slashCommandsMarker > 0, 'session management section must exist');

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : !!force;
    if (next) this.values.add(name); else this.values.delete(name);
    return next;
  }
  contains(name) { return this.values.has(name); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = new Map();
    this.textContent = '';
    this.innerHTML = '';
    this.title = '';
    this.disabled = false;
    this.value = '';
    this.focused = false;
  }

  append(...nodes) {
    nodes.forEach(node => this.appendChild(node));
  }

  appendChild(node) {
    this.children.push(node);
    node.parentNode = this;
    return node;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  async click() {
    const listener = this.listeners.get('click');
    if (!listener) return undefined;
    return listener({
      target: this,
      currentTarget: this,
      stopPropagation() {},
      preventDefault() {},
    });
  }

  focus() { this.focused = true; }
  select() { this.selected = true; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 20, height: 20 }; }
}

function makeContext() {
  const elements = new Map();
  const document = {
    body: new FakeElement('body'),
    createElement: tagName => new FakeElement(tagName),
    getElementById: id => elements.get(id) || null,
    querySelectorAll: () => [],
  };
  const context = {
    console,
    Date,
    Math,
    JSON,
    Set,
    Map,
    RegExp,
    String,
    Number,
    Boolean,
    Array,
    Object,
    Error,
    TypeError,
    setTimeout,
    clearTimeout,
    performance: { now: () => 1000 },
    navigator: { platform: 'test' },
    localStorage: { getItem: () => null, setItem: () => {} },
    document,
    window: {},
  };
  context.globalThis = context;
  return context;
}

const context = makeContext();
const testExports = `
  globalThis.__testExports = {
    state,
    createLocalSession,
    createSessionItem,
    deleteSession,
    closeSession,
    assignSessionToGroup,
    createGroup,
    renderSessionList,
    getSessionTimeBucket: typeof getSessionTimeBucket === 'function' ? getSessionTimeBucket : null,
    compareSessionsByUpdatedAt: typeof compareSessionsByUpdatedAt === 'function' ? compareSessionsByUpdatedAt : null,
    setConfirmHook: (fn) => { showConfirmDialog = fn; },
    setErrorHook: (fn) => { showError = fn; },
    setActiveHook: (fn) => { setActiveSession = fn; },
    setRenderHook: (fn) => { renderSessionList = fn; },
    setSessionListElement: (element) => { sessionListEl = element; },
  };
`;
vm.runInNewContext(source.slice(0, slashCommandsMarker) + testExports, context, {
  filename: appPath,
});

const exported = context.__testExports;
const { state } = exported;
exported.setRenderHook(() => {});

function resetState() {
  state.sessions.clear();
  state.sessionGroups.clear();
  state.runtimeBySessionId.clear();
  state.activeAgents.clear();
  state.activeId = null;
}

async function testDeleteUsesBridgeIdAndSelectsNewestRemaining() {
  resetState();
  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      if (method === 'session/delete') return { ok: true };
      throw new Error(`unexpected RPC ${method}`);
    },
  };
  exported.setConfirmHook(async () => true);
  exported.setErrorHook(() => { throw new Error('unexpected deletion error'); });
  exported.setActiveHook(id => { state.activeId = id; });

  const active = { id: 'local-active', bridgeSessionId: 'bridge-active', title: 'Active', messages: [], updatedAt: 3_000 };
  const old = { id: 'local-old', bridgeSessionId: 'bridge-old', title: 'Old', messages: [], updatedAt: 1_000 };
  const newest = { id: 'local-newest', bridgeSessionId: 'bridge-newest', title: 'Newest', messages: [], updatedAt: 5_000 };
  state.sessions.set(active.id, active);
  state.sessions.set(old.id, old);
  state.sessions.set(newest.id, newest);
  state.activeId = active.id;

  const item = exported.createSessionItem(active);
  const deleteButton = item.children.find(child => child.className === 'session-delete');
  assert.ok(deleteButton, 'session delete button must be present');
  await deleteButton.click();

  assert.equal(state.sessions.has(active.id), false);
  assert.equal(state.activeId, newest.id, 'active deletion should select newest remaining session');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'session/delete');
  assert.equal(calls[0].params.sessionId, 'bridge-active');
}

async function testDelete404IsIdempotentAndNon404RetainsSession() {
  resetState();
  const gone = { id: 'local-gone', bridgeSessionId: 'bridge-gone', title: 'Gone', messages: [] };
  const failed = { id: 'local-failed', bridgeSessionId: 'bridge-failed', title: 'Failed', messages: [] };
  state.sessions.set(gone.id, gone);
  state.sessions.set(failed.id, failed);
  state.activeId = failed.id;
  const errors = [];
  exported.setConfirmHook(async () => true);
  exported.setErrorHook(message => errors.push(message));
  context.window.zeroAgent = {
    rpc: async (_method, { sessionId }) => {
      if (sessionId === gone.bridgeSessionId) {
        const error = new Error('session not found');
        error.status = 404;
        throw error;
      }
      const error = new Error('bridge unavailable');
      error.status = 503;
      throw error;
    },
  };
  exported.setActiveHook(id => { state.activeId = id; });

  await exported.closeSession(gone.id);
  assert.equal(state.sessions.has(gone.id), false, '404 should clear stale local session');
  await exported.closeSession(failed.id);
  assert.equal(state.sessions.has(failed.id), true, 'non-404 should preserve local session');
  assert.equal(errors.length, 1);
}

async function testSoleSessionUsesReplacementAndKeepsLocalContainer() {
  resetState();
  const only = { id: 'local-only', bridgeSessionId: 'bridge-only', title: 'Only', messages: [{ role: 'user', content: 'old' }], groupId: 'work' };
  state.sessions.set(only.id, only);
  state.activeId = only.id;
  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      return { sessionId: 'bridge-replacement', session: { id: 'bridge-replacement', title: 'New chat' } };
    },
  };
  exported.setErrorHook(() => { throw new Error('unexpected replacement error'); });
  await exported.deleteSession(only.id);

  assert.equal(state.sessions.size, 1);
  assert.equal(state.sessions.has(only.id), true);
  assert.equal(only.bridgeSessionId, 'bridge-replacement');
  assert.equal(only.messages.length, 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'session/replace');
  assert.equal(calls[0].params.sessionId, 'bridge-only');
}

async function testSoleSession404RecoversWithNewRemoteSession() {
  resetState();
  const only = {
    id: 'local-stale-only',
    bridgeSessionId: 'bridge-stale-only',
    cwd: '/tmp/workspace',
    title: 'Stale',
    messages: [{ role: 'user', content: 'old' }],
    groupId: 'work',
  };
  state.sessions.set(only.id, only);
  state.activeId = only.id;
  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      if (method === 'session/replace') {
        const error = new Error('session not found');
        error.status = 404;
        throw error;
      }
      return { sessionId: 'bridge-recovered', session: { id: 'bridge-recovered', title: 'New chat' } };
    },
  };
  await exported.deleteSession(only.id);
  assert.equal(only.bridgeSessionId, 'bridge-recovered');
  assert.equal(only.messages.length, 0);
  assert.deepEqual(calls.map(call => call.method), ['session/replace', 'session/new']);
}

async function testRenderSessionListBucketsAndGroupActions() {
  resetState();
  state.leftDrawerCollapsed = false;
  state.rightDrawerCollapsed = false;
  const list = new FakeElement('div');
  exported.setSessionListElement(list);
  const now = Date.now();
  const grouped = { id: 'local-group', bridgeSessionId: 'bridge-group', title: 'Grouped', messages: [], groupId: 'Work', updatedAt: now };
  const todayNewest = { id: 'local-today-new', bridgeSessionId: 'bridge-today-new', title: 'Today newest', messages: [], updatedAt: now - 1000 };
  const todayOld = { id: 'local-today-old', bridgeSessionId: 'bridge-today-old', title: 'Today old', messages: [], updatedAt: now - 2000 };
  const yesterday = { id: 'local-yesterday', bridgeSessionId: 'bridge-yesterday', title: 'Yesterday', messages: [], updatedAt: now - 86400000 };
  const week = { id: 'local-week', bridgeSessionId: 'bridge-week', title: 'Week', messages: [], updatedAt: now - 3 * 86400000 };
  const older = { id: 'local-older', bridgeSessionId: 'bridge-older', title: 'Older', messages: [], updatedAt: now - 8 * 86400000 };
  [grouped, todayOld, todayNewest, yesterday, week, older].forEach(sess => state.sessions.set(sess.id, sess));
  await exported.createGroup('Work');
  exported.renderSessionList();

  const group = list.children[0];
  assert.equal(group.className, 'session-group');
  assert.equal(group.children[1].children[0].dataset.sessionId, grouped.id);
  const timeHeaders = list.children.filter(child => child.className.includes('session-time-header'));
  assert.deepEqual(timeHeaders.map(header => header.children[1].textContent), ['今天', '昨天', '最近 7 天', '更早']);
  const todayHeader = timeHeaders[0];
  const todaySessions = list.children[list.children.indexOf(todayHeader) + 1];
  assert.equal(todaySessions.children[0].dataset.sessionId, todayNewest.id);
  assert.equal(todaySessions.children[1].dataset.sessionId, todayOld.id);

  context.window.zeroAgent = { rpc: async () => ({ ok: true, groupId: null }) };
  exported.setConfirmHook(async () => true);
  const deleteGroupButton = group.children[0].children[2];
  await deleteGroupButton.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.sessionGroups.has('Work'), false);
  assert.equal(grouped.groupId, null);
}

async function testConcurrentDeletesSharePromise() {
  resetState();
  const session = { id: 'local-concurrent', bridgeSessionId: 'bridge-concurrent', title: 'Concurrent', messages: [] };
  state.sessions.set(session.id, session);
  state.activeId = session.id;
  let resolveRpc;
  let calls = 0;
  context.window.zeroAgent = {
    rpc: async () => {
      calls += 1;
      await new Promise(resolve => { resolveRpc = resolve; });
      return { ok: true };
    },
  };
  exported.setErrorHook(() => {});
  const first = exported.closeSession(session.id);
  const second = exported.closeSession(session.id);
  assert.equal(first, second, 'duplicate deletion must reuse the pending promise');
  resolveRpc();
  await first;
  assert.equal(calls, 1);
}

async function testGroupAssignmentAndTimeHelpers() {
  resetState();
  const session = { id: 'local-grouped', bridgeSessionId: 'bridge-grouped', title: 'Grouped', messages: [], groupId: null };
  state.sessions.set(session.id, session);
  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      return { ok: true, groupId: params.groupId };
    },
  };
  await exported.createGroup('Work');
  await exported.assignSessionToGroup(session.id, 'Work');
  await exported.assignSessionToGroup(session.id, null);
  assert.equal(session.groupId, null);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].method, 'session/group');
  assert.equal(calls[0].params.sessionId, 'bridge-grouped');
  assert.equal(calls[0].params.groupId, 'Work');
  assert.equal(calls[1].method, 'session/group');
  assert.equal(calls[1].params.sessionId, 'bridge-grouped');
  assert.equal(calls[1].params.groupId, null);

  assert.equal(typeof exported.getSessionTimeBucket, 'function');
  assert.equal(typeof exported.compareSessionsByUpdatedAt, 'function');
  const now = new Date('2026-08-05T12:00:00').getTime();
  assert.equal(exported.getSessionTimeBucket(now - 1_000, now), 'today');
  assert.equal(exported.getSessionTimeBucket(now - 86400000, now), 'yesterday');
  assert.equal(exported.getSessionTimeBucket(now - 3 * 86400000, now), 'week');
  assert.equal(exported.getSessionTimeBucket(now - 8 * 86400000, now), 'older');
  assert.ok(exported.compareSessionsByUpdatedAt({ updatedAt: now }, { updatedAt: now - 1 }) < 0);
}

(async () => {
  await testDeleteUsesBridgeIdAndSelectsNewestRemaining();
  await testDelete404IsIdempotentAndNon404RetainsSession();
  await testSoleSessionUsesReplacementAndKeepsLocalContainer();
  await testSoleSession404RecoversWithNewRemoteSession();
  await testConcurrentDeletesSharePromise();
  await testGroupAssignmentAndTimeHelpers();
  await testRenderSessionListBucketsAndGroupActions();
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
