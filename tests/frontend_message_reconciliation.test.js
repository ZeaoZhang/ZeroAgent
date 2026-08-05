'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const slashCommandsMarker = source.indexOf('// ─── Slash commands');
assert.ok(slashCommandsMarker > 0, 'message handling section must exist');

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
  performance: { now: () => 1000 },
  navigator: { platform: 'test' },
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    querySelectorAll: () => [],
    getElementById: () => null,
  },
  window: {},
};
context.globalThis = context;

const testExports = `
renderAssistantDraftInPlace = () => null;
renderMessage = () => null;
renderMessages = () => null;
isActiveSession = () => false;
hideError = () => {};
startTaskTimer = () => {};
stopTaskTimer = () => {};
async function ensureBridgeSession(sess) { return sess.bridgeSessionId || sess.id; }
setBusy = (busy, label, sess) => {
  if (sess) getSessionRuntime(sess).busy = busy;
};
globalThis.__testExports = {
  state,
  getSessionRuntime,
  upsertPolledMessage,
  finalizeAssistantReply,
  handleNotification,
  appendTurn,
  beginAssistantTurn,
  sendPrompt,
  setMessagesElement: (value) => { messagesEl = value; },
  setStatusElements: (model, usage) => {
    currentModelEl = model;
    tokenUsageEl = usage;
  },
};
`;
vm.runInNewContext(source.slice(0, slashCommandsMarker) + testExports, context, { filename: appPath });

const { state, getSessionRuntime, upsertPolledMessage, finalizeAssistantReply, handleNotification, appendTurn, beginAssistantTurn, sendPrompt } = context.__testExports;
const messageSummary = (session) => session.messages.map(({ id = null, content }) => ({ id, content }));
const assistantSummary = (session) => session.messages
  .filter((message) => message.role === 'assistant')
  .map(({ id = null, content }) => ({ id, content }));
const messages = {
  lastElementChild: null,
  querySelectorAll: () => [],
};
context.__testExports.setMessagesElement(messages);

const session = { id: 'local-test', bridgeSessionId: 'bridge-test', messages: [] };
state.sessions.clear();
state.sessions.set(session.id, session);
state.activeId = session.id;
state.bridgeReady = true;

// Normal polling order: the formal message arrives while its draft is active.
const runtime = getSessionRuntime(session);
runtime.busy = true;
upsertPolledMessage(session, { id: 2, role: 'assistant', content: 'streaming answer' }, { partial: true });
assert.equal(runtime.assistantDraft.bridgeMessageId, 2);
upsertPolledMessage(session, { id: 2, role: 'assistant', content: 'canonical final answer' });
assert.deepEqual(messageSummary(session), [{ id: 2, content: 'canonical final answer' }]);

// Formal message can arrive before streaming completion. It is queued and
// consumed when the notification path finalizes the active draft.
const earlySession = { id: 'local-early-test', bridgeSessionId: 'bridge-early', messages: [] };
state.sessions.set(earlySession.id, earlySession);
state.activeId = earlySession.id;
const earlyRuntime = getSessionRuntime(earlySession);
earlyRuntime.busy = true;
upsertPolledMessage(earlySession, { id: 3, role: 'assistant', content: 'canonical early answer' });
assert.deepEqual(messageSummary(earlySession), []);
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-early', update: {
    sessionUpdate: 'agent_message_chunk',
    content: { type: 'text', text: 'streaming early answer' },
  } },
});
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-early', update: { sessionUpdate: 'task_completed' } },
});
assert.deepEqual(messageSummary(earlySession), [{ id: 3, content: 'canonical early answer' }]);

// Regression: streaming completion may happen before polling returns the
// formal message. The late message must adopt the existing rendered reply.
const lateSession = { id: 'local-late-test', bridgeSessionId: 'bridge-late', messages: [] };
state.sessions.set(lateSession.id, lateSession);
state.activeId = lateSession.id;
const lateRuntime = getSessionRuntime(lateSession);
lateRuntime.busy = true;
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-late', update: {
    sessionUpdate: 'agent_message_chunk',
    content: { type: 'text', text: 'streaming late answer' },
  } },
});
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-late', update: { sessionUpdate: 'task_completed' } },
});
assert.deepEqual(messageSummary(lateSession), [{ id: null, content: 'streaming late answer' }]);

