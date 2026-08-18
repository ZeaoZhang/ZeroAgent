# `/plan` Command and Plan SOP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose ZeroAgent's existing `TaskMode.PLAN` as a real `/plan <task>` command across Desktop, CLI, and TUI, persist its lifecycle, and align `plan_sop.md` with explicit user approval before execution.

**Architecture:** Create one frontend-neutral plan workspace helper. Extend `ZeroAgent.run()` and `AgentRunner.put_task()` with an initial task mode and plan path so Plan Mode is initialized in the Handler that actually runs. Desktop gets dedicated plan resource endpoints and persisted session metadata; CLI/TUI use the same task-start contract. A verified plan transitions to `ready`; only explicit user approval starts an EXECUTING task.

**Tech Stack:** Python 3.10+, dataclasses, pathlib, pytest, aiohttp, vanilla JavaScript, Node.js assertion tests, Markdown SOPs.

**Reference:** Approved design at `docs/superpowers/specs/2026-08-18-plan-command-design.md`; comparison source at `../AgentExplore/GenericAgent/ga.py`, `../AgentExplore/GenericAgent/frontends/plan_state.py`, and `../AgentExplore/GenericAgent/memory/plan_sop.md`.

**Workspace rule:** Existing user changes in `tests/frontend_message_reconciliation.test.js`, `zero_agent/frontends/desktop/static/app.js`, and `zero_agent/llm/sessions.py` are unrelated working-tree changes. Do not overwrite or stage them unless a task explicitly requires the same file; review each diff before editing.

---

### Task 1: Add the shared plan workspace helper

**Files:**
- Create: `zero_agent/frontends/plan_command.py`
- Create: `tests/test_plan_command.py`

- [ ] **Step 1: Write failing workspace tests**

Add tests for the public helper contract:

```python
from pathlib import Path

import pytest

from zero_agent.frontends.plan_command import PlanWorkspace, create_plan_workspace


def test_create_plan_workspace_writes_safe_skeleton(tmp_path: Path) -> None:
    workspace = create_plan_workspace(str(tmp_path), "实现用户头像上传")

    assert isinstance(workspace, PlanWorkspace)
    assert Path(workspace.directory).parent == tmp_path.resolve()
    assert Path(workspace.path) == Path(workspace.directory) / "plan.md"
    assert Path(workspace.path).is_file()
    text = Path(workspace.path).read_text(encoding="utf-8")
    assert "实现用户头像上传" in text
    assert "探索发现" in text
    assert "执行计划" in text
    assert "验证" in text


def test_create_plan_workspace_never_escapes_root(tmp_path: Path) -> None:
    workspace = create_plan_workspace(str(tmp_path), "../../outside/../任务")
    assert Path(workspace.directory).parent == tmp_path.resolve()


def test_create_plan_workspace_uses_unique_directory_for_duplicate_task(tmp_path: Path) -> None:
    first = create_plan_workspace(str(tmp_path), "same task")
    second = create_plan_workspace(str(tmp_path), "same task")
    assert first.directory != second.directory
    assert Path(first.path).is_file()
    assert Path(second.path).is_file()


def test_create_plan_workspace_rejects_empty_task(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task"):
        create_plan_workspace(str(tmp_path), "  ")
```

Add boundary cases for Unicode-only input, long input truncation, and a root that does not exist yet. Assert that all generated paths resolve below the supplied root and that only `plan.md` is created inside the new directory.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
.venv/bin/pytest tests/test_plan_command.py -q
```

Expected: collection fails because `zero_agent.frontends.plan_command` and `create_plan_workspace` do not exist.

- [ ] **Step 3: Implement the helper**

Define the immutable result and helper:

```python
@dataclass(frozen=True)
class PlanWorkspace:
    root: str
    directory: str
    path: str
    slug: str


def create_plan_workspace(root: str, task: str) -> PlanWorkspace:
    ...
```

Implementation requirements:

1. Strip and reject an empty task with `ValueError`.
2. Resolve `root` and create it with `mkdir(parents=True, exist_ok=True)`.
3. Normalize the task with Unicode normalization, convert non-word runs to `-`, lower-case ASCII where possible, and use `task` when no safe characters remain.
4. Truncate the slug to a fixed bounded length before adding a unique suffix.
5. Create `plan_<slug>` below the resolved root with `mkdir(exist_ok=False)`; on `FileExistsError`, retry with a short UUID suffix.
6. Write a non-empty UTF-8 `plan.md` skeleton containing the escaped task title, `## 探索发现`, `## 执行计划`, and `## 验证` sections. Do not insert fake `[x]` items or implementation claims.
7. Resolve the final paths and assert the plan directory remains a child of root. On write failure, remove only the new directory and re-raise the original exception.

