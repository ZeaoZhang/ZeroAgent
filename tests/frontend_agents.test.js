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
  insertAdjacentHTML(_position, html) { this.innerHTML += String(html); }
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
const messageStart = source.indexOf('function prepareMessagesForContent() {');
const panelStart = source.indexOf('// ─── Agent Panel');
const initMarker = source.indexOf('// ─── Init');
assert.ok(messageStart > 0 && marker > messageStart && panelStart > marker && initMarker > panelStart);
const context = makeAppContext();
vm.runInNewContext(source.slice(0, marker) + source.slice(panelStart, initMarker) + `
renderSessionList = () => null;
updateModelStatus = () => null;
setBusy = () => null;
pollSessionMessages = () => null;
sendPrompt = (prompt) => { globalThis.__selectedCandidate = prompt; };
showError = (message) => { globalThis.__error = message; };
messagesEl = document.getElementById('messages');
globalThis.__testExports = {
  state, createLocalSession, hydrateBridgeSessions, handleNotification,
  renderAgentPanel, setActiveSession, cancelAgent, getSessionRuntime,
  appendOutputFileLinks, extractOutputFileRefs, createLocalImagePreview, normalizeBridgeMessage,
  stripLocalOutputImageEmbeds, renderStructuredMarkdownInto,
  renderMessage,
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
  const sessionWorkspace = '/Users/zhangzeao/workspace/ZeroAgent/workspace';
  await t.hydrateBridgeSessions({ sessions: [{ id: 'bridge-a', title: 'A', cwd: sessionWorkspace, subAgents: [hydratedAgent] }] });
  const sessionA = [...t.state.sessions.values()][0];
  assert.equal(sessionA.cwd, sessionWorkspace);
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

  t.state.activeId = sessionA.id;
  context.window.zeroAgent.sessionImageUrl = (sessionId, filePath) => `${sessionId}::${filePath}`;
  context.window.zeroAgent.sessionFileUrl = (sessionId, filePath) =>
    `/session/${sessionId}/file?path=${encodeURIComponent(filePath)}`;
  assert.equal(
    t.createLocalImagePreview('/workspace/puppy.png').src,
    'bridge-a::/workspace/puppy.png',
  );
  const waitingMessage = t.normalizeBridgeMessage({
    id: 12, role: 'system', kind: 'input_required', content: 'Choose:', candidates: ['one', 'two'],
  });
  assert.equal(waitingMessage.kind, 'input_required');
  assert.equal(JSON.stringify(waitingMessage.candidates), JSON.stringify(['one', 'two']));
  assert.equal(
    JSON.stringify(t.extractOutputFileRefs('Done [FILE:reports/diagram.png]')),
    JSON.stringify(['reports/diagram.png']),
  );
  const generatedImagePath = '/Users/zhangzeao/workspace/ZeroAgent/workspace/plan_jev_diagram/jev_principle_diagram.png';
  sessionA.cwd = sessionWorkspace;
  assert.equal(
    JSON.stringify(t.extractOutputFileRefs('Saved to [FILE:plan_jev_diagram/jev_principle_diagram.png]', sessionWorkspace)),
    JSON.stringify([generatedImagePath]),
  );
  assert.equal(JSON.stringify(t.extractOutputFileRefs(`Saved to **\`${generatedImagePath}\`**`)), '[]');
  assert.equal(JSON.stringify(t.extractOutputFileRefs(`![JEV diagram](${generatedImagePath})`)), '[]');
  assert.equal(JSON.stringify(t.extractOutputFileRefs(`[FILE:${generatedImagePath}]`, sessionWorkspace)), '[]');
  const repeatedImageTrace = [
    '**LLM Running (Turn 1) ...**',
    'Old tool trace: `plan_jev_diagram/jev_principle_diagram.png`',
    '**LLM Running (Turn 2) ...**',
    'Final answer: [FILE:plan_jev_diagram/jev_principle_diagram.png]',
    'Also mentioned: [FILE:./plan_jev_diagram/jev_principle_diagram.png]',
  ].join('\n');
  assert.equal(
    JSON.stringify(t.extractOutputFileRefs(repeatedImageTrace, sessionWorkspace)),
    JSON.stringify([generatedImagePath]),
  );
  const localMarkdownImage = `![JEV diagram](${generatedImagePath})`;
  assert.equal(t.stripLocalOutputImageEmbeds(localMarkdownImage), '');
  assert.equal(
    t.stripLocalOutputImageEmbeds('![remote](https://example.com/diagram.png)'),
    '![remote](https://example.com/diagram.png)',
  );
  const markdownContainer = new FakeElement('div');
  t.renderStructuredMarkdownInto(markdownContainer, `Diagram [FILE:plan_jev_diagram/jev_principle_diagram.png]\n\n${localMarkdownImage}`);
  assert.doesNotMatch(markdownContainer.innerHTML, /<img[^>]+jev_principle_diagram\.png/);
  assert.equal(markdownContainer.children[0].className, 'assistant-output-files');
  assert.equal(markdownContainer.children[0].children[0].children[0].tagName, 'IMG');
  const traceContainer = new FakeElement('div');
  t.renderStructuredMarkdownInto(traceContainer, repeatedImageTrace);
  const outputLists = traceContainer.children.filter((child) => child.className === 'assistant-output-files');
  assert.equal(outputLists.length, 1);
  assert.equal(outputLists[0].children.length, 1);
  const fileContainer = new FakeElement('div');
  assert.equal(t.appendOutputFileLinks(fileContainer, [generatedImagePath, 'reports/report.pdf']), true);
  assert.equal(fileContainer.children[0].children[0].children[0].tagName, 'IMG');
  const links = fileContainer.children[0].children.map((item) => item.children.find((child) => child.tagName === 'A'));
  assert.equal(JSON.stringify(links.map((link) => link.href)), JSON.stringify([
    `/session/bridge-a/file?path=${encodeURIComponent(generatedImagePath)}`,
    '/session/bridge-a/file?path=reports%2Freport.pdf',
  ]));

  const waitingContainer = context.document.getElementById('messages');
  t.renderMessage({
    role: 'system', kind: 'input_required', content: 'Choose one:', candidates: ['first', 'second'],
  });
  const waitingCard = waitingContainer.children[waitingContainer.children.length - 1];
  assert.equal(waitingCard.className, 'msg msg-system msg-input-required');
  assert.equal(waitingCard.children[0].className, 'input-required-question md');
  assert.equal(waitingCard.children[1].className, 'input-required-options');
  assert.deepEqual(waitingCard.children[1].children.map((button) => button.textContent), ['first', 'second']);
  waitingCard.children[1].children[1].listeners.get('click')();
  assert.equal(context.__selectedCandidate, 'second');
}

