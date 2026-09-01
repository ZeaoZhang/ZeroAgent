# Channel Control Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real desktop channel-control modal that reports and controls seven bot processes and can enable or disable their message link to ZeroAgent.

**Architecture:** Keep channel metadata and link-state persistence in `zero_agent/bots/channel_control.py`, expose process lifecycle and authenticated HTTP handlers through the existing desktop bridge, and render the controls in the existing static Web2/Tauri frontend. Bot entrypoints consult the shared link-state file at message boundaries, while process status is always read from the OS rather than cached in the browser.

**Tech Stack:** Python 3.10+, aiohttp, psutil, JSON atomic replacement, vanilla JavaScript, HTML, CSS, Node VM frontend regression tests, pytest.

---

## File map

- Create `zero_agent/bots/channel_control.py`: fixed channel definitions, required-configuration checks, link-state JSON read/write, safe defaults.
- Modify `zero_agent/bots/common.py`: shared `channel_is_linked()` gate and mixin enforcement.
- Modify `zero_agent/frontends/desktop_commands.py`: process discovery/start/stop/status for channel entrypoints.
- Modify `zero_agent/frontends/desktop_bridge.py`: authenticated `/channels` handlers and routes.
- Modify `zero_agent/frontends/desktop/static/za-web.js`: RPC mapping for channel endpoints.
- Modify `zero_agent/frontends/desktop/static/index.html`: topbar button and channel modal markup.
- Modify `zero_agent/frontends/desktop/static/app.js`: channel state, rendering, lifecycle/link actions, modal events.
- Modify `zero_agent/frontends/desktop/static/styles.css`: channel cards, status pills, accessible switches, responsive modal sizing.
- Modify `zero_agent/bots/wechat_app.py`, `wecom_app.py`, `dingtalk_app.py`, `qq_app.py`, `feishu_app.py`, `telegram_app.py`, `discord_app.py`: gate non-mixin inbound paths before handling or downloading messages.
- Create `tests/test_channel_control.py`: deterministic Python behavior tests.
- Create `tests/frontend_channels.test.js`: deterministic frontend and adapter regression tests.
- Modify `tests/test_desktop_bridge.py`: authenticated endpoint smoke-level contract assertions.

### Task 1: Add channel definitions and safe link-state storage

**Files:**
- Create: `zero_agent/bots/channel_control.py`
- Test: `tests/test_channel_control.py`

- [ ] **Step 1: Write failing tests for definitions and defaults**

```python
def test_channel_definitions_are_stable():
    assert [item.id for item in channel_definitions()] == [
        "wechat", "wecom", "dingtalk", "qq", "feishu", "telegram", "discord",
    ]
    assert channel_definition("telegram").required_keys == ("tg_bot_token",)


def test_link_state_defaults_to_true_and_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", str(tmp_path / "channels.json"))
    assert is_channel_linked("telegram") is True
    set_channel_linked("telegram", False)
    assert is_channel_linked("telegram") is False
    assert get_channel_settings()["telegram"] == {"linked": False}


def test_corrupt_link_state_fails_open(monkeypatch, tmp_path):
    path = tmp_path / "channels.json"
    path.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("ZA_CHANNEL_SETTINGS_PATH", str(path))
    assert is_channel_linked("telegram") is True
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest tests/test_channel_control.py -q`
Expected: FAIL because the module and exported functions do not exist.

- [ ] **Step 3: Implement the channel-control module**

Define a frozen dataclass:

```python
@dataclass(frozen=True)
class ChannelDefinition:
    id: str
    label: str
    source: str
    module: str
    required_keys: tuple[str, ...]
```

Expose `channel_definitions()`, `channel_definition(channel_id)`, `get_channel_settings()`, `set_channel_linked(channel_id, linked)`, `is_channel_linked(channel_id)`, and `channel_is_configured(definition, keys=None)`. Use `ZA_CHANNEL_SETTINGS_PATH` when set, otherwise `<project-root>/temp/channel_settings.json`; write a temporary sibling file and `os.replace`. Unknown IDs raise `KeyError`. A missing, malformed, or non-object JSON document yields `{}` and therefore `linked=True`. For WeChat, configuration is true only when `~/.wxbot/token.json` contains a non-empty `bot_token`; other channels require all listed keys from `load_keys()`.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest tests/test_channel_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the storage unit**