Keep this module free of UI imports and Agent state mutation so Desktop, CLI, and TUI can share it.

- [ ] **Step 4: Run helper tests**

Run:

```bash
.venv/bin/pytest tests/test_plan_command.py -q
```

Expected: all workspace, path-safety, duplicate, and boundary tests pass.

- [ ] **Step 5: Commit the isolated helper change**

```bash
git add zero_agent/frontends/plan_command.py tests/test_plan_command.py
git commit -m "feat: add safe plan workspace creation"
```

---

### Task 2: Initialize PLAN and EXECUTING contracts at task start

**Files:**
- Modify: `zero_agent/core/agent.py:414-499`
- Modify: `zero_agent/runners/agent_runner.py:523-559,650-705`
- Test: `tests/test_agent.py`
- Test: `tests/test_agent_runner.py`

- [ ] **Step 1: Add failing contract initialization tests**

Add a `ZeroAgent.run()` test that drives the generator with a stubbed loop and asserts the fresh Handler receives:

```python
TaskContract(
    task_id="task-generated-by-run",
    user_request="plan task",
    mode=TaskMode.PLAN,
    plan_path=str(plan_path),
)
```

Add a second test for `TaskMode.EXECUTING` with the same `plan_path`. Add a regression test proving the default `agent.run("ordinary task")` remains `TaskMode.OPEN` with no plan path.

Add an `AgentRunner.put_task()` queue test that monkeypatches the wrapped Agent's `run()` and asserts the queued fields arrive as:

```python
initial_mode=TaskMode.PLAN
plan_path=str(plan_path)
```

- [ ] **Step 2: Run the focused tests and confirm failure**

```bash
.venv/bin/pytest tests/test_agent.py tests/test_agent_runner.py -q
```

Expected: the new calls fail with unexpected keyword arguments or the captured contract remains `TaskMode.OPEN`.

- [ ] **Step 3: Extend the task-start signatures without changing defaults**

Add keyword-only parameters to `ZeroAgent.run()`:

```python
def run(
    self,
    user_input: str,
    system_prompt: Optional[str] = None,
    initial_user_content: Optional[str] = None,
    *,
    initial_mode: TaskMode = TaskMode.OPEN,
    plan_path: Optional[str] = None,
) -> Generator[Any, None, TerminalEvent]:
```

When no pending task exists, require a non-empty `plan_path` for `TaskMode.PLAN` and `TaskMode.EXECUTING`, then initialize the contract with `initial_mode` and `plan_path` instead of hard-coding `OPEN`. Preserve ordinary OPEN calls, `task_id` generation, evidence ledger reset, and `plan_verify_status="missing"`. Keep the existing 120-turn behavior for PLAN.

Extend `AgentRunner.put_task()`:

```python
def put_task(
    self,
    query: str,
    source: str = "user",
    images: Optional[list] = None,
    *,
    task_mode: TaskMode = TaskMode.OPEN,
    plan_path: Optional[str] = None,
) -> queue.Queue:
```

Store `task_mode` and `plan_path` in the queue item. In the worker's `self._agent.run()` call, pass them as `initial_mode=...` and `plan_path=...`. Keep all existing callers valid through the OPEN defaults.

- [ ] **Step 4: Run core and runner tests**

```bash
.venv/bin/pytest tests/test_agent.py tests/test_agent_runner.py -q
```

Expected: new PLAN/EXECUTING initialization tests and all existing AgentRunner tests pass.

- [ ] **Step 5: Commit the task-start contract change**

```bash
git add zero_agent/core/agent.py zero_agent/runners/agent_runner.py tests/test_agent.py tests/test_agent_runner.py
git commit -m "feat: initialize task mode at agent start"
```

---

