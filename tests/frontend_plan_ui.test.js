'use strict';

// UI-level coverage for the desktop `/plan` interaction in app.js.
// No real bridge/LLM: a fake DOM plus a faked window.zeroAgent exercise the
// slash handler, session-state plan metadata merge, and the ready->execute
// callback. The source is sliced just before the bridge-event registrations
// (which would otherwise run window.zeroAgent.onBridge* at load).

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const bridgeEventsMarker = source.indexOf('// ─── Bridge events');
assert.ok(bridgeEventsMarker > 0, 'bridge events marker must exist');

// ─── Fake DOM ──────────────────────────────────────────────────────────────
class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
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
    this.type = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }

  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }

  appendChild(node) {
    this.children.push(node);
    node.parentNode = this;
    return node;
  }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index !== -1) this.children.splice(index, 1);
    node.parentNode = null;
    return node;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  get firstChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
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

  scrollTo() {}

  focus() {}

  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function makeContext() {
  const elements = new Map();
  const document = {
    body: new FakeElement('body'),
    createElement: (tagName) => new FakeElement(tagName),
    createDocumentFragment: () => new FakeElement('#fragment'),
    getElementById: (id) => elements.get(id) || null,
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
renderMessage = () => null;
renderSessionList = () => null;
updateModelStatus = () => null;
renderAgentPanel = () => null;
updateSendButton = () => null;
setBusy = (busy, label, sess) => { if (sess) getSessionRuntime(sess).busy = busy; };
hideError = () => null;
showError = () => null;
startTaskTimer = () => null;
stopTaskTimer = (sess) => { if (sess) { const r = getSessionRuntime(sess); r.taskTimerId = null; r.taskStartedAt = 0; } };
runAgentSlash = async () => { globalThis.__runAgentSlashCalls = (globalThis.__runAgentSlashCalls || 0) + 1; };
sendPrompt = async () => { globalThis.__sendPromptCalls = (globalThis.__sendPromptCalls || 0) + 1; };
globalThis.__testExports = {
  state,
  LOCAL_SLASH_COMMANDS,
  handleSlash,
  handleNotification,
  executePlanForSession,
  renderPlanStatus,
  renderMessages,
  createLocalSession,
  getSessionRuntime,
  getActiveSessionRuntime,
  restoreResumeIndex,
  hydrateBridgeSessions,
  applyCommandSuggestion,
  setMessagesElement: (el) => { messagesEl = el; },
  setInputEl: (el) => { inputEl = el; },
  setEnsureBridgeSession: (fn) => { ensureBridgeSession = fn; },
};
`;

vm.runInNewContext(source.slice(0, bridgeEventsMarker) + testExports, context, { filename: appPath });

const t = context.__testExports;
const { state } = t;

const messagesEl = new FakeElement('div');
t.setMessagesElement(messagesEl);
t.setEnsureBridgeSession(async (sess) => sess.bridgeSessionId || sess.id);
const inputEl = new FakeElement('textarea');
t.setInputEl(inputEl);

function resetCounters() {
  context.__runAgentSlashCalls = 0;
  context.__sendPromptCalls = 0;
}

function findByClass(el, cls) {
  if (!el) return null;
  const names = String(el.className || '').split(/\s+/).filter(Boolean);
  if (names.includes(cls) || (el.classList && el.classList.contains(cls))) return el;
  for (const child of el.children || []) {
    const found = findByClass(child, cls);
    if (found) return found;
  }
  return null;
}


(async () => {
  resetCounters();

  // 1. /plan is registered as a local slash command.
  const planCmd = t.LOCAL_SLASH_COMMANDS.find((c) => c.cmd === '/plan');
  assert.ok(planCmd, '/plan must be listed in LOCAL_SLASH_COMMANDS');
  assert.equal(planCmd.argHint, '[task]');
  assert.match(planCmd.description, /计划/);

  // 2. New sessions initialize plan metadata to inert defaults.
  const initSession = t.createLocalSession('local-init', 'Init', 'bridge-init');
  assert.equal(initSession.planPath, null);
  assert.equal(initSession.planStatus, 'inactive');
  assert.equal(initSession.planTask, '');

  // 3. Empty /plan shows usage and never calls startPlan.
  let startPlanCalls = [];
  context.window.zeroAgent = {
    startPlan: async (sid, task) => { startPlanCalls.push({ sid, task }); return {}; },
    executePlan: async (sid) => { throw new Error('executePlan must not run'); },
  };
  state.sessions.clear();
  const emptySession = t.createLocalSession('local-empty', 'Empty', 'bridge-empty');
  state.activeId = emptySession.id;
  await t.handleSlash('/plan');
  assert.equal(startPlanCalls.length, 0, 'empty /plan must not start a plan');
  assert.equal(emptySession.messages.at(-1).content, 'Usage: /plan <task>');
  assert.equal(context.__runAgentSlashCalls, 0, '/plan must not fall through to runAgentSlash');

  // 4. Busy session: /plan uses the existing busy guard and does not start.
  const busySession = t.createLocalSession('local-busy', 'Busy', 'bridge-busy');
  state.activeId = busySession.id;
  t.getSessionRuntime(busySession).busy = true;
  startPlanCalls.length = 0;
  await t.handleSlash('/plan build it');
  assert.equal(startPlanCalls.length, 0, 'busy /plan must not call startPlan');
  assert.match(busySession.messages.at(-1).content, /responding/i);
  t.getSessionRuntime(busySession).busy = false;

  // 5. /plan calls startPlan(ensureBridgeSession(sess), arg), merges metadata,
  //    shows a confirmation, and never routes through the normal prompt path.
  context.window.zeroAgent.startPlan = async (sid, task) => {
    startPlanCalls.push({ sid, task });
    return { ok: true, planPath: '/tmp/plans/demo/plan.md', planTask: 'build it' };
  };
  startPlanCalls.length = 0;
  await t.handleSlash('/plan build it');
  assert.equal(startPlanCalls.length, 1, 'startPlan must be called once');
  assert.equal(startPlanCalls[0].sid, 'bridge-busy');
  assert.equal(startPlanCalls[0].task, 'build it');
  assert.equal(busySession.planPath, '/tmp/plans/demo/plan.md');
  assert.equal(busySession.planTask, 'build it');
  assert.equal(busySession.planStatus, 'planning', 'missing planStatus defaults to planning');
  assert.match(busySession.messages.at(-1).content, /Plan created/);
  assert.equal(context.__sendPromptCalls, 0, '/plan must not send a normal prompt');
  assert.equal(context.__runAgentSlashCalls, 0);

  // 6. An explicit planStatus from startPlan is preserved verbatim.
  const readyFromStart = t.createLocalSession('local-ready-start', 'Ready', 'bridge-ready-start');
  state.activeId = readyFromStart.id;
  context.window.zeroAgent.startPlan = async () => ({ ok: true, planPath: '/p.md', planStatus: 'ready', planTask: 'ship it' });
  await t.handleSlash('/plan ship it');
  assert.equal(readyFromStart.planStatus, 'ready');
  assert.equal(readyFromStart.planPath, '/p.md');

  // 7. session-state notifications merge plan metadata.
  const notifiedSession = t.createLocalSession('local-notified', 'Notified', 'bridge-notified');
  state.activeId = notifiedSession.id;
  t.handleNotification({
    type: 'session-state',
    sessionId: 'bridge-notified',
    planPath: '/notified/plan.md',
    planStatus: 'executing',
    planTask: 'notified task',
  });
  assert.equal(notifiedSession.planPath, '/notified/plan.md');
  assert.equal(notifiedSession.planStatus, 'executing');
  assert.equal(notifiedSession.planTask, 'notified task');

  // 8. A ready plan renders an execute button but does NOT auto-execute;
  //    only an explicit click calls executePlan exactly once and refreshes.
  const executeCalls = [];
  context.window.zeroAgent.executePlan = async (sid) => {
    executeCalls.push(sid);
    return { ok: true, planStatus: 'executing' };
  };
  const readySession = t.createLocalSession('local-ready', 'Ready', 'bridge-ready');
  state.activeId = readySession.id;
  readySession.planStatus = 'ready';
  readySession.planPath = '/ready/plan.md';
  readySession.planTask = 'execute me';
  const card = t.renderPlanStatus(readySession);
  assert.ok(card, 'renderPlanStatus must produce a card for a ready plan');
  assert.equal(executeCalls.length, 0, 'ready must not auto-execute');

  const btn = findByClass(card, 'plan-execute-btn');
  assert.ok(btn, 'ready card must expose an execute button');
  assert.equal(btn.textContent, '执行计划');
  assert.equal(btn.type, 'button');

  await btn.click();
  assert.equal(executeCalls.length, 1, 'click must call executePlan exactly once');
  assert.equal(executeCalls[0], 'bridge-ready');
  assert.equal(readySession.planStatus, 'executing', 'status refreshes after execution');

  // 9. Command palette: /plan keeps the cursor for its task argument and does
  //    NOT auto-submit; genuinely no-argument commands still auto-submit.
  assert.equal(t.applyCommandSuggestion({ cmd: '/plan', argHint: '[task]', description: 'plan' }), false,
    '/plan selection must not auto-submit');
  assert.equal(inputEl.value, '/plan ', '/plan must leave a trailing space for the task');
  assert.equal(inputEl.selectionStart, inputEl.value.length, 'cursor stays at end for typing');
  assert.equal(t.applyCommandSuggestion({ cmd: '/help', argHint: '', description: 'help' }), true,
    'no-argument command must auto-submit');
  assert.equal(inputEl.value, '/help');

  // 10. Duplicate /plan while a start is in flight reuses the same RPC.
  const dupSession = t.createLocalSession('local-dup', 'Dup', 'bridge-dup');
  state.activeId = dupSession.id;
  let resolveStart;
  const startGate = new Promise((res) => { resolveStart = res; });
  let dupStartCalls = 0;
  context.window.zeroAgent.startPlan = async (sid, task) => { dupStartCalls += 1; return startGate; };
  const dupP1 = t.handleSlash('/plan dup it');
  const dupP2 = t.handleSlash('/plan dup it');
  resolveStart({ ok: true, planPath: '/dup/plan.md', planTask: 'dup it' });
  await dupP1;
  await dupP2;
  assert.equal(dupStartCalls, 1, 'duplicate /plan must issue a single startPlan RPC');
  assert.equal(dupSession.planPath, '/dup/plan.md');
  assert.equal(dupSession.planStatus, 'planning');

  // 11. Double-clicking the execute button reuses the in-flight execute RPC.
  const execEl = new FakeElement('div');
  t.setMessagesElement(execEl);
  const execSession = t.createLocalSession('local-exec-dup', 'Exec', 'bridge-exec');
  state.activeId = execSession.id;
  execSession.planStatus = 'ready';
  execSession.planPath = '/exec/plan.md';
  execSession.planTask = 'run me';
  let resolveExec;
  const execGate = new Promise((res) => { resolveExec = res; });
  const execDupCalls = [];
  context.window.zeroAgent.executePlan = async (sid) => { execDupCalls.push(sid); return execGate; };
  const execCard = t.renderPlanStatus(execSession);
  const execBtn = findByClass(execCard, 'plan-execute-btn');
  assert.equal(execBtn.disabled, false, 'execute button starts enabled');
  const execP1 = execBtn.click();
  const inFlightBtn = findByClass(execEl, 'plan-execute-btn');
  assert.equal(inFlightBtn.disabled, true, 'execute button must disable while in flight');
  assert.equal(inFlightBtn.textContent, '执行中…');
  const execP2 = execBtn.click();
  resolveExec({ ok: true, planStatus: 'executing' });
  await execP1;
  await execP2;
  assert.equal(execDupCalls.length, 1, 'double-click must issue a single executePlan RPC');
  assert.equal(execSession.planStatus, 'executing');

  // 12. Switching session during a /plan await: the background session still
  //     merges metadata, but never pollutes the foreground DOM/messages.
  const bgEl = new FakeElement('div');
  t.setMessagesElement(bgEl);
  const bgSession = t.createLocalSession('local-bg', 'Background', 'bridge-bg');
  const fgSession = t.createLocalSession('local-fg', 'Foreground', 'bridge-fg');
  state.activeId = bgSession.id;
  let resolveBg;
  const bgGate = new Promise((res) => { resolveBg = res; });
  context.window.zeroAgent.startPlan = async (sid, task) => bgGate;
  const bgRun = t.handleSlash('/plan background task');
  state.activeId = fgSession.id; // switch before the RPC resolves
  const fgBefore = fgSession.messages.length;
  resolveBg({ ok: true, planPath: '/bg/plan.md', planTask: 'background task' });
  await bgRun;
  assert.equal(bgSession.planPath, '/bg/plan.md', 'background session still merges metadata');
  assert.equal(bgSession.planStatus, 'planning');
  assert.equal(fgSession.messages.length, fgBefore, 'switch must not pollute foreground messages');
  assert.equal(findByClass(bgEl, 'plan-status-card'), null, 'background plan must not render into DOM');

  // 13. /clear (empty messages) still renders the plan card when active.
  const clearEl = new FakeElement('div');
  t.setMessagesElement(clearEl);
  const clearSession = t.createLocalSession('local-clear', 'Clear', 'bridge-clear');
  state.activeId = clearSession.id;
  clearSession.planStatus = 'ready';
  clearSession.planPath = '/clear/plan.md';
  clearSession.planTask = 'keep me';
  clearSession.messages = [{ role: 'user', content: 'old' }];
  state._prevRenderedId = state.activeId;
  await t.handleSlash('/clear');
  const clearCard = findByClass(clearEl, 'plan-status-card');
  assert.ok(clearCard, '/clear must keep the plan card visible');
  assert.ok(!clearEl.classList.contains('empty'), 'empty state must not be shown with an active plan');

  // 14. Resume hydration merges plan metadata from result.session.
  const resumeEl = new FakeElement('div');
  t.setMessagesElement(resumeEl);
  const resumeSession = t.createLocalSession('local-resume', 'Resume', 'bridge-resume');
  state.activeId = resumeSession.id;
  context.window.zeroAgent.resumeSession = async (sid, index) => ({
    ok: true,
    message: 'restored',
    messages: [],
    session: { title: 'Restored title', planPath: '/resume/plan.md', planStatus: 'ready', planTask: 'resume task' },
  });
  state._prevRenderedId = state.activeId;
  await t.restoreResumeIndex(1, 'resume');
  assert.equal(resumeSession.title, 'Restored title');
  assert.equal(resumeSession.planPath, '/resume/plan.md');
  assert.equal(resumeSession.planStatus, 'ready');
  assert.equal(resumeSession.planTask, 'resume task');

  // 15. List hydration merges plan metadata from session/list snapshots.
  context.window.zeroAgent.rpc = async (method, params) => {
    if (method === 'session/poll') return { messages: [{ id: 1, role: 'user', content: 'hello' }] };
    return null;
  };
  state.sessions.clear();
  await t.hydrateBridgeSessions({
    sessions: [
      { id: 'bridge-hydrated', title: 'Hydrated', planPath: '/hyd/plan.md', planStatus: 'ready', planTask: 'hydrate me' },
    ],
  });
  const hydrated = [...state.sessions.values()].find((s) => s.bridgeSessionId === 'bridge-hydrated');
  assert.ok(hydrated, 'hydration must create a local session for the snapshot');
  assert.equal(hydrated.planPath, '/hyd/plan.md');
  assert.equal(hydrated.planStatus, 'ready');
  assert.equal(hydrated.planTask, 'hydrate me');


  console.log('frontend_plan_ui.test.js OK');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
