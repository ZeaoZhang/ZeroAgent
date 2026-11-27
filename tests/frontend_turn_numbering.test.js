'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.resolve(__dirname, '..', 'zero_agent', 'frontends', 'desktop', 'static', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const slashCommandsMarker = source.indexOf('// ─── Slash commands');
assert.ok(slashCommandsMarker > 0, 'turn rendering section must exist');

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

vm.runInNewContext(source.slice(0, slashCommandsMarker) + `
globalThis.__testExports = { groupIntoTurns, explicitTurnIndex, assistantTurnStartIndex };
`, context, { filename: appPath });

const { groupIntoTurns, explicitTurnIndex, assistantTurnStartIndex } = context.__testExports;

const llmTurn = (n) => ({ kind: 'LLM_RUNNING', text: `**LLM Running (Turn ${n}) ...**\n\nbody ${n}` });

assert.equal(explicitTurnIndex('**LLM Running (Turn 62) ...**'), 62);
assert.equal(explicitTurnIndex('TURN 41: 探测结束，开始执行。'), 41);
assert.equal(explicitTurnIndex('TURN 61: TOOL: `code_run` ARGS:'), 61);
assert.equal(explicitTurnIndex('plain answer'), 0);

// A hot restart resumes the round at Turn 62 while the new bubble's own
// transcript still starts at Turn 1: numbering must continue, not reset.
assert.deepEqual(
  groupIntoTurns([llmTurn(1), llmTurn(2)], { startIndex: 61 }).map((turn) => turn.index),
  [62, 63],
);
assert.deepEqual(
  groupIntoTurns([llmTurn(1), llmTurn(2)]).map((turn) => turn.index),
  [1, 2],
);

// Runner-authored numbers jump ahead of the local counter when the transcript
// was restored (CLI history numbers every round from the real turn count).
assert.deepEqual(
  groupIntoTurns([llmTurn(41), llmTurn(42)]).map((turn) => turn.index),
  [41, 42],
);
// ...but a stale/lower number never rewinds the counter.
assert.deepEqual(
  groupIntoTurns([llmTurn(1), llmTurn(2)], { startIndex: 61 }).map((turn) => turn.index),
  [62, 63],
);

// Plain segments render outside the fold counter.
const mixed = groupIntoTurns([
  { kind: 'agent_message_chunk', text: 'answer' },
  llmTurn(1),
  { kind: 'agent_message_chunk', text: 'more' },
]);
assert.deepEqual(mixed.map((item) => item.type), ['plain', 'turn', 'plain']);
assert.equal(mixed[1].index, 1);

const userMessage = { role: 'user', content: 'go' };
const firstAnswer = { role: 'assistant', content: 'a', _turnIndex: 12 };
const restartNote = { role: 'system', content: '内核已热重启，任务从检查点继续。' };
const resumed = { role: 'assistant', content: 'b' };
const sess = { messages: [userMessage, firstAnswer, restartNote, resumed] };

assert.equal(assistantTurnStartIndex(sess), 12);
assert.equal(assistantTurnStartIndex(sess, resumed), 12);
assert.equal(assistantTurnStartIndex({ messages: [userMessage, resumed] }, resumed), 0);

console.log('frontend turn numbering regression passed');