### Task 3: Add persisted Desktop plan metadata and lifecycle methods

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py:229-350,570-615,901-1057`
- Test: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Write failing Session and lifecycle tests**

Add tests for:

```python
session.plan_path is None
session.plan_status == "inactive"
session.plan_task == ""
```

Add a persistence round-trip test asserting `_session_to_persistable()` and `_session_from_persisted()` preserve all three fields.

Add manager tests using a fake `AgentRunner.put_task()`:

- `start_plan(sid, task)` creates a workspace, records `planning`, and passes `TaskMode.PLAN` plus the generated path;
- an empty task raises HTTP 400 and creates no directory;
- a running session raises HTTP 409 without aborting it;
- a second active plan raises HTTP 409 and returns the existing path;
- a submission exception restores the previous metadata and removes only the new workspace.

- [ ] **Step 2: Run the focused bridge tests and confirm failure**

```bash
.venv/bin/pytest tests/test_desktop_bridge.py -q
```

Expected: new fields and lifecycle methods are missing.

- [ ] **Step 3: Add Session fields and persistence projection**

Extend the `Session` dataclass:

```python
plan_path: Optional[str] = None
plan_status: str = "inactive"
plan_task: str = ""
```

Update `_session_to_persistable()`, `_session_from_persisted()`, and `AgentManager.snapshot()` to serialize and expose them as `planPath`, `planStatus`, and `planTask` using the existing camelCase Desktop payload convention.

Keep unknown persisted status values safe by restoring them to `inactive` rather than allowing arbitrary state strings.

- [ ] **Step 4: Implement `AgentManager.start_plan()`**

Add a manager method with this behavior:

```python
def start_plan(self, sid: str, task: str) -> dict:
    """Create a plan workspace and submit the first PLAN task atomically."""
```

Under `self.lock`:

1. load the session or raise the existing JSON 404;
2. reject `running`, `planning`, `ready`, and `executing` states with the specified HTTP error;
3. call `create_plan_workspace(sess.cwd or self.workspace_dir, task)`;
4. save the prior plan metadata;
5. set `plan_task`, `plan_path`, and `plan_status="planning"`;
6. call a mode-aware `submit_prompt()` path that passes `TaskMode.PLAN` and `plan_path` to `AgentRunner.put_task()`;
7. on submission failure, restore prior metadata and remove only the workspace returned by this call;
8. persist the session and emit a plan-aware state event outside the lock.

Do not call `handler.enter_plan_mode()` directly before the task starts; Task 2's initial contract is the only activation path.
Extend `AgentManager.submit_prompt()` with keyword-only `task_mode: TaskMode = TaskMode.OPEN` and `plan_path: Optional[str] = None`. Pass both values to `AgentRunner.put_task()` while preserving the existing prompt/image behavior and OPEN defaults.

Add `AgentManager.execute_plan(sid: str) -> dict` with this exact transition: under the manager lock, require `plan_status == "ready"`, require the plan file, reject a running session, set `plan_status="executing"`, and submit the preserved task plus the plan path with `TaskMode.EXECUTING`. If queue submission fails, restore `plan_status="ready"`; otherwise persist and emit the state after releasing the lock.

- [ ] **Step 5: Update terminal status transitions**

In `run_agent_turn()`, after the existing terminal status is known:

- retain `planning` for a non-terminal continuation;
- set `ready` only when the live Handler contract is PLAN, the terminal status is completed, the completion certificate is present, `plan_remaining == 0`, and `plan_verify_status` is `pass` or `partial_accepted`;
- set `failed` or `cancelled` for those terminal outcomes while preserving `plan_path`;
- do not set `ready` from a plain text claim or from an empty/missing plan file.

Emit the updated session state after persistence. Keep existing message and token-usage handling unchanged.

- [ ] **Step 6: Run bridge regression tests**

```bash
.venv/bin/pytest tests/test_desktop_bridge.py -q
```

Expected: lifecycle, rollback, persistence, and existing Desktop bridge tests pass.

- [ ] **Step 7: Commit Desktop metadata and manager lifecycle**

```bash
git add zero_agent/frontends/desktop_bridge.py tests/test_desktop_bridge.py
git commit -m "feat: persist desktop plan lifecycle"
```

---

### Task 4: Expose dedicated Desktop Plan endpoints and transport methods

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py:1240-1500,1633-1648`
- Modify: `zero_agent/frontends/desktop/static/za-web.js:90-145,182-210`
- Test: `tests/test_desktop_bridge.py`
- Test: `tests/frontend_plan_command.test.js`

- [ ] **Step 1: Add failing endpoint and adapter tests**