upsertPolledMessage(lateSession, { id: 4, role: 'assistant', content: 'canonical late answer' });
assert.deepEqual(
  messageSummary(lateSession),
  [{ id: 4, content: 'canonical late answer' }],
  'the late formal message must replace the streaming draft without duplication',
);

upsertPolledMessage(lateSession, { id: 5, role: 'assistant', content: 'second answer' });
assert.deepEqual(messageSummary(lateSession), [
  { id: 4, content: 'canonical late answer' },
  { id: 5, content: 'second answer' },
]);

// A late formal message from an earlier turn must not bind to a newer draft.
const crossSession = { id: 'local-cross-turn-test', bridgeSessionId: 'bridge-cross', messages: [] };
state.sessions.set(crossSession.id, crossSession);
state.activeId = crossSession.id;
const crossRuntime = getSessionRuntime(crossSession);
beginAssistantTurn(crossRuntime);
crossRuntime.busy = true;
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-cross', update: {
    sessionUpdate: 'agent_message_chunk',
    content: { type: 'text', text: 'first streaming answer' },
  } },
});
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-cross', update: { sessionUpdate: 'task_completed' } },
});
beginAssistantTurn(crossRuntime);
crossRuntime.activePromptUserId = 3;
crossRuntime.pendingAssistantMessages[0].nextUserMessageId = 3;
crossRuntime.busy = true;
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-cross', update: {
    sessionUpdate: 'agent_message_chunk',
    content: { type: 'text', text: 'second streaming answer' },
  } },
});
// The old formal id arrives while the second turn is still busy.
upsertPolledMessage(crossSession, { id: 2, role: 'assistant', content: 'first canonical answer' });
upsertPolledMessage(crossSession, { id: 4, role: 'assistant', content: 'second canonical answer' });
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-cross', update: { sessionUpdate: 'task_completed' } },
});
assert.deepEqual(messageSummary(crossSession), [
  { id: 2, content: 'first canonical answer' },
  { id: 4, content: 'second canonical answer' },
]);

// A formal id different from an already-bound partial draft must not replace
// that draft or become a duplicate during finalization.
const mismatchSession = { id: 'local-mismatch-test', bridgeSessionId: 'bridge-mismatch', messages: [] };
state.sessions.set(mismatchSession.id, mismatchSession);
state.activeId = mismatchSession.id;
const mismatchRuntime = getSessionRuntime(mismatchSession);
mismatchRuntime.busy = true;
upsertPolledMessage(mismatchSession, { id: 6, role: 'assistant', content: 'streaming mismatch answer' }, { partial: true });
upsertPolledMessage(mismatchSession, { id: 7, role: 'assistant', content: 'unrelated formal answer' });
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-mismatch', update: { sessionUpdate: 'task_completed' } },
});
assert.deepEqual(messageSummary(mismatchSession), [
  { id: 6, content: 'streaming mismatch answer' },
]);
assert.equal(mismatchRuntime.pendingFormalAssistantMessages.length, 1);

// Multiple formal messages queued in one busy turn remain distinct, and
// streamed tool/thought segments survive canonical content reconciliation.
const queuedSession = { id: 'local-queued-test', bridgeSessionId: 'bridge-queued', messages: [] };
state.sessions.set(queuedSession.id, queuedSession);
state.activeId = queuedSession.id;
const queuedRuntime = getSessionRuntime(queuedSession);
queuedRuntime.busy = true;
appendTurn(queuedSession, 'agent_thought_chunk', 'thinking detail', true);
upsertPolledMessage(queuedSession, { id: 6, role: 'assistant', content: 'canonical queued answer' });
upsertPolledMessage(queuedSession, { id: 7, role: 'assistant', content: 'second queued answer' });
handleNotification({
  method: 'session/update',
  params: { sessionId: 'bridge-queued', update: { sessionUpdate: 'task_completed' } },
});
assert.deepEqual(messageSummary(queuedSession), [
  { id: 6, content: 'canonical queued answer' },
  { id: 7, content: 'second queued answer' },
]);
assert.equal(queuedSession.messages[0].segments[0].text, 'thinking detail');