```bash
git add zero_agent/bots/channel_control.py tests/test_channel_control.py
git commit -m "feat: add channel control state"
```

### Task 2: Add desktop process lifecycle support

**Files:**
- Modify: `zero_agent/frontends/desktop_commands.py`
- Test: `tests/test_channel_control.py`

- [ ] **Step 1: Add failing process-control tests**

Patch `desktop_commands.psutil`/`subprocess.Popen` at test scope and assert:

```python
def test_channel_status_includes_configuration_and_link(monkeypatch):
    monkeypatch.setattr(desktop_commands, "running_services", lambda use_cache=True: {"bots/telegram_app.py": 321})
    monkeypatch.setattr(channel_control, "get_channel_settings", lambda: {"telegram": {"linked": False}})
    monkeypatch.setattr(channel_control, "channel_is_configured", lambda definition, keys=None: True)
    row = next(item for item in desktop_commands.channel_statuses() if item["id"] == "telegram")
    assert row["running"] is True
    assert row["pid"] == 321
    assert row["linked"] is False


def test_start_channel_rejects_missing_configuration(monkeypatch):
    monkeypatch.setattr(channel_control, "channel_is_configured", lambda definition, keys=None: False)
    ok, message = desktop_commands.start_channel("telegram")
    assert ok is False
    assert "未配置" in message
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest tests/test_channel_control.py -q`
Expected: FAIL because desktop command channel functions do not exist.

- [ ] **Step 3: Extend service discovery without removing scheduler behavior**

Add channel entrypoints to `list_services()` as `{name: "bots/<module>", cmd: [sys.executable, absolute_script], doc, kind: "channel", channel_id}`. Generalize `running_services()` to scan scheduler and channel markers using the existing psutil cache. Add `channel_statuses()`, `start_channel(channel_id)`, and `stop_channel(channel_id)`. `start_channel` returns a configuration error before spawning, returns success for an already-running channel, and rejects a child that exits during the existing 0.4-second check. `stop_channel` reuses terminate/wait/kill behavior and treats an absent process as success. Return rows with only non-sensitive metadata: `id`, `label`, `module`, `source`, `configured`, `requiredKeys`, `running`, `pid`, `linked`.

- [ ] **Step 4: Run the focused Python tests**

Run: `pytest tests/test_channel_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit process support**

```bash
git add zero_agent/frontends/desktop_commands.py tests/test_channel_control.py
git commit -m "feat: manage channel bot processes"
```

### Task 3: Expose authenticated bridge endpoints

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py`
- Test: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Add route contract tests**

Use the existing `create_app()` and authenticated test client convention. Assert `GET /channels` returns seven rows and no values named `token`, `secret`, `api_key`, or `api_secret`; assert `POST /channels/telegram/link` accepts `{"linked": false}` and returns the changed row. Stub `desktop_commands.channel_statuses`, `start_channel`, and `stop_channel` so tests do not spawn real services.

- [ ] **Step 2: Run the bridge tests to verify the new assertions fail**

Run: `pytest tests/test_desktop_bridge.py -q`
Expected: FAIL because the routes and handlers are not registered.

- [ ] **Step 3: Implement handlers and routes**

Add `channels_handler`, `channel_start_handler`, `channel_stop_handler`, and `channel_link_handler`. Validate the path ID through `channel_definition`; validate `linked` is a JSON boolean. Return `json_ok({"ok": True, "channels": channel_statuses()})` on success. Return 404 for unknown IDs, 409 for start/stop operation failures, and 400 for invalid link bodies. Register:

```python
app.router.add_get("/channels", channels_handler)
app.router.add_post("/channels/{channel_id}/start", channel_start_handler)
app.router.add_post("/channels/{channel_id}/stop", channel_stop_handler)
app.router.add_post("/channels/{channel_id}/link", channel_link_handler)
```

All routes remain behind the existing security middleware.

- [ ] **Step 4: Run the focused bridge tests**