Test the route table includes:

```text
POST /session/{sid}/plan
POST /session/{sid}/plan/execute
```

Test `startPlan(sessionId, task)` posts exactly:

```json
{"task": "..."}
```

to `/session/{sid}/plan`, and `executePlan(sessionId)` posts to `/session/{sid}/plan/execute`.

Add endpoint tests for missing sessions, empty tasks, running sessions, non-ready execution, missing plan files, and successful accepted responses. Assert error status and JSON error text, not only HTTP 200.

- [ ] **Step 2: Run the focused tests and confirm failure**

```bash
.venv/bin/pytest tests/test_desktop_bridge.py -q
```

Expected: route lookup and adapter methods are absent.

- [ ] **Step 3: Add Bridge handlers and routes**

Implement:

```python
async def start_plan_handler(request):
    sid = request.match_info["sid"]
    data = await read_json(request)
    return json_ok(manager.start_plan(sid, str(data.get("task") or "")), status=202)


async def execute_plan_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.execute_plan(sid), status=202)
```

Use the existing middleware, `read_json()`, `json_ok()`, and HTTP exception style. Add routes beside the existing session prompt/cancel routes. `execute_plan()` must require `ready`, submit with `TaskMode.EXECUTING`, preserve the plan path, and restore `ready` if queue submission fails.

- [ ] **Step 4: Add Web2 adapter methods**

Add RPC cases:

```javascript
case 'session/plan': {
  const sid = params.sessionId || params.id || params.bridgeSessionId;
  if (!sid) throw new Error('session/plan missing sessionId');
  return http(`/session/${encodeURIComponent(sid)}/plan`, {
    method: 'POST',
    body: { task: params.task || '' },
  });
}
case 'session/plan/execute': {
  const sid = params.sessionId || params.id || params.bridgeSessionId;
  if (!sid) throw new Error('session/plan/execute missing sessionId');
  return http(`/session/${encodeURIComponent(sid)}/plan/execute`, {
    method: 'POST',
    body: {},
  });
}
```

Expose `startPlan(sessionId, task)` and `executePlan(sessionId)` on `window.zeroAgent` using the existing `rpc()` wrapper. Do not alter unrelated RPC methods.

- [ ] **Step 5: Run API and adapter tests**

```bash
.venv/bin/pytest tests/test_desktop_bridge.py -q
```

Expected: both endpoints return the documented response shape and all adapter route tests pass.

- [ ] **Step 6: Commit the Desktop API layer**

```bash
git add zero_agent/frontends/desktop_bridge.py zero_agent/frontends/desktop/static/za-web.js tests/test_desktop_bridge.py
git commit -m "feat: expose desktop plan endpoints"
```

---

### Task 5: Add Desktop `/plan` command handling and ready approval UI

**Files:**
- Modify: `zero_agent/frontends/desktop/static/app.js:8-22,2462-2775,1751-1877,3000-3600`
- Modify: `zero_agent/frontends/desktop/static/index.html` only if a new execution action needs a semantic button container
- Create: `tests/frontend_plan_command.test.js`
- Test: `tests/frontend_message_reconciliation.test.js` only if existing test exports need a non-overlapping plan notification hook

- [ ] **Step 1: Write failing JavaScript behavior tests**

Load `app.js` through the existing slash-command marker pattern, inject a fake `window.zeroAgent`, and assert:

```javascript
await handleSlash('/plan implement avatar upload');
assert.deepEqual(calls, [{ method: 'startPlan', task: 'implement avatar upload' }]);
```

Add tests asserting:

- `/plan` without a task shows usage and does not call the bridge;
- command completion includes `/plan` with `[task]`;
- a `session-state` payload carrying `planStatus: 'planning'` updates the local session;
- `planStatus: 'ready'` renders an execution affordance or exposes the execution callback;
- the callback does not call `executePlan` until activated;
- activation calls `executePlan` once and does not submit a normal prompt directly.

- [ ] **Step 2: Run the focused frontend test and confirm failure**

```bash
node tests/frontend_plan_command.test.js
```

Expected: the test fails because `/plan`, session plan fields, and the execution action are not implemented.

- [ ] **Step 3: Add local command registration and routing**

Add to `LOCAL_SLASH_COMMANDS`:

```javascript
{ cmd: '/plan', argHint: '[task]', description: '创建并运行一个可验证的计划流程' },
```