// Queued formal updates compare bridge ids numerically, matching persisted
// string ids against normalized numeric poll ids without duplicating.
const queuedStringIdSession = { id: 'local-queued-string-id-test', bridgeSessionId: 'bridge-queued-string-id', messages: [] };
state.sessions.set(queuedStringIdSession.id, queuedStringIdSession);
state.activeId = queuedStringIdSession.id;
const queuedStringRuntime = getSessionRuntime(queuedStringIdSession);
queuedStringRuntime.busy = true;
queuedStringRuntime.activeTurnToken = 1;
queuedStringRuntime.pendingFormalAssistantMessages.push({
  message: { id: '8', role: 'assistant', content: 'queued stale answer' },
  turnToken: 1,
});
upsertPolledMessage(queuedStringIdSession, { id: 8, role: 'assistant', content: 'queued updated answer' });
assert.equal(queuedStringRuntime.pendingFormalAssistantMessages.length, 1);
assert.equal(queuedStringRuntime.pendingFormalAssistantMessages[0].message.content, 'queued updated answer');

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

(async () => {
  const promptCalls = [];
  context.window.zeroAgent = {
    rpc(method) {
      assert.equal(method, 'session/prompt');
      const call = deferred();
      promptCalls.push(call);
      return call.promise;
    },
    pollSession: () => new Promise(() => {}),
  };

  const promptRaceSession = {
    id: 'local-prompt-race-test',
    bridgeSessionId: 'bridge-prompt-race',
    title: 'Prompt race',
    untitled: false,
    config: { llmNo: 0 },
    messages: [],
  };
  state.sessions.set(promptRaceSession.id, promptRaceSession);
  state.activeId = promptRaceSession.id;
  const promptRaceRuntime = getSessionRuntime(promptRaceSession);

  const firstPrompt = sendPrompt('first prompt');
  await Promise.resolve();
  assert.equal(promptCalls.length, 1);
  handleNotification({
    method: 'session/update',
    params: { sessionId: 'bridge-prompt-race', update: {
      sessionUpdate: 'agent_message_chunk',
      content: { type: 'text', text: 'first streaming answer' },
    } },
  });
  handleNotification({
    method: 'session/update',
    params: { sessionId: 'bridge-prompt-race', update: { sessionUpdate: 'task_completed' } },
  });

  const secondPrompt = sendPrompt('second prompt');
  await Promise.resolve();
  assert.equal(promptCalls.length, 2);
  handleNotification({
    method: 'session/update',
    params: { sessionId: 'bridge-prompt-race', update: {
      sessionUpdate: 'agent_message_chunk',
      content: { type: 'text', text: 'second streaming answer' },
    } },
  });
  // The second streamed draft can finalize before prompt RPC responses return.
  finalizeAssistantReply(promptRaceSession);
  // The old formal id is observed while the new turn remains busy.
  upsertPolledMessage(promptRaceSession, { id: 2, role: 'assistant', content: 'first canonical answer' });
  promptCalls[1].resolve({ userMessageId: 3 });
  await secondPrompt;
  promptCalls[0].resolve({ userMessageId: 1 });
  await firstPrompt;
  assert.equal(promptRaceRuntime.activePromptUserId, 3);
  assert.equal(promptRaceRuntime.pendingAssistantMessages.length, 1);
  assert.equal(promptRaceRuntime.pendingAssistantMessages[0].userMessageId, 3);
  upsertPolledMessage(promptRaceSession, { id: 4, role: 'assistant', content: 'second canonical answer' });
  handleNotification({
    method: 'session/update',
    params: { sessionId: 'bridge-prompt-race', update: { sessionUpdate: 'task_completed' } },
  });
  assert.deepEqual(assistantSummary(promptRaceSession), [
    { id: 2, content: 'first canonical answer' },
    { id: 4, content: 'second canonical answer' },
  ]);

  console.log('frontend message reconciliation regression passed');
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});

assert.match(source, /Cache: \$\{cacheStatus\}/);
assert.match(source, /usage\.cacheMetricsAvailable/);
assert.match(source, /: 'n\/a'/);
assert.match(source, /sess\.modelOverride = replacement\.modelOverride \?\? null;/)
