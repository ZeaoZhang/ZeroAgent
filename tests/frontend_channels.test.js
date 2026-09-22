'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const staticDir = path.join(root, 'zero_agent', 'frontends', 'desktop', 'static');
const appPath = path.join(staticDir, 'app.js');
const adapterPath = path.join(staticDir, 'za-web.js');

async function adapterTests() {
  const calls = [];
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
  class FakeWebSocket {
    static get CONNECTING() { return 0; }
    static get OPEN() { return 1; }
    constructor() { this.readyState = 0; }
    addEventListener() {}
  }
  const context = {
    window: windowObj,
    location: windowObj.location,
    document: {},
    navigator: { platform: 'MacIntel' },
    WebSocket: FakeWebSocket,
    console,
    fetch: async (url, init = {}) => {
      calls.push({ url, init });
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        text: async () => JSON.stringify({ ok: true, channels: [] }),
      };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync(adapterPath, 'utf8'), context, { filename: adapterPath });
  calls.length = 0;

  await windowObj.zeroAgent.rpc('channels/list', {});
  assert.equal(calls[0].url, 'http://127.0.0.1:14168/channels');
  assert.equal(calls[0].init.method, undefined);

  await windowObj.zeroAgent.rpc('channels/start', { channelId: 'telegram' });
  assert.equal(calls[1].url, 'http://127.0.0.1:14168/channels/telegram/start');
  assert.equal(calls[1].init.method, 'POST');

  await windowObj.zeroAgent.rpc('channels/link', { channelId: 'telegram', linked: false });
  assert.equal(calls[2].url, 'http://127.0.0.1:14168/channels/telegram/link');
  assert.equal(calls[2].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[2].init.body), { linked: false });

  await windowObj.zeroAgent.rpc('channels/config', { channelId: 'telegram' });
  assert.equal(calls[3].url, 'http://127.0.0.1:14168/channels/telegram/config');
  assert.equal(calls[3].init.method, undefined);

  await windowObj.zeroAgent.rpc('channels/config/save', {
    channelId: 'telegram',
    values: { tg_bot_token: 'new-token', tg_allowed_users: ['1001'] },
  });
  assert.equal(calls[4].url, 'http://127.0.0.1:14168/channels/telegram/config');
  assert.equal(calls[4].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[4].init.body), {
    values: { tg_bot_token: 'new-token', tg_allowed_users: ['1001'] },
  });

  await windowObj.zeroAgent.rpc('channels/wechat/login/start', {});
  assert.equal(calls[5].url, 'http://127.0.0.1:14168/channels/wechat/login');
  assert.equal(calls[5].init.method, 'POST');
  await windowObj.zeroAgent.rpc('channels/wechat/login/status', { sessionId: 'session-1' });
  assert.equal(calls[6].url, 'http://127.0.0.1:14168/channels/wechat/login/session-1');
  assert.equal(calls[6].init.method, undefined);
  await windowObj.zeroAgent.rpc('channels/wechat/login/cancel', { sessionId: 'session-1' });
  assert.equal(calls[7].url, 'http://127.0.0.1:14168/channels/wechat/login/session-1/cancel');
  assert.equal(calls[7].init.method, 'POST');
  await assert.rejects(
    windowObj.zeroAgent.rpc('channels/wechat/login/status', {}),
    /missing sessionId/,
  );

}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : !!force;
    if (next) this.add(name); else this.remove(name);
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
    this.disabled = false;
    this.value = '';
    this.type = '';
    this.name = '';
    this.placeholder = '';
    this.src = '';
  }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) this.children.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  remove() { this.parentNode?.removeChild(this); }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  _matches(selector) {
    if (selector === '*') return true;
    if (selector === 'input') return this.tagName === 'INPUT';
    if (selector === 'textarea') return this.tagName === 'TEXTAREA';
    if (selector === 'img') return this.tagName === 'IMG';
    if (selector === '.modal-backdrop') return this.classList.contains('modal-backdrop');
    if (selector === '[data-channel-field]') return this.dataset.channelField !== undefined;
    if (selector === '[data-channel-config-field]') {
      return this.dataset.channelConfigField !== undefined;
    }
    return false;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map((item) => item.trim());
    const matches = [];
    const visit = (node) => {
      if (selectors.some((item) => node._matches(item))) matches.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {}
}

function makeAppContext() {
  const elements = new Map();
  const document = {
    body: new FakeElement('body'),
    documentElement: new FakeElement('html'),
    createElement: (tag) => new FakeElement(tag),
    createDocumentFragment: () => new FakeElement('fragment'),
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll: () => [],
  };
  for (const id of [
    'channel-list',
    'channel-settings-modal',
    'channel-settings-btn',
    'channel-config-modal',
    'channel-config-title',
    'channel-config-form',
    'wechat-qr-panel',
    'wechat-qr-image',
    'wechat-qr-status',
    'channel-config-error',
  ]) {
    elements.set(id, new FakeElement('div'));
  }
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
    Promise,
    setTimeout,
    clearTimeout,
    performance: { now: () => 1000 },
    navigator: { platform: 'test' },
    localStorage: { getItem: () => null, setItem: () => {} },
    document,
    window: {},
  };
  context.globalThis = context;
  return { context, elements };
}