Add a `case 'plan'` to `handleSlash()` that:

1. rejects an empty argument with `Usage: /plan <task>`;
2. rejects an active runtime using the existing busy guard;
3. calls `window.zeroAgent.startPlan(await ensureBridgeSession(sess), arg)`;
4. merges the returned `planPath`, `planStatus`, and `planTask` into the session;
5. renders the system acknowledgement and Plan card;
6. never routes `/plan` through `runAgentSlash()`.

- [ ] **Step 4: Synchronize plan metadata and render approval**

Extend local session creation/normalization to initialize:

```javascript
planPath: null,
planStatus: 'inactive',
planTask: '',
```

In the `session-state` notification branch, merge `planPath`, `planStatus`, and `planTask` when present. When `planStatus === 'ready'`, render the existing plan presentation style with the path and an explicit execution button/callback. The callback must call `window.zeroAgent.executePlan()` and then refresh session state. Do not hide or overwrite existing message reconciliation behavior.

Keep the existing `kind === 'plan'` ACP branch as a compatibility renderer; metadata notifications are the authoritative Desktop state channel.

- [ ] **Step 5: Expose test hooks without production-only branches**

Export only the smallest needed functions through the existing test-injection pattern, such as:

```javascript
globalThis.__testExports = {
  ...existingExports,
  handleSlash,
  getAllSlashCommands,
  handleNotification,
};
```

Use fake DOM methods already present in `frontend_session_sidebar.test.js`; do not add a browser framework or modify production behavior for tests.

- [ ] **Step 6: Run all affected frontend tests**

```bash
node tests/frontend_plan_command.test.js
node tests/frontend_message_reconciliation.test.js
node tests/frontend_session_sidebar.test.js
```

Expected: all three commands exit with status 0 and existing frontend regressions remain green.

- [ ] **Step 7: Commit the Desktop UI change**

```bash
git add zero_agent/frontends/desktop/static/app.js tests/frontend_plan_command.test.js
# Add index.html only if the implementation actually changed it.
git commit -m "feat: add desktop plan command and approval UI"
```

Do not stage the pre-existing unrelated changes in `tests/frontend_message_reconciliation.test.js` or other files.

---

### Task 6: Add CLI/TUI `/plan` parity without mutating a pre-run Handler