Run: `pytest tests/test_desktop_bridge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the bridge API**

```bash
git add zero_agent/frontends/desktop_bridge.py tests/test_desktop_bridge.py
git commit -m "feat: expose channel control API"
```

### Task 4: Enforce App-link state in all bot entrypoints

**Files:**
- Modify: `zero_agent/bots/common.py`
- Modify: `zero_agent/bots/wechat_app.py`
- Modify: `zero_agent/bots/wecom_app.py`
- Modify: `zero_agent/bots/dingtalk_app.py`
- Modify: `zero_agent/bots/qq_app.py`
- Modify: `zero_agent/bots/feishu_app.py`
- Modify: `zero_agent/bots/telegram_app.py`
- Modify: `zero_agent/bots/discord_app.py`
- Test: `tests/test_channel_control.py`

- [ ] **Step 1: Add failing gate tests**

```python
def test_channel_is_linked_reads_shared_state(monkeypatch):
    monkeypatch.setattr(channel_control, "is_channel_linked", lambda channel: channel != "telegram")
    assert common.channel_is_linked("discord") is True
    assert common.channel_is_linked("telegram") is False

@pytest.mark.asyncio
async def test_mixin_ignores_commands_when_unlinked(monkeypatch):
    monkeypatch.setattr(common, "channel_is_linked", lambda _source: False)
    bot = FakeBot()
    await bot.handle_command("chat", "/help")
    assert bot.sent == []
```

- [ ] **Step 2: Run the bot gate tests to verify they fail**

Run: `pytest tests/test_channel_control.py -q`
Expected: FAIL because the common gate is not present.

- [ ] **Step 3: Add the shared gate and boundary checks**

Add a lazy import wrapper `channel_is_linked(source)` in `common.py`. At the start of `AgentBotMixin.handle_command` and `AgentBotMixin.run_agent`, return without sending or queuing work when the source is unlinked. Add the same guard to overridden Discord/Feishu methods and to each non-mixin or custom inbound path: Telegram command/message/photo/callback handlers, WeChat `on_message`, and the WeCom/DingTalk/QQ message callbacks. Put the WeChat guard before `_dl_media()` so an unlinked channel does not download attachments. Keep `/status` and transport heartbeats inactive while unlinked; no existing task is cancelled.

- [ ] **Step 4: Run bot tests**

Run: `pytest tests/test_channel_control.py tests/test_bots.py -q`
Expected: PASS.

- [ ] **Step 5: Commit link enforcement**

```bash
git add zero_agent/bots tests/test_channel_control.py
git commit -m "feat: gate bot messages by app link"
```

### Task 5: Add frontend RPC and channel settings modal

**Files:**
- Modify: `zero_agent/frontends/desktop/static/za-web.js`
- Modify: `zero_agent/frontends/desktop/static/index.html`
- Modify: `zero_agent/frontends/desktop/static/styles.css`
- Modify: `zero_agent/frontends/desktop/static/app.js`
- Test: `tests/frontend_channels.test.js`

- [ ] **Step 1: Write failing adapter and renderer tests**

Cover these observable contracts:

```javascript
await windowObj.zeroAgent.rpc('channels/list', {});
assert.equal(calls[0].url, 'http://127.0.0.1:14168/channels');
await windowObj.zeroAgent.rpc('channels/start', { channelId: 'telegram' });
assert.equal(calls[1].url, 'http://127.0.0.1:14168/channels/telegram/start');
assert.equal(calls[1].init.method, 'POST');
```

Load the app slice before bridge/init markers with fake DOM elements, call exported `renderChannelList` using rows that include a token-like `requiredKeys` value, and assert the rendered HTML contains the label and status but not the token name/value. Call `handleChannelToggle` with `linked=false` and assert the RPC receives `{channelId: 'telegram', linked: false}`.

- [ ] **Step 2: Run the Node regression to verify it fails**

Run: `node tests/frontend_channels.test.js`
Expected: FAIL because the RPC cases and renderer do not exist.

- [ ] **Step 3: Add RPC mappings and markup**

In `za-web.js`, add cases for `channels/list`, `channels/start`, `channels/stop`, and `channels/link`, using `encodeURIComponent` for channel IDs and POST bodies for mutations. Add a topbar `#channel-settings-btn` beside the theme button. Add `#channel-settings-modal` with backdrop, close/refresh buttons, explanatory copy, `#channel-list`, and footer close button. Keep labels bilingual only where existing UI already mixes Chinese/English; primary channel labels follow the specification.