async function rendererTests() {
  const { context, elements } = makeAppContext();
  const source = fs.readFileSync(appPath, 'utf8');
  const bridgeMarker = source.indexOf('// ─── Bridge events');
  assert.ok(bridgeMarker > 0);
  vm.runInNewContext(source.slice(0, bridgeMarker) + `
channelListEl = document.getElementById('channel-list');
channelSettingsModal = document.getElementById('channel-settings-modal');
channelConfigModal = document.getElementById('channel-config-modal');
channelConfigTitle = document.getElementById('channel-config-title');
channelConfigForm = document.getElementById('channel-config-form');
wechatQrPanel = document.getElementById('wechat-qr-panel');
wechatQrImage = document.getElementById('wechat-qr-image');
wechatQrStatus = document.getElementById('wechat-qr-status');
channelConfigError = document.getElementById('channel-config-error');
globalThis.__testExports = {
  state,
  renderChannelList,
  handleChannelToggle,
  channelErrorMessage,
  openChannelConfig,
  renderChannelConfig,
  saveChannelConfig,
  closeChannelConfig,
  startWechatLogin,
  pollWechatLogin,
};
`, context, { filename: appPath });

  const t = context.__testExports;
  const channelList = elements.get('channel-list');
  const rows = [
    {
      id: 'telegram',
      label: 'Telegram',
      module: 'bots/telegram_app.py',
      configured: false,
      requiredKeys: ['tg_bot_token'],
      running: false,
      pid: null,
      linked: true,
    },
    {
      id: 'discord',
      label: 'Discord',
      module: 'bots/discord_app.py',
      configured: true,
      requiredKeys: ['discord_bot_token'],
      running: true,
      pid: 321,
      linked: true,
    },
    {
      id: 'wechat',
      label: '微信',
      module: 'bots/wechat_app.py',
      configured: false,
      requiredKeys: [],
      running: false,
      pid: null,
      linked: true,
    },
  ];
  t.state.channelStatuses = rows;
  t.renderChannelList();
  assert.match(channelList.innerHTML, /Telegram/);
  assert.match(channelList.innerHTML, /Discord/);
  assert.match(channelList.innerHTML, /未配置/);
  assert.doesNotMatch(channelList.innerHTML, /运行中|已停止/);
  assert.doesNotMatch(channelList.innerHTML, /tg_bot_token/);
  assert.doesNotMatch(channelList.innerHTML, /test-secret-value/);
  assert.doesNotMatch(channelList.innerHTML, /连接 App/);
  assert.doesNotMatch(channelList.innerHTML, /扫码登录/);
  assert.doesNotMatch(channelList.innerHTML, /连接状态/);
  assert.match(channelList.innerHTML, /已连接/);
  assert.doesNotMatch(channelList.innerHTML, /未连接|已断开/);
  assert.match(
    channelList.innerHTML,
    /data-channel-id="wechat"[^>]+data-channel-action="running"[^>]+disabled>/,
  );
  rows[2].configured = true;
  rows[2].linked = true;
  t.renderChannelList();
  assert.match(channelList.innerHTML, /已配置/);
  rows[2].running = true;
  t.renderChannelList();
  assert.match(channelList.innerHTML, /已连接/);
  rows[2].linked = false;
  t.renderChannelList();
  assert.match(channelList.innerHTML, /已配置/);
  assert.doesNotMatch(channelList.innerHTML, /已断开/);
  assert.equal(
    t.channelErrorMessage({ status: 409, message: 'Telegram 未配置: tg_bot_token' }, '启动渠道'),
    '启动渠道失败：Telegram 未配置: tg_bot_token',
  );

  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      if (method === 'channels/config') {
        return {
          ok: true,
          config: {
            channelId: 'telegram',
            source: 'config.yaml',
            configured: true,
            fields: [
              {
                key: 'tg_bot_token',
                label: 'Bot Token',
                kind: 'secret',
                required: true,
                configured: true,
                source: 'file',
                editable: true,
              },
              {
                key: 'tg_allowed_users',
                label: 'Allowed Users',
                kind: 'list',
                required: true,
                configured: true,
                source: 'file',
                editable: true,
              },
            ],
          },
        };
      }
      if (method === 'channels/config/save') {
        return {
          ok: true,
          config: { channelId: 'telegram', fields: [] },
          channels: rows,
          requiresRestart: false,
        };
      }
      if (method === 'channels/list') return { ok: true, channels: rows };
      return {
        ok: true,
        channels: rows.map((row) => row.id === 'telegram'
          ? { ...row, linked: false, running: true, pid: 456 }
          : row),
      };
    },
  };
  rows[0].configured = true;
  await t.handleChannelToggle('telegram', 'running', true);
  assert.equal(calls[0].method, 'channels/start');
  assert.equal(calls[0].params.channelId, 'telegram');
  assert.equal(t.state.channelStatuses[0].running, true);

  calls.length = 0;
  await t.handleChannelToggle('telegram', 'linked', false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'channels/link');
  assert.equal(calls[0].params.channelId, 'telegram');
  assert.equal(calls[0].params.linked, false);
  assert.equal(t.state.channelStatuses[0].linked, false);

  calls.length = 0;
  await t.openChannelConfig('telegram');
  assert.equal(calls[0].method, 'channels/config');
  assert.equal(calls[0].params.channelId, 'telegram');
  const configError = elements.get('channel-config-error');
  assert.equal(configError.textContent, '');
  assert.equal(
    configError.classList.contains('hidden'),
    true,
    'an empty channel-config error must not render a red alert bar',
  );
  const configForm = elements.get('channel-config-form');
  const configModal = elements.get('channel-config-modal');
  const fields = configForm.querySelectorAll('input, textarea');
  const tokenInput = fields.find((input) => input.dataset.channelField === 'tg_bot_token');
  const usersInput = fields.find((input) => input.dataset.channelField === 'tg_allowed_users');
  assert.ok(tokenInput);
  assert.ok(usersInput);
  assert.equal(tokenInput.value, '');
  assert.equal(tokenInput.placeholder, '已配置；留空保持不变');
  assert.doesNotMatch(JSON.stringify(fields.map((input) => input.value)), /configured-secret/);

  tokenInput.value = 'new-token';
  usersInput.value = '1001';
  await t.saveChannelConfig();
  const saveCall = calls.find((call) => call.method === 'channels/config/save');
  assert.deepEqual(JSON.parse(JSON.stringify(saveCall.params)), {
    channelId: 'telegram',
    values: { tg_bot_token: 'new-token', tg_allowed_users: ['1001'] },
  });
  assert.equal(configModal.classList.contains('hidden'), true);

  let statusPolls = 0;
  let pollPromise;
  context.setTimeout = (callback) => {
    pollPromise = callback();
    return 1;
  };
  context.window.zeroAgent.rpc = async (method, params) => {
    calls.push({ method, params });
    if (method === 'channels/wechat/login/start') {
      return {
        ok: true,
        sessionId: 'wx-1',
        status: 'pending',
        qrDataUrl: 'data:image/png;base64,qr',
      };
    }
    if (method === 'channels/wechat/login/status') {
      statusPolls += 1;
      return { ok: true, sessionId: 'wx-1', status: 'confirmed' };
    }
    if (method === 'channels/list') return { ok: true, channels: rows };
    throw new Error(`unexpected ${method}`);
  };
  t.state.channelConfig = {
    channelId: 'wechat',
    snapshot: { channelId: 'wechat', fields: [] },
  };
  configModal.classList.remove('hidden');
  await t.startWechatLogin();
  await pollPromise;
  assert.match(elements.get('wechat-qr-image').src, /^data:image\/png;base64,/);
  assert.equal(statusPolls, 1);
  assert.equal(t.state.wechatLogin.timer, null);
  assert.equal(elements.get('wechat-qr-status').textContent, 'confirmed');

  t.closeChannelConfig();
  assert.equal(elements.get('wechat-qr-panel').classList.contains('hidden'), true);
  assert.equal(elements.get('wechat-qr-image').src, '');
  assert.equal(elements.get('wechat-qr-status').textContent, '点击扫码登录生成二维码。');

  t.state.channelConfig = {
    channelId: 'telegram',
    snapshot: { channelId: 'telegram', fields: [] },
  };
  configModal.classList.remove('hidden');
  tokenInput.value = 'unsaved-token';
  context.window.zeroAgent.rpc = async (method) => {
    calls.push({ method });
    if (method === 'channels/config/save') {
      const error = new Error('invalid field');
      error.status = 400;
      throw error;
    }
    if (method === 'channels/list') return { ok: true, channels: rows };
    return { ok: true, channels: rows };
  };
  await t.saveChannelConfig();
  assert.equal(configModal.classList.contains('hidden'), false);
  assert.equal(tokenInput.value, 'unsaved-token');
  assert.ok(elements.get('channel-config-error').textContent);
  assert.equal(elements.get('channel-config-error').classList.contains('hidden'), false);
}

Promise.all([adapterTests(), rendererTests()])
  .then(() => console.log('frontend_channels.test.js OK'))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