**Files:**
- Modify: `zero_agent/frontends/commands/slash_commands.py:24-363`
- Modify: `zero_agent/runners/cli.py:331-363,459-532`
- Modify: `zero_agent/frontends/tui.py:481-556`
- Test: `tests/test_slash_commands.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add failing command and mode propagation tests**

Add a registry test asserting `/plan` appears with `arg_hint="[task]"` and the description used by Desktop help.

Add a CLI test for empty `/plan` usage and a plan-task dispatch test using a fake Agent whose `run()` records:

```python
initial_mode is TaskMode.PLAN
plan_path is an existing plan.md
```

Add a TUI dispatch test at the method boundary, using a fake app/agent and asserting the special plan path does not call `handle_command()` as a normal side-effect-only command.

- [ ] **Step 2: Run focused CLI/command tests and confirm failure**

```bash
.venv/bin/pytest tests/test_slash_commands.py tests/test_cli.py -q
```

Expected: `/plan` is absent or the fake Agent receives a normal OPEN run.

- [ ] **Step 3: Register metadata without making generic dispatch mutate state**

Register `/plan` in the central command list with its description and `[task]` hint. The command definition must identify Plan as a task-start command rather than invoking `handler.enter_plan_mode()` on the existing Handler.

Keep ordinary `handle_command()` behavior unchanged for every existing command.

- [ ] **Step 4: Add CLI special dispatch**

In `_handle_slash_cmd()`, detect `/plan` before the generic command dispatcher:

1. parse the non-empty task argument;
2. reject a running Agent using its existing runtime guard;
3. call `create_plan_workspace(agent.config.workspace_dir, task)`;
4. call `_consume_prompt(agent, task, _display_chunk, initial_mode=TaskMode.PLAN, plan_path=workspace.path)` after extending `_consume_prompt()` to forward these keyword arguments to `agent.run()`;
5. print the terminal result using existing formatting;
6. remove the new workspace only when task startup fails before Agent execution.

Normal slash commands still use `handle_command()`.

- [ ] **Step 5: Add TUI special dispatch and plan approval**

In `_dispatch_command()`, handle `/plan` before `handle_command()` and append the raw command as a user message. Interpret exactly `/plan execute` as approval when the current plan status is `ready`; call the shared execute path with `TaskMode.EXECUTING`. For any other `/plan <task>`, create a workspace and start a PLAN task. Extend `_run_prompt()` / `_worker_run()` with optional `initial_mode` and `plan_path` arguments. Do not automatically start implementation when a PLAN task reaches `ready`.

When the TUI receives a verified ready state, show a system prompt requiring explicit user approval. The approval action is `/plan execute`; it must call `agent.run(..., initial_mode=TaskMode.EXECUTING, plan_path=...)` and must not automatically start implementation.

Use existing `plan_state.py` for live plan rendering; do not add message-scan activation.

- [ ] **Step 6: Run CLI/TUI regression tests**

```bash
.venv/bin/pytest tests/test_slash_commands.py tests/test_cli.py -q
```

Expected: `/plan` parity tests pass and all existing command behavior remains unchanged.

- [ ] **Step 7: Commit CLI/TUI parity**

```bash
git add zero_agent/frontends/commands/slash_commands.py zero_agent/runners/cli.py zero_agent/frontends/tui.py tests/test_slash_commands.py tests/test_cli.py
git commit -m "feat: add plan command to cli and tui"
```

---

### Task 7: Align Plan SOP and user-facing command documentation

**Files:**
- Modify: `README.md:242-250`
- Modify: `docs/quickstart.md:114-122`
- Test: `tests/test_packaging.py` only if package-data assertions need updating

- [ ] **Step 1: Write documentation acceptance checks**

Use repository search assertions or a focused documentation test to require these strings in the packaged SOP:

```text
/plan <task>
ready
verify_context.json
evidence.json
result.md
VERDICT: PASS / FAIL / PARTIAL
```

Also assert that the SOP names the packaged verification path:

```text
zero_agent/assets/memory_seed/sops/verify_sop.md
```

- [ ] **Step 2: Run the documentation checks and confirm failure**

```bash
.venv/bin/pytest tests/test_packaging.py -q
```

If existing packaging tests do not cover SOP contents, run the exact Python assertion script added in Step 1 and record the missing `/plan` entry before implementation.

- [ ] **Step 3: Update `plan_sop.md`**

Preserve GenericAgent's useful protocol but replace the sole public-entry statement with:

```markdown
## 入口

- 用户从 `/plan <task>` 进入。Desktop、CLI、TUI 都必须创建真实 `TaskMode.PLAN` 契约。
- Agent/Subagent 内部兼容入口仍可使用：
  `code_run({'inline_eval': True, 'script': 'handler.enter_plan_mode("./plan_XXX/plan.md")'})`
- `/plan` 创建 `plan_XXX/plan.md` 后，先探索，再写计划；禁止直接修改实现文件。
```

Add the artifact contract:

```markdown
plan_XXX/
├── plan.md
├── exploration_findings.md
├── verify_context.json
├── evidence.json
└── result.md
```

State explicitly that valid verification changes the session to `ready`, and implementation requires explicit user approval. Retain `[D]`, `[P]`, `[?]`, Mini verification, zero-`[ ]` termination check, and the existing `verify_sop.md` protocol.
- [ ] **Step 4: Update command help and quickstart**

Add one `/plan` command entry to each existing command table at `README.md:242-250` and `docs/quickstart.md:114-122`:

```text
/plan <task>       创建并运行一个可验证的计划流程
```

Explain in the surrounding text that Plan completion waits for explicit execution approval. Do not claim `/plan` automatically modifies code.

- [ ] **Step 5: Verify packaged SOP files**

```bash
.venv/bin/pytest tests/test_packaging.py -q
python -c 'from pathlib import Path; p=Path("zero_agent/assets/memory_seed/sops/plan_sop.md"); text=p.read_text(encoding="utf-8"); required=("/plan <task>", "ready", "verify_context.json", "evidence.json", "result.md", "VERDICT"); assert all(x in text for x in required); print("plan SOP contract passed")'
```

Expected: packaging passes and the command prints `plan SOP contract passed`.

- [ ] **Step 6: Commit SOP and documentation changes**

```bash
git add zero_agent/assets/memory_seed/sops/plan_sop.md README.md docs/quickstart.md tests/test_packaging.py
# Stage only the documentation file that was actually modified.
git commit -m "docs: align plan SOP with plan command"
```

---

### Task 8: Run integration verification and review the final diff

**Files:**
- Test: `tests/test_plan_command.py`
- Test: `tests/test_agent.py`
- Test: `tests/test_agent_runner.py`
- Test: `tests/test_desktop_bridge.py`
- Test: `tests/test_slash_commands.py`
- Test: `tests/test_cli.py`
- Test: `tests/frontend_plan_command.test.js`
- Test: affected existing frontend tests

- [ ] **Step 1: Run the focused Python suite**

```bash
.venv/bin/pytest tests/test_plan_command.py tests/test_agent.py tests/test_agent_runner.py tests/test_desktop_bridge.py tests/test_slash_commands.py tests/test_cli.py -q
```

Expected: all focused Python tests pass.

- [ ] **Step 2: Run the focused frontend suite**

```bash
node tests/frontend_plan_command.test.js
node tests/frontend_message_reconciliation.test.js
node tests/frontend_session_sidebar.test.js
```

Expected: all commands exit with status 0.

- [ ] **Step 3: Exercise the real bridge API**

Start the existing Desktop bridge using its documented project launcher. Create a session, then call:

```bash
BASE_URL="http://127.0.0.1:14168"
SID="$(curl -sS -X POST "$BASE_URL/session/new" -H 'Content-Type: application/json' -d '{}' | python -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')"
curl -sS -X POST "$BASE_URL/session/$SID/plan" \
  -H 'Content-Type: application/json' \
  -d '{"task":"plan command smoke test"}'