async function adapterTests() {
  const calls = [];
  const windowObj = { location: { protocol: 'http:', hostname: '127.0.0.1', hash: '#token=secret', pathname: '/', search: '' }, history: { replaceState() {} } };
  class FakeWebSocket { static get CONNECTING() { return 0; } static get OPEN() { return 1; } constructor() { this.readyState = 0; } addEventListener() {} }
  const ctx = { window: windowObj, location: windowObj.location, document: {}, navigator: { platform: 'MacIntel' }, WebSocket: FakeWebSocket, URLSearchParams, console, fetch: async (url, init = {}) => { calls.push({ url, init }); return { ok: true, status: 200, statusText: 'OK', text: async () => '{}' }; } };
  ctx.globalThis = ctx;
  vm.runInNewContext(fs.readFileSync(adapterPath, 'utf8'), ctx, { filename: adapterPath });
  calls.length = 0;
  await windowObj.zeroAgent.rpc('session/agents', { sessionId: 'bridge/a' });
  assert.equal(calls[0].url, 'http://127.0.0.1:14168/session/bridge%2Fa/agents');
  assert.equal(calls[0].init.method, undefined);
  await windowObj.zeroAgent.rpc('session/agent/cancel', { sessionId: 'bridge/a', agentId: 'agent/1' });
  assert.equal(calls[1].url, 'http://127.0.0.1:14168/session/bridge%2Fa/agents/agent%2F1/cancel');
  assert.equal(calls[1].init.method, 'POST');
  assert.equal(
    windowObj.zeroAgent.sessionFileUrl('bridge/a', 'reports/a b.pdf'),
    'http://127.0.0.1:14168/session/bridge%2Fa/file?path=reports%2Fa+b.pdf&token=secret',
  );
}

Promise.all([appTests(), adapterTests()]).then(() => console.log('frontend_agents.test.js OK')).catch((error) => { console.error(error); process.exit(1); });
