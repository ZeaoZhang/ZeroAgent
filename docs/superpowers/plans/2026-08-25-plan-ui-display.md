# Plan UI Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make desktop Plan mode readable and keep long Plan titles inside the normal topbar bounds.

**Architecture:** Keep the backend contract unchanged. Store the latest ACP plan-entry snapshot on each local session, render it through the existing `renderPlanStatus()` card, and constrain only the topbar title with CSS plus a full-value tooltip/accessibility label.

**Tech Stack:** Vanilla JavaScript, CSS, Node built-in assertions, existing fake-DOM UI test harness.

---

### Task 1: Add red tests for Plan entry state and card structure

**Files:**
- Modify: `tests/frontend_plan_ui.test.js`
- Test target: `tests/frontend_plan_ui.test.js`

- [ ] **Step 1: Extend the fake DOM only as required for assertions**

Add a small recursive class/text helper or selector support so tests can inspect Plan entry nodes and their text without testing implementation-specific HTML strings.

- [ ] **Step 2: Add the failing entry-state test**

Exercise `handleNotification()` with an ACP `plan` update containing completed, in-progress, and pending entries. Assert the active session stores three normalized entries and `renderPlanStatus()` exposes a progress summary and one rendered node for each entry state.

- [ ] **Step 3: Add the failing empty-entry and title metadata tests**

Assert a Plan session with no entries renders a waiting-progress label and no fabricated step nodes. Assert the session title retains its complete long value in the rendered title element's accessible/full-value property.

- [ ] **Step 4: Run the focused test and verify the expected failure**

Run:

```bash
node tests/frontend_plan_ui.test.js
```

Expected: FAIL because local sessions have no `planEntries`, ACP plan updates are still generic turns, and the Plan card has no progress/entry nodes.

### Task 2: Implement structured Plan metadata and rendering

**Files:**
- Modify: `zero_agent/frontends/desktop/static/app.js:896-903, 932, 1786-1789, 1880-1885, 2917-2970`

- [ ] **Step 1: Initialize and merge normalized Plan entries**

Initialize `planEntries: []` in `createLocalSession()`. Add a normalizer that maps each ACP entry to string `content` and string `status`, using `pending` and an empty content fallback when fields are absent. In the `plan` notification branch, replace the session snapshot and refresh the active Plan card.

- [ ] **Step 2: Render progress and stateful entries**

Update `renderPlanStatus()` to render a progress summary when entries exist, a waiting label otherwise, and a list of entry nodes. Use `textContent`; map `completed`, `in_progress`, and all other states to explicit visual classes and readable labels. Preserve the existing ready-only execute button.

- [ ] **Step 3: Run the focused test and verify it passes**

Run:

```bash
node tests/frontend_plan_ui.test.js
```

Expected: PASS, including the pre-existing Plan RPC, execute, duplicate-request, background-session, and restore coverage.

### Task 3: Constrain the topbar title and Plan card text

**Files:**
- Modify: `zero_agent/frontends/desktop/static/app.js:931-933, 2408-2411`
- Modify: `zero_agent/frontends/desktop/static/styles.css:491-518, 713-739`

- [ ] **Step 1: Preserve the full title for accessibility**

Whenever the active title is assigned, set the title element's `title` and `aria-label` to the full session title. Do not alter the stored session title.

- [ ] **Step 2: Add shrink and wrapping rules**

Allow `.topbar-left` and `#session-title` to shrink, bound title width with a viewport-relative maximum, and use one-line ellipsis. Make Plan task/entries wrap within the card; keep paths breakable.

- [ ] **Step 3: Add a focused CSS/source assertion if the harness supports it**

Assert the stylesheet contains the required title overflow rules and Plan entry wrapping rules only if this matches existing source-level test conventions; otherwise verify through the browser smoke check.

### Task 4: Run regression verification and review

**Files:**
- Verify: `tests/frontend_plan_ui.test.js`
- Verify: relevant Python desktop bridge tests

- [ ] **Step 1: Run the focused frontend test**

```bash
node tests/frontend_plan_ui.test.js
```

Expected: PASS.

- [ ] **Step 2: Run backend Plan contract tests**

```bash
pytest -q tests/test_desktop_bridge.py -k 'plan or session_plan'
```

Expected: PASS; backend behavior remains unchanged.

- [ ] **Step 3: Inspect the final diff for scope**

Confirm only desktop frontend/test files and the new design/plan documents changed; keep unrelated pre-existing worktree changes untouched.

- [ ] **Step 4: Request code review**

Review the final diff for standards, security, accessibility, and consistency with the design specification before declaring completion.