```

Verify all of the following from the response and filesystem:

- HTTP 202;
- `planStatus` is `planning`;
- `planPath` is below the session workspace;
- `plan.md` is non-empty;
- the session enters `running` without an OPEN contract.

Then exercise the adversarial paths:

```bash
curl -sS -o /tmp/plan-empty.json -w '%{http_code}\n' \
  -X POST "$BASE_URL/session/$SID/plan" \
  -H 'Content-Type: application/json' -d '{"task":"  "}'

curl -sS -o /tmp/plan-execute-not-ready.json -w '%{http_code}\n' \
  -X POST "$BASE_URL/session/$SID/plan/execute"
```

Expected: empty task returns 400; execution before `ready` returns 409; no extra plan directory is created.

- [ ] **Step 4: Verify ready approval boundary**

Use a deterministic fixture plan and valid `verify_context.json`, `evidence.json`, and `result.md` with a literal `VERDICT: PASS`. Confirm the bridge changes the session to `ready` and does not start a thread or code-changing task.

Call:

```bash
curl -sS -X POST "$BASE_URL/session/$SID/plan/execute"
```

Expected: only this call changes the session to `executing` and starts an `EXECUTING` task with the same `planPath`.

- [ ] **Step 5: Run the complete project test suite**

```bash
.venv/bin/pytest -q
```

Expected: the full suite passes. If a failure occurs, fix only regressions caused by this feature; do not alter unrelated working-tree changes.

- [ ] **Step 6: Review changed files and packaging**

Run:

```bash
git diff --check
git status --short
```

Confirm:

- all new files are included in packaging where required;
- no stale `/plan` rejection remains in Desktop command resolution;
- no code path calls `handler.enter_plan_mode()` before `ZeroAgent.run()` for a new task;
- no automatic execution occurs at `ready`;
- unrelated pre-existing modifications remain unstaged or unchanged.

- [ ] **Step 7: Commit any final test-only adjustment**

Only if the previous steps require a final adjustment:

```bash
git add zero_agent/frontends/plan_command.py zero_agent/core/agent.py zero_agent/runners/agent_runner.py zero_agent/frontends/desktop_bridge.py zero_agent/frontends/desktop/static/za-web.js zero_agent/frontends/desktop/static/app.js zero_agent/frontends/commands/slash_commands.py zero_agent/runners/cli.py zero_agent/frontends/tui.py zero_agent/assets/memory_seed/sops/plan_sop.md tests/test_plan_command.py tests/test_agent.py tests/test_agent_runner.py tests/test_desktop_bridge.py tests/test_slash_commands.py tests/test_cli.py tests/frontend_plan_command.test.js
git commit -m "test: verify plan command lifecycle"
```

Do not create a no-op commit if no final adjustment is needed.
