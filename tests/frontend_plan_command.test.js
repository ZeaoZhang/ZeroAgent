'use strict';

// Adapter-level coverage for the plan RPC surface in za-web.js.
// No real LLM is involved: fetch is faked and the adapter's rpc/http path is
// exercised directly against the captured request shape.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const adapterPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'za-web.js');
const source = fs.readFileSync(adapterPath, 'utf8');

const fetchCalls = [];
function fakeFetch(url, init = {}) {
  fetchCalls.push({ url, init });
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    async text() { return '{}'; },
  };
}

class FakeWebSocket {
  static get CONNECTING() { return 0; }
  static get OPEN() { return 1; }
  constructor(url) { this.url = url; this.readyState = FakeWebSocket.CONNECTING; }
  addEventListener() {}
}

const windowObj = {
  location: {
    protocol: 'http:',
    hostname: '127.0.0.1',
    hash: '',
    pathname: '/',
    search: '',
  },
  history: { replaceState() {} },
};

const context = {
  window: windowObj,
  location: windowObj.location,
  document: { title: 'test' },
  navigator: { platform: 'MacIntel' },
  WebSocket: FakeWebSocket,
  fetch: fakeFetch,
  console,
};
context.globalThis = context;

vm.runInNewContext(source, context, { filename: adapterPath });

const zeroAgent = windowObj.zeroAgent;
assert.ok(zeroAgent, 'adapter must expose window.zeroAgent');
assert.equal(typeof zeroAgent.startPlan, 'function');
assert.equal(typeof zeroAgent.executePlan, 'function');

// Discard the adapter's own startup /status request.
fetchCalls.length = 0;

function lastCall() {
  return fetchCalls[fetchCalls.length - 1];
}

async function main() {
  await zeroAgent.rpc('session/plan', { sessionId: 'sess-123456789abc', task: 'build it' });
  const plan = lastCall();
  assert.equal(plan.url, 'http://127.0.0.1:14168/session/sess-123456789abc/plan');
  assert.equal(plan.init.method, 'POST');
  assert.deepEqual(JSON.parse(plan.init.body), { task: 'build it' });

  await zeroAgent.rpc('session/plan/execute', { sessionId: 'sess-123456789abc' });
  const execute = lastCall();
  assert.equal(execute.url, 'http://127.0.0.1:14168/session/sess-123456789abc/plan/execute');
  assert.equal(execute.init.method, 'POST');
  assert.deepEqual(JSON.parse(execute.init.body), {});

  await zeroAgent.startPlan('sess-abcdef123456', 'ship the plan');
  const wrapperPlan = lastCall();
  assert.equal(wrapperPlan.url, 'http://127.0.0.1:14168/session/sess-abcdef123456/plan');
  assert.deepEqual(JSON.parse(wrapperPlan.init.body), { task: 'ship the plan' });

  await zeroAgent.executePlan('sess-abcdef123456');
  const wrapperExecute = lastCall();
  assert.equal(wrapperExecute.url, 'http://127.0.0.1:14168/session/sess-abcdef123456/plan/execute');
  assert.deepEqual(JSON.parse(wrapperExecute.init.body), {});

  await assert.rejects(
    () => zeroAgent.rpc('session/plan', { task: 'no session' }),
    /session\/plan missing sessionId/,
  );
  await assert.rejects(
    () => zeroAgent.rpc('session/plan/execute', {}),
    /session\/plan\/execute missing sessionId/,
  );
  await assert.rejects(
    () => zeroAgent.startPlan(undefined, 'no session'),
    /session\/plan missing sessionId/,
  );
  await assert.rejects(
    () => zeroAgent.executePlan(undefined),
    /session\/plan\/execute missing sessionId/,
  );
}

main().then(
  () => console.log('frontend_plan_command.test.js OK'),
  (error) => {
    console.error(error);
    process.exit(1);
  },
);