- [ ] **Step 4: Implement channel state and modal behavior**

Add `state.channelStatuses`, `state.channelActionIds`, DOM refs, and functions `renderChannelList()`, `loadChannelStatuses()`, `openChannelSettings()`, `closeChannelSettings()`, `setChannelActionBusy()`, and `handleChannelToggle(channelId, action, value)`. Render status from API data. Running changes call start/stop and reload; linking changes call link and replace the returned snapshot. On failure, reload, add a diagnostic, and call `showError`; never leave a stale optimistic value. Disable only the active row controls while awaiting its request. Register click/change/backdrop/escape events during init and expose no credentials in text or attributes.

- [ ] **Step 5: Add styles matching existing UI**

Use `var(--bg)`, `var(--bg-subtle)`, `var(--border)`, `var(--text-muted)`, `var(--ok)`, `var(--warn)`, `var(--err)`, and existing radii/shadows. Add a compact card list, status pills, two-column controls, and an accessible switch whose native checkbox remains focusable. At `max-width: 620px`, stack controls and constrain modal height using the same responsive conventions as existing styles.

- [ ] **Step 6: Run the frontend regression**

Run: `node tests/frontend_channels.test.js`
Expected: PASS.

- [ ] **Step 7: Run existing frontend regressions**

Run: `node tests/frontend_agents.test.js && node tests/frontend_message_reconciliation.test.js && node tests/frontend_plan_ui.test.js && node tests/frontend_session_sidebar.test.js && node tests/frontend_plan_command.test.js`
Expected: all existing scripts print their `OK` marker and exit 0.

- [ ] **Step 8: Commit the frontend**

```bash
git add zero_agent/frontends/desktop/static tests/frontend_channels.test.js
git commit -m "feat: add channel control settings UI"
```

### Task 6: Verify end-to-end behavior and clean up

**Files:**
- Modify: `docs/superpowers/specs/2026-08-30-channel-control-design.md` only if implementation decisions materially differ.

- [ ] **Step 1: Run the focused Python suite**

Run: `pytest tests/test_channel_control.py tests/test_desktop_bridge.py tests/test_bots.py -q`
Expected: PASS.

- [ ] **Step 2: Run the frontend suite**

Run: `node tests/frontend_channels.test.js && node tests/frontend_agents.test.js && node tests/frontend_message_reconciliation.test.js && node tests/frontend_plan_ui.test.js && node tests/frontend_session_sidebar.test.js && node tests/frontend_plan_command.test.js`
Expected: all scripts exit 0.

- [ ] **Step 3: Run a bridge smoke scenario**

Start the real bridge with the project’s configured Python environment, call the authenticated `GET /channels`, and inspect that seven rows are returned with booleans for `configured`, `running`, and `linked`. Use a known unconfigured channel in the current environment for `POST /channels/{id}/start`; verify HTTP 409 and a non-sensitive error. Do not start a configured external bot or alter real credentials.

- [ ] **Step 4: Inspect the actual Web2 surface**

Open the bridge frontend in a browser, click the new channel settings icon, verify the modal uses the existing theme, verify all seven rows render, and toggle only the App-link switch for one channel. Refresh the modal and verify the persisted state is returned by the bridge. Restore the switch to its original value.

- [ ] **Step 5: Run the applicable full checks**

Run: `pytest tests/test_desktop_bridge.py tests/test_bots.py tests/test_channel_control.py -q` and the frontend command from Step 2. Expected: PASS with no new diagnostics or failures.

- [ ] **Step 6: Review the final diff for scope and secrets**

Run: `git diff --check` and inspect changed files. Confirm no token/secret values, temporary process output, or generated settings files are staged. Remove only temporary test artifacts created by this work.
